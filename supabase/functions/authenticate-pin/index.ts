// authenticate-pin — the only online login path for a till.
//
// A cashier's credential is an employee code and a PIN, not an email and a
// password. Verifying a PIN needs privileged reads (the argon2id hash is
// revoked from `authenticated` in 0003_rls.sql), so it happens here, where the
// service_role key stays on the server. Bundling that key into the desktop
// build would hand every till full database access — a PyInstaller bundle
// decompiles trivially (architecture §1.7).
//
// Returns a real Supabase session, so the access token carries the claims the
// custom access token hook stamped in, and RLS applies to everything the
// terminal does with it afterwards.
//
// Deploy:  supabase functions deploy authenticate-pin --no-verify-jwt
// (--no-verify-jwt because the caller has no JWT yet; the anon key still
// gates it, and the rate limit below does the rest.)

import { createClient } from "jsr:@supabase/supabase-js@2";
import { argon2id, argon2Verify } from "npm:hash-wasm@4";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY")!;

// Offline snapshots are valid for 14 days, after which one online
// authentication is required (architecture §11.4). This bounds how long a
// dismissed employee can keep opening a till.
const SNAPSHOT_TTL_DAYS = 14;

// ── argon2id ────────────────────────────────────────────────────────────────
//
// Must match app/config.py. `scripts/remint_pin_hashes.py` writes both the
// constants below and the decoy from those settings, and a test fails if they
// drift — because nothing else would notice.
//
// Verification never reads these: an argon2 hash carries its own parameters,
// so a hash minted at any cost verifies correctly. They are used for two
// things only — minting a replacement for a hash that was made at some other
// cost, and keeping the decoy honest.
const ARGON2 = { memoryKiB: 65536, timeCost: 3, parallelism: 4 } as const;
const ARGON2_PARAMS = `m=${ARGON2.memoryKiB},t=${ARGON2.timeCost},p=${ARGON2.parallelism}`;

// A decoy for an unknown employee code, so a missing code and a wrong PIN cost
// the same wall-clock time. **It has to carry the same parameters as a real
// hash**, or it is not a decoy: it previously said `t=2` while real hashes
// said `t=12`, so an unknown code answered roughly six times faster and
// employee codes could be enumerated with a stopwatch. Generated, not typed.
const DECOY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$5EGpi0dpbH7a2BemXBBVwg$MuglEN3+wAVgYzNtkw7mzMl9SScqMEG7OoEXAkhDF3U";

async function mintPinHash(pin: string): Promise<string> {
  return await argon2id({
    password: pin,
    salt: crypto.getRandomValues(new Uint8Array(16)),
    parallelism: ARGON2.parallelism,
    iterations: ARGON2.timeCost,
    memorySize: ARGON2.memoryKiB,
    hashLength: 32,
    outputType: "encoded",
  });
}

// A till types a PIN wrong occasionally; a script types thousands. Ten
// attempts per code per five minutes leaves the cashier alone and stops
// enumeration cold.
const MAX_ATTEMPTS = 10;
const WINDOW_MS = 5 * 60 * 1000;
const attempts = new Map<string, { count: number; resetAt: number }>();

function rateLimited(key: string): boolean {
  const now = Date.now();
  const entry = attempts.get(key);
  if (!entry || now > entry.resetAt) {
    attempts.set(key, { count: 1, resetAt: now + WINDOW_MS });
    return false;
  }
  entry.count += 1;
  return entry.count > MAX_ATTEMPTS;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

// One shape for every rejection. Distinguishing "no such employee" from
// "wrong PIN" would turn this endpoint into a staff roster.
const reject = () => json({ error: "invalid_credentials" }, 401);

interface LoginBody {
  employee_code?: string;
  pin?: string;
  store_code?: string;
  terminal_code?: string;
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  let body: LoginBody;
  try {
    body = await req.json();
  } catch {
    return json({ error: "invalid_json" }, 400);
  }

  const employeeCode = (body.employee_code ?? "").trim().toUpperCase();
  const pin = body.pin ?? "";
  const storeCode = (body.store_code ?? "").trim().toUpperCase();

  if (!employeeCode || !pin || !storeCode) {
    return json({ error: "missing_fields" }, 400);
  }
  if (rateLimited(`${employeeCode}:${storeCode}`)) {
    return json({ error: "too_many_attempts" }, 429);
  }

  const admin = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const { data: store } = await admin
    .from("stores")
    .select("id")
    .eq("code", storeCode)
    .eq("is_active", true)
    .maybeSingle();
  if (!store) return reject();

  const { data: employee } = await admin
    .from("employees")
    .select("user_id, employee_code, full_name, pin_hash, status")
    .eq("employee_code", employeeCode)
    .maybeSingle();

  const hash = employee?.pin_hash ?? DECOY_HASH;

  let pinOk = false;
  try {
    pinOk = await argon2Verify({ password: pin, hash });
  } catch {
    pinOk = false;
  }
  if (!employee || !pinOk) return reject();

  // Re-mint a hash made at some other cost. This is the only moment anyone
  // holds the plaintext PIN, so it is the only moment a rehash is possible.
  //
  // Without it, changing the parameters changes nothing: an argon2 hash
  // carries its own, so `authenticate-pin` would keep paying the old cost
  // forever on every account that already exists. Each one migrates itself on
  // next sign-in instead, with no announcement and no forced reset.
  //
  // Deliberately not awaited into the response path beyond the update itself,
  // and deliberately not fatal: a cashier whose rehash failed is a cashier who
  // signed in, and the next sign-in will try again.
  if (!employee.pin_hash.includes(ARGON2_PARAMS)) {
    try {
      const reminted = await mintPinHash(pin);
      const { error: rehashError } = await admin
        .from("employees")
        .update({ pin_hash: reminted })
        .eq("user_id", employee.user_id);
      if (rehashError) {
        console.error("authenticate-pin: rehash could not be stored", {
          employee_code: employeeCode,
          error: rehashError.message,
        });
      }
    } catch (cause) {
      console.error("authenticate-pin: rehash failed", {
        employee_code: employeeCode,
        error: cause instanceof Error ? cause.message : String(cause),
      });
    }
  }

  if (employee.status !== "active") {
    return json({ error: "account_disabled" }, 403);
  }

  // Roles are per store. No role in *this* store is not a login here.
  const { data: roleRows } = await admin
    .from("user_store_roles")
    .select("role_key")
    .eq("user_id", employee.user_id)
    .eq("store_id", store.id);

  const roles = (roleRows ?? []).map((r: { role_key: string }) => r.role_key);
  if (roles.length === 0) return reject();

  const { data: permissionRows } = await admin
    .from("role_permissions")
    .select("permission_key")
    .in("role_key", roles);

  const permissions = [
    ...new Set(
      (permissionRows ?? []).map((r: { permission_key: string }) => r.permission_key),
    ),
  ].sort();

  // Mint a genuine session. generateLink + verifyOtp is the supported way to
  // sign a user in without their password; the resulting access token goes
  // through custom_access_token_hook like any other login.
  // The three failures below are the only 500s this function returns, and
  // each one is logged before it is returned. The response stays generic —
  // the caller is a till at a counter and has no use for a GoTrue message —
  // but the cause has to be recoverable from the Edge Function logs, or the
  // person debugging is left with a status code and a shrug.
  //
  // Learned the hard way: `getUserById` had its `error` discarded, so an
  // Admin API failure surfaced as "employee_not_provisioned" — which reads
  // as a data problem and sent the investigation to entirely the wrong place.
  const { data: authUser, error: lookupError } = await admin.auth.admin
    .getUserById(employee.user_id);
  const email = authUser?.user?.email;
  if (lookupError || !email) {
    console.error("authenticate-pin: cannot read the auth user", {
      employee_code: employeeCode,
      user_id: employee.user_id,
      error: lookupError?.message ?? "the auth user has no email address",
    });
    return json({ error: "employee_not_provisioned" }, 500);
  }

  const { data: link, error: linkError } = await admin.auth.admin.generateLink({
    type: "magiclink",
    email,
  });
  if (linkError || !link?.properties?.hashed_token) {
    console.error("authenticate-pin: generateLink failed", {
      employee_code: employeeCode,
      error: linkError?.message ?? "no hashed_token in the generated link",
    });
    return json({ error: "session_mint_failed" }, 500);
  }

  const anon = createClient(SUPABASE_URL, ANON_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data: verified, error: verifyError } = await anon.auth.verifyOtp({
    token_hash: link.properties.hashed_token,
    type: "email",
  });
  if (verifyError || !verified?.session) {
    // This step runs custom_access_token_hook, so a broken hook lands here
    // rather than anywhere that mentions hooks.
    console.error("authenticate-pin: verifyOtp failed", {
      employee_code: employeeCode,
      error: verifyError?.message ?? "no session came back from verifyOtp",
    });
    return json({ error: "session_mint_failed" }, 500);
  }

  const signedAt = new Date();
  const expiresAt = new Date(
    signedAt.getTime() + SNAPSHOT_TTL_DAYS * 24 * 60 * 60 * 1000,
  );

  return json({
    access_token: verified.session.access_token,
    refresh_token: verified.session.refresh_token,
    // The terminal caches this to authenticate the same person offline
    // tomorrow. It is an argon2id hash of a PIN the holder already knows.
    pin_hash: employee.pin_hash,
    snapshot_signed_at: signedAt.toISOString(),
    snapshot_expires_at: expiresAt.toISOString(),
    user: {
      id: employee.user_id,
      employee_code: employee.employee_code,
      full_name: employee.full_name,
      store_id: store.id,
      status: employee.status,
      roles,
      permissions,
    },
  });
});

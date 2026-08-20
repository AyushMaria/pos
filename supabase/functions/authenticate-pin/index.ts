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
import { argon2Verify } from "npm:hash-wasm@4";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY")!;

// Offline snapshots are valid for 14 days, after which one online
// authentication is required (architecture §11.4). This bounds how long a
// dismissed employee can keep opening a till.
const SNAPSHOT_TTL_DAYS = 14;

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

  // Verify against a decoy hash when the employee is unknown, so that a
  // missing code and a wrong PIN cost the caller the same wall-clock time.
  const hash = employee?.pin_hash ??
    "$argon2id$v=19$m=65536,t=2,p=4$AAAAAAAAAAAAAAAAAAAAAA$" +
      "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

  let pinOk = false;
  try {
    pinOk = await argon2Verify({ password: pin, hash });
  } catch {
    pinOk = false;
  }
  if (!employee || !pinOk) return reject();

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
  const { data: authUser } = await admin.auth.admin.getUserById(employee.user_id);
  const email = authUser?.user?.email;
  if (!email) return json({ error: "employee_not_provisioned" }, 500);

  const { data: link, error: linkError } = await admin.auth.admin.generateLink({
    type: "magiclink",
    email,
  });
  if (linkError || !link?.properties?.hashed_token) {
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

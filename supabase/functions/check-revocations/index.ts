// check-revocations — which of the identities this till remembers are gone.
//
// The terminal caches employees so that a supervisor can authorise an override
// with the network down. Those rows are the whole reason `cached_users`
// exists, and nothing has ever purged them: `0004_access_token_hook.sql`
// asserts that "the terminal's next sync purges the offline snapshot" and no
// code ever made that true.
//
// ## Why this is a function and not a pull
//
// The obvious implementation — add `employees` to the puller's ENTITIES —
// cannot work, and would have shipped green. The puller runs under the
// signed-in cashier's own token (§11.2, deliberately), and
// `employees_select_self_or_manager` in 0003_rls.sql lets a cashier read
// exactly one row: their own. So the pull would have returned one row, revoked
// the person whose deactivation is already caught at next login, and missed
// every cached supervisor — the entire population the cache is for. A keyset
// pull returning one row looks exactly like one that worked.
//
// Reading other people's rows needs the service role, so it happens here,
// where that key stays on the server (§1.7).
//
// ## What it will not become
//
// **It answers about ids the caller already has, and only about the revoked
// ones.** Returning a status per id would make it an existence oracle: post
// any UUID, learn whether it is an employee of that shop. The response is a
// subset of the input, so a caller learns nothing it did not already know
// except which of its own rows to drop.
//
// **It verifies the caller's JWT itself.** This is the first place a cashier's
// token is exchanged for a privileged read, so the exchange has to be earned.
//
// The platform's own `verify_jwt` is not that check and must not be mistaken
// for it: the anon key *is* a signed project JWT, so a gateway configured to
// require one lets the anon key straight through. `authenticate-pin` is
// deployed with `verify_jwt: true` despite its own comment saying
// `--no-verify-jwt`, and it works — which is the proof that the flag is not
// deciding anything here. The check that matters is below, in code: the anon
// key is refused by name, and `auth.getUser()` is called under the caller's
// own token so a forged or expired one fails rather than being taken at face
// value.
//
// **It scopes to the caller's own store.** A valid token from one shop must
// not be able to ask about another shop's staff.
//
// Deploy:  supabase functions deploy check-revocations

import { createClient } from "jsr:@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY")!;

// A till caches the staff of one shop. The cap is here so a malformed or
// hostile request cannot turn one call into a table scan; a real terminal
// sends a handful.
const MAX_IDS = 200;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

interface CheckBody {
  store_code?: string;
  user_ids?: string[];
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authorization = req.headers.get("Authorization") ?? "";
  const token = authorization.replace(/^Bearer\s+/i, "");
  if (!token || token === ANON_KEY) {
    // The anon key is not a session. It is on every request this project
    // makes, so accepting it would mean accepting anybody.
    return json({ error: "not_signed_in" }, 401);
  }

  let body: CheckBody;
  try {
    body = await req.json();
  } catch {
    return json({ error: "invalid_json" }, 400);
  }

  const storeCode = (body.store_code ?? "").trim().toUpperCase();
  const userIds = (body.user_ids ?? []).filter(
    (id) => typeof id === "string" && id.length > 0,
  );
  if (!storeCode) return json({ error: "missing_fields" }, 400);
  if (userIds.length > MAX_IDS) return json({ error: "too_many_ids" }, 400);
  if (userIds.length === 0) return json({ revoked: [] });

  // Who is asking. Under the caller's own token, so a forged or expired one
  // fails here rather than being taken at face value.
  const caller = createClient(SUPABASE_URL, ANON_KEY, {
    global: { headers: { Authorization: `Bearer ${token}` } },
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data: user, error: userError } = await caller.auth.getUser();
  if (userError || !user?.user) return json({ error: "not_signed_in" }, 401);

  const admin = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const { data: store } = await admin
    .from("stores")
    .select("id")
    .eq("code", storeCode)
    .maybeSingle();
  if (!store) return json({ error: "unknown_store" }, 404);

  // The caller must belong to the store they are asking about. Checked
  // server-side against `user_store_roles` rather than trusted from the body,
  // for the same reason `authorize-override` reads `role_permissions` itself:
  // a claim in a request is a statement by the requester.
  const { data: callerRoles } = await admin
    .from("user_store_roles")
    .select("role_key")
    .eq("user_id", user.user.id)
    .eq("store_id", store.id);
  if (!callerRoles || callerRoles.length === 0) {
    return json({ error: "not_your_store" }, 403);
  }

  // Everyone in the asked-about set who is still active *in this store*.
  // Anyone absent from that answer is revoked as far as this till is
  // concerned — which covers three different real events with one query: the
  // account was disabled, the person left this store, or the row is gone.
  const { data: active } = await admin
    .from("employees")
    .select("user_id, user_store_roles!inner(store_id)")
    .in("user_id", userIds)
    .eq("status", "active")
    .eq("user_store_roles.store_id", store.id);

  const stillActive = new Set(
    (active ?? []).map((row: { user_id: string }) => row.user_id),
  );

  return json({
    // A subset of what was sent, never a status per id. The caller learns
    // which of its own rows to drop and nothing else.
    revoked: userIds.filter((id) => !stillActive.has(id)),
    checked_at: new Date().toISOString(),
  });
});

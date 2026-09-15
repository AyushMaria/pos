-- 0020_narrow_default_grants — take back what nothing uses.
--
-- Supabase grants `anon` and `authenticated` every table privilege on
-- anything created in `public`. Three tables have been narrowed so far, each
-- because a specific hole forced it: `product_prices` in 0003 to hide `cost`,
-- `employees` in 0003, and `stock_levels` in 0018 to stop a reorder-point
-- policy from also opening `on_hand`. Everything else still carries the lot.
--
-- ## What is and is not at risk
--
-- Not at risk, and worth saying plainly before the rest of this file reads
-- like an emergency: PostgREST only ever issues SELECT, INSERT, UPDATE and
-- DELETE, and RLS gates all four. Architecture §11.1's claim that RLS is the
-- real boundary still holds, and nothing here is reachable with an anon key.
--
-- What the blanket grants actually cost is three things:
--
-- 1. **TRUNCATE is not subject to RLS.** No policy can stop it, so the
--    privilege is the only thing standing in front of it. Nothing in this
--    system truncates anything.
-- 2. **Every future policy inherits 0018's trap.** On a table where UPDATE is
--    already granted table-wide, adding an UPDATE policy for one column opens
--    all of them. That cost an hour and a migration once already.
-- 3. **A table created without RLS is immediately public.** The advisors
--    already flag `legacy.products` as RLS-enabled-with-no-policy; the same
--    oversight on a new table in `public` would be worse, because the grants
--    are waiting.
--
-- ## Why this migration is narrow
--
-- The thorough fix is to revoke everything and grant back per table what each
-- one needs. That is the right end state and it is not this file: it would
-- touch twenty-two tables at once, and the sync path, the puller and the
-- admin screen all depend on those grants in ways that want a test run rather
-- than an argument. Recorded as debt at the bottom instead.
--
-- This file takes only the privileges with no legitimate caller anywhere.

-- ── TRUNCATE, everywhere ──────────────────────────────────────────────────

-- The one privilege RLS cannot gate, and nothing uses it. Not reachable
-- through PostgREST today, so this is defence in depth rather than a fix —
-- but it costs nothing and it removes the only way to empty a table that no
-- policy would see.
revoke truncate on all tables in schema public from anon, authenticated;

-- New tables would otherwise arrive with it again.
alter default privileges in schema public
    revoke truncate on tables from anon, authenticated;

-- ── The two append-only tables ────────────────────────────────────────────

-- `audit_log` and `stock_ledger` are append-only by design. Neither has an
-- UPDATE or DELETE policy, so RLS refuses both already — but a log that the
-- privilege system says may be rewritten is a log with a caveat, and the
-- point of an audit trail is that it has none.
--
-- 0019 puts the catalogue's trail in `audit_log`, which is what makes this
-- worth doing now rather than later: it is about to start carrying the only
-- record of who changed a price.
revoke all on public.audit_log from anon, authenticated;
grant select, insert on public.audit_log to authenticated;

revoke all on public.stock_ledger from anon, authenticated;
grant select, insert on public.stock_ledger to authenticated;

-- ── A trigger function on the public API ──────────────────────────────────

-- `apply_stock_delta` is the SECURITY DEFINER trigger that maintains
-- `stock_levels` from the ledger. Because it lives in `public`, PostgREST
-- publishes it at `/rest/v1/rpc/apply_stock_delta` and both roles may call
-- it.
--
-- Calling it would fail — Postgres refuses to invoke a trigger function
-- directly — so this is not an open door either. It is that a SECURITY
-- DEFINER function should never be reachable from the public API by
-- accident, and a trigger needs no EXECUTE grant to fire: the trigger runs
-- regardless of the calling user's privileges on the function.
revoke execute on function public.apply_stock_delta() from anon, authenticated;
revoke execute on function public.apply_stock_delta() from public;

-- ── Recorded debt, not fixed here ─────────────────────────────────────────
--
-- **Per-table narrowing.** Twenty-two tables in `public` still grant
-- `anon` and `authenticated` INSERT, UPDATE and DELETE table-wide, with RLS
-- the only thing refusing them. The end state is `revoke all` plus a grant
-- naming the columns each role genuinely writes, table by table, the way
-- 0003 did for `product_prices` and 0018 for `stock_levels`. Do it a table at
-- a time with the RLS suite green between each, not in one commit.
--
-- **`anon` on POS tables.** Nothing in this system reads or writes as `anon`
-- — the terminal signs in, and every policy is `to authenticated`, so an
-- anonymous caller already matches zero rows. Revoking it outright would make
-- that deny explicit rather than incidental. Left out here only because
-- proving nothing uses it needs the login path exercised end to end.
--
-- **Mutable `search_path` on twelve functions.** Flagged by the advisors. All
-- twelve are SECURITY INVOKER, where a mutable search path is a hygiene
-- warning rather than a privilege-escalation route — the two SECURITY
-- DEFINER functions, `pos.next_internal_barcode` and `apply_stock_delta`,
-- both already pin theirs. Worth a sweep; not worth doing blind to twelve
-- function bodies at once.

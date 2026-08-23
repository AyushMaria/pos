-- 0009_sync_insert_policies — let a terminal push work it did not personally
-- take.
--
-- 0003 wrote three insert policies that require the row's actor to be the
-- caller:
--
--     sales_insert              cashier_id = auth.uid()
--     register_sessions_insert  user_id    = auth.uid()
--     cash_movements_insert     actor_id   = auth.uid()
--
-- That premise is true of a direct write from a signed-in user and false of
-- every sync push. A terminal accumulates a day of sales offline — often
-- across a shift change — and then pushes the backlog using whichever session
-- happens to be signed in when the network returns. The pusher sends up to
-- 200 envelopes per call and `sync_push` is a single transaction, so one sale
-- rung up by anyone else aborts the whole batch.
--
-- Found on a real terminal, not in a test: four queued sales, one taken by
-- C001 and three by M001. Signed in as M001, C001's row was refused and took
-- the other three down with it. Signed in as C001, the reverse. Neither
-- cashier could drain the queue — a permanent deadlock, retried every cycle
-- for as long as the till stayed open, because pusher._raise_for classifies
-- 403 as transient and never quarantines it. Nothing appeared in
-- /sync/failures, so the one place built to show a refusal stayed empty.
--
-- The fix is to stop treating the actor id as an authorization claim. It is
-- provenance: a fact the terminal records about who was at the counter, which
-- travels with the row and is what `audit_log` and the receipt are for.
-- Authorization for an insert is what it always should have been — the caller
-- belongs to the store, and holds the permission for that kind of row.
--
-- What this gives up: a cashier with `sale.create` can now insert a sale
-- attributed to a colleague. That was never really prevented — the terminal
-- composes the payload and is trusted to report who rang up a sale honestly,
-- exactly as it is trusted to report the total. What it buys is that a shop
-- whose morning cashier has gone home can still get the day's takings to the
-- cloud.
--
-- Sales stay append-only: no update or delete policy is added here, and a
-- correction remains a compensating row.

-- ── sales ─────────────────────────────────────────────────────────────────

drop policy if exists sales_insert on public.sales;

create policy sales_insert on public.sales
    for insert to authenticated
    with check (pos.in_store(store_id) and pos.has_perm('sale.create'));

-- The insert policy is not the only gate. `sync_push` inserts with
-- `on conflict (id) do nothing` — that is what makes a re-send after a
-- dropped acknowledgement a no-op instead of a second sale — and Postgres
-- evaluates the table's SELECT policy to perform the conflict check. So
-- `sales_select` silently gates every push as well, and it reads
-- `cashier_id = auth.uid() or pos.has_perm('report.sales.store')`.
--
-- A manager clears that on the permission. A cashier clears it only for their
-- own rows, so relaxing the insert policy alone would fix the deadlock for
-- managers and leave every cashier exactly where they started. That is a
-- worse failure than the original: it works when you test it as a manager.
--
-- The extra policy below is deliberately narrow — same store, and only for
-- someone who may create a sale in the first place. It is additive:
-- `sales_select` stays as it is, and policies are OR'd.
--
-- What it changes: a cashier can now read their own store's sales, where
-- before they could read only their own. For a single-till shop where staff
-- share one counter, that is close to no change at all. If that is not
-- wanted, drop this policy and accept that only managers can drain a mixed
-- queue.

create policy sales_select_for_push on public.sales
    for select to authenticated
    using (pos.in_store(store_id) and pos.has_perm('sale.create'));

-- ── register_sessions ─────────────────────────────────────────────────────
--
-- Same reasoning: a till's own shift rows sync with the rest of the day.

drop policy if exists register_sessions_insert on public.register_sessions;

create policy register_sessions_insert on public.register_sessions
    for insert to authenticated
    with check (pos.in_store(store_id) and pos.has_perm('sale.create'));

-- ── cash_movements ────────────────────────────────────────────────────────
--
-- This one cannot simply lose its actor check. `actor_id = auth.uid()` was
-- doing double duty: it tied the row to its author *and* it was the only
-- thing scoping the policy to a store at all — `cash.payout` alone would have
-- let a holder of that permission write a payout against any store in the
-- database. So the actor test is replaced by the store test the policy should
-- always have had, borrowed from cash_movements_select.

drop policy if exists cash_movements_insert on public.cash_movements;

create policy cash_movements_insert on public.cash_movements
    for insert to authenticated
    with check (pos.has_perm('cash.payout')
                and exists (select 1 from public.register_sessions rs
                             where rs.id = session_id
                               and pos.in_store(rs.store_id)));

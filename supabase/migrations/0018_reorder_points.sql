-- 0018_reorder_points — let somebody set the minimum.
--
-- `low_stock` (0017) selects `on_hand <= reorder_point where reorder_point > 0`.
-- The column has existed since 0001 and defaults to 0, and nothing in the
-- product could ever write it: the puller reads it, the screen displays it,
-- and the RLS tests set it with raw SQL. So the reorder list was correct,
-- shipped, and structurally incapable of returning a row.
--
-- ## Why this is not simply an UPDATE policy
--
-- `stock_levels` is the one table the architecture says clients must never
-- write. It is derived from `stock_ledger` by `apply_stock_delta`, a
-- security-definer trigger, and "write deltas, never levels" is what makes
-- two terminals selling the last unit offline reconcile to the truth rather
-- than to whichever synced last.
--
-- Supabase grants `authenticated` table-wide privileges on tables created in
-- `public` — which is why 0003 had to *revoke* on `product_prices` before
-- handing back columns. So UPDATE is almost certainly already granted here
-- and only the absent policy has been stopping it. Adding a policy alone
-- would therefore open `on_hand` to any till with `product.edit`, and the
-- reconciliation invariant would be gone in one statement, silently, because
-- nothing would error.
--
-- The revoke below is the part that does the work. A column-level privilege
-- is checked *before* RLS and before any trigger, so after this migration an
-- `UPDATE stock_levels SET on_hand = ...` is refused by Postgres at the grant
-- layer, whatever the policy says and whoever is asking.
--
-- ## Why no INSERT
--
-- Deliberately absent. A client that could insert could create a row with any
-- `on_hand` it liked and bypass the ledger entirely. The cost is that a
-- reorder point can only be set on a product that already has a level — which
-- means one that has been counted or received. That is the right order
-- anyway: a minimum for something never stocked is a number about nothing,
-- and plan §6 step 6 sequences the opening count before go-live.
--
-- `AdminService.set_reorder_point` turns the resulting empty result into a
-- sentence about counting the product in, rather than a silent no-op.

-- ── Narrow the surface ────────────────────────────────────────────────────

revoke all on public.stock_levels from authenticated;

-- Every column the table has: the puller selects all five (`store_id` for the
-- keyset, dropped before the local insert), and `low_stock` is
-- `security_invoker`, so it reads them as the caller and not as its owner.
-- Nothing is lost by the revoke above; the point is what is *not* restored.
grant select (store_id, product_id, on_hand, reorder_point, updated_at)
    on public.stock_levels to authenticated;

grant update (reorder_point) on public.stock_levels to authenticated;

-- ── Who may set it ────────────────────────────────────────────────────────

-- `product.edit` rather than `stock.count`: a reorder point is a policy about
-- a product, not an observation about the shelf. The person who decides when
-- to reorder is the person who maintains the catalogue.
create policy stock_levels_set_reorder_point on public.stock_levels
    for update to authenticated
    using      (pos.in_store(store_id) and pos.has_perm('product.edit'))
    with check (pos.in_store(store_id) and pos.has_perm('product.edit'));

-- ── Keep it sane, and keep it moving ──────────────────────────────────────

alter table public.stock_levels
    add constraint stock_levels_reorder_point_nonneg
    check (reorder_point >= 0);

-- The puller pages `stock_levels` by `updated_at`. Only `apply_stock_delta`
-- has ever advanced it, so a reorder point set through the admin screen would
-- change the cloud and never reach a till: the row's cursor would not move,
-- and the pull would step straight over it.
--
-- Stamped here rather than sent by the client, because `updated_at` is the
-- cursor. A client that could write it could set it to the past, and a keyset
-- pull would skip the row for good.
create or replace function pos.stock_level_touched()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

-- Fires for `apply_stock_delta`'s upsert too, which already sets the same
-- value — harmless, and it means there is exactly one rule about this column
-- rather than two places that must agree.
drop trigger if exists stock_levels_touch on public.stock_levels;
create trigger stock_levels_touch
    before update on public.stock_levels
    for each row execute function pos.stock_level_touched();

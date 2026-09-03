-- 0017_catalogue_editing - the writes the admin screens actually make.
--
-- 0003 was written for a till, and a till only reads the catalogue. It gave
-- `products` insert and update, `product_barcodes` insert, and
-- `product_prices` select. Slice 6 is the first client that edits, and three
-- of its four features land on a policy that is not there:
--
--   * removing a barcode is a soft delete, which is an UPDATE
--   * setting a price is an INSERT, and closing one is an UPDATE
--   * low stock compares two columns, which PostgREST cannot express
--
-- The first two would have failed the way a missing policy always fails:
-- zero rows affected, no error, a screen that looks like it worked.

-- ── Barcodes come off as well as on ───────────────────────────────────────
--
-- Soft delete only. `ux_product_barcodes_barcode` is partial on
-- `deleted_at is null`, so withdrawing a code frees it for another product
-- while the old row still explains a `sale_lines.barcode_scanned` from last
-- month. Hard deletes would take that explanation with them.

create policy product_barcodes_update on public.product_barcodes
    for update to authenticated
    using      (pos.has_perm('product.edit'))
    with check (pos.has_perm('product.edit'));

-- ── Prices, without handing over the margin ───────────────────────────────
--
-- `cost` is deliberately absent from every grant below. 0003 revoked the
-- table-wide grant and handed back each column except that one, so a client
-- that asks for it gets 42501 from Postgres before any policy runs. Slice 6
-- has no reason to widen that: it sets the shelf price, and the margin stays
-- where 0003 put it.

grant insert (product_id, store_id, price, valid_from, valid_to)
    on public.product_prices to authenticated;
grant update (price, valid_to, updated_at, deleted_at)
    on public.product_prices to authenticated;

create policy product_prices_write on public.product_prices
    for insert to authenticated
    with check (pos.in_store(store_id) and pos.has_perm('product.edit'));

create policy product_prices_update on public.product_prices
    for update to authenticated
    using      (pos.in_store(store_id) and pos.has_perm('product.edit'))
    with check (pos.in_store(store_id) and pos.has_perm('product.edit'));

-- ── Low stock is a query ──────────────────────────────────────────────────
--
-- `on_hand <= reorder_point` compares two columns, and a PostgREST filter
-- compares a column to a value. A view is the honest place for it; the
-- alternative is pulling 31,465 rows to the client and filtering there.
--
-- `security_invoker` matters: without it the view runs as its owner and
-- quietly becomes a hole around `stock_levels_select`. With it, a manager
-- sees their own store and nothing else, exactly as they would querying the
-- table.
--
-- Rooted at `stock_levels`, not at `products`. The unlisted placeholder never
-- gets a level row - slice 5 stopped writing ledger rows for anything with
-- `track_stock false` - so this shape excludes it for free. A
-- `products left join stock_levels` would have surfaced it as 0 on hand
-- against a reorder point of 0 and put "Unlisted item" at the top of the
-- reorder list. `reorder_point > 0` and `track_stock` say the same thing
-- twice on purpose.

create view public.low_stock
with (security_invoker = true) as
select sl.store_id,
       sl.product_id,
       p.sku,
       p.name,
       p.short_name,
       p.uom,
       sl.on_hand,
       sl.reorder_point,
       sl.updated_at
  from public.stock_levels sl
  join public.products p on p.id = sl.product_id
 where sl.reorder_point > 0
   and sl.on_hand <= sl.reorder_point
   and p.track_stock
   and p.is_active
   and p.deleted_at is null;

grant select on public.low_stock to authenticated;

comment on view public.low_stock is
    'Products at or under their reorder point, in the caller''s own store. '
    'A query, not a feature (phase 6 slice 6) - no alerting behind it.';

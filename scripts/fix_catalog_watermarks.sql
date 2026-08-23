-- fix_catalog_watermarks.sql — give every catalogue row its own watermark.
--
-- Run ONCE, in the SQL editor, on a project where the catalogue was imported
-- by `scripts/migrate_legacy_catalog.sql` before that script was fixed.
--
-- ── What went wrong ───────────────────────────────────────────────────────
--
-- The catalogue migration inserts every product in one transaction, so
-- `updated_at` defaults to that transaction's `now()` — one identical
-- timestamp across all ~19,000 rows.
--
-- The puller walks each table by a watermark:
--
--     GET /products?updated_at=gt.{cursor}&order=updated_at.asc&limit=1000
--
-- and stops when a page fails to move the cursor forward:
--
--     last = rows[-1][cursor_column]
--     if not last or last == cursor:
--         break        # "rare and bounded"
--
-- With every row sharing a timestamp, page one returns 1000 rows whose last
-- value *is* the cursor, so the loop breaks and the remaining 18,000 products
-- are never pulled. The terminal ends up with a 1000-product catalogue and no
-- error, and then the `product_barcodes` pull fails outright with
-- `FOREIGN KEY constraint failed`, because barcodes arrive for products the
-- terminal was never given.
--
-- The break is the right defence against an infinite loop; the bug is the
-- data that made a bounded case unbounded. This spreads the timestamps by a
-- microsecond per row so the cursor always advances. 32,000 rows spans about
-- 32 ms, which no report or reconciliation can perceive.
--
-- `product_prices` is included because its watermark is `valid_from`, not
-- `updated_at` — miss that and prices stall at 1000 while products flow.

begin;

with ordered as (
    select id, row_number() over (order by sku) as rn
    from public.products
)
update public.products p
   set updated_at = date_trunc('second', now()) + (o.rn * interval '1 microsecond')
  from ordered o
 where o.id = p.id;

with ordered as (
    select id, row_number() over (order by product_id, barcode) as rn
    from public.product_barcodes
)
update public.product_barcodes b
   set updated_at = date_trunc('second', now()) + (o.rn * interval '1 microsecond')
  from ordered o
 where o.id = b.id;

-- Both columns: `valid_from` is the watermark, `updated_at` is not, but
-- leaving it identical across every row is a trap for the next person.
with ordered as (
    select id, row_number() over (order by product_id) as rn
    from public.product_prices
)
update public.product_prices pp
   set valid_from = date_trunc('second', now()) + (o.rn * interval '1 microsecond'),
       updated_at = date_trunc('second', now()) + (o.rn * interval '1 microsecond')
  from ordered o
 where o.id = pp.id;

with ordered as (
    select id, row_number() over (order by name) as rn
    from public.categories
)
update public.categories c
   set updated_at = date_trunc('second', now()) + (o.rn * interval '1 microsecond')
  from ordered o
 where o.id = c.id;

commit;

-- Every table the terminal walks must now have as many distinct watermarks as
-- it has rows. Anything less and a page can still stall.
select 'products'         as entity, count(*) as rows,
       count(distinct updated_at) as watermarks from public.products
union all
select 'product_barcodes', count(*), count(distinct updated_at) from public.product_barcodes
union all
select 'product_prices',   count(*), count(distinct valid_from) from public.product_prices
union all
select 'categories',       count(*), count(distinct updated_at) from public.categories;

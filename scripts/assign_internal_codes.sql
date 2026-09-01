-- assign_internal_codes.sql - give every uncoded product a way in.
--
-- Run in the Supabase SQL editor, after `supabase db push` has applied 0013.
--
-- 38.6% of the pilot catalogue - 12,431 products - has no barcode at all.
-- Each one is currently unsellable: the register can find it by name, but
-- nothing can be rung up without a code. Execution plan 6 step 5 makes this
-- mandatory before go-live and says the exceptions report must be **empty**,
-- not short.
--
-- Safe to re-run. It only touches products that still have no code, so a
-- second run after importing more of the catalogue picks up the new ones and
-- leaves everything already done alone.
--
-- ## Read this before running it
--
-- A code makes a product **sellable**. It does not make it **scannable** -
-- nothing in v1 prints labels. After this runs, every product can be found
-- and rung up, and 12,431 of them still have nothing on the shelf edge for a
-- scanner to read. Two jobs follow, and they are the owner's:
--
--   * a packaged good that simply lacks a manufacturer code needs a printed
--     shelf or product label;
--   * something sold loose or by weight wants the `22…` weighed flow instead,
--     and a printed label would be wrong for it.
--
-- Budget time for that triage, and for renaming: the names are what the
-- fallback search matches on, and for a third of the shop search is the only
-- way in.

-- ── 1. How many, before anything changes ──────────────────────────────────

select count(*) as products_with_no_code
from public.products p
where p.deleted_at is null
  and not exists (
      select 1 from public.product_barcodes b
       where b.product_id = p.id and b.deleted_at is null
  );

-- ── 2. Assign ─────────────────────────────────────────────────────────────
--
-- Ordered by sku so a re-run over a grown catalogue produces a predictable
-- diff rather than churn, and so the codes read in roughly the order someone
-- would work through a shelf list.

begin;

select public.assign_internal_barcode(p.id)
from public.products p
where p.deleted_at is null
  and not exists (
      select 1 from public.product_barcodes b
       where b.product_id = p.id and b.deleted_at is null
  )
order by p.sku;

commit;

-- ── 3. The check that matters ─────────────────────────────────────────────
--
-- **Zero is the pass.** Anything left here is a product nobody can sell.

select count(*) as still_with_no_code
from public.products p
where p.deleted_at is null
  and not exists (
      select 1 from public.product_barcodes b
       where b.product_id = p.id and b.deleted_at is null
  );

-- Every code must be unique, or the till picks one of two products at random.
-- `ux_product_barcodes_barcode` enforces it; this proves it held.

select barcode, count(*) as products
from public.product_barcodes
where deleted_at is null
group by barcode
having count(*) > 1;

-- ── 4. What the shop now looks like ───────────────────────────────────────

select symbology,
       count(*) as barcodes,
       count(*) filter (where is_primary) as primary_codes
from public.product_barcodes
where deleted_at is null
group by symbology
order by barcodes desc;

-- ── 5. The worklist this leaves behind ────────────────────────────────────
--
-- Sellable, not scannable. Split by what each product actually needs, because
-- the answer differs and a printed label is wrong for half of them.

-- Sold loose or by weight: these want the `22…` weighed flow at the counter,
-- and a printed shelf label would be the wrong thing to make.
select 'weighed - use the 22 flow, do not label' as needs,
       count(*)
from public.products p
join public.product_barcodes b
  on b.product_id = p.id and b.symbology = 'INTERNAL' and b.deleted_at is null
where p.deleted_at is null and p.is_weighed
union all
-- Packaged goods with no manufacturer code: these need something printed for
-- the shelf edge before the code helps anybody at the till.
select 'packaged - needs a printed label',
       count(*)
from public.products p
join public.product_barcodes b
  on b.product_id = p.id and b.symbology = 'INTERNAL' and b.deleted_at is null
where p.deleted_at is null and not p.is_weighed;

-- The list itself, to work through.
select p.sku,
       p.name,
       p.uom,
       case when p.is_weighed then 'weighed flow' else 'print a label' end as needs,
       b.barcode as internal_code
from public.products p
join public.product_barcodes b
  on b.product_id = p.id and b.symbology = 'INTERNAL' and b.deleted_at is null
where p.deleted_at is null
order by p.is_weighed, p.name;

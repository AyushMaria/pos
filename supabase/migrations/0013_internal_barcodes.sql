-- 0013_internal_barcodes - codes for the 38.6% that have none.
--
-- 12,431 of the pilot catalogue's 32,165 products carry no manufacturer
-- barcode. A product with no code cannot be rung up, so this is not a
-- convenience: execution plan 6 step 5 makes it mandatory before go-live, and
-- says the exceptions report must be *empty* rather than merely short.
--
-- ## Why this lives in the cloud
--
-- Phase 6 decided admin is online-only: `products` and `product_barcodes` are
-- pull-only on the terminal, written by the puller and nothing else. Assigning
-- a code creates a `product_barcodes` row, so it is a catalogue write and
-- belongs on the side that owns the catalogue. The phase plan originally put
-- the counter in `terminal_state` beside the receipt sequence; that was
-- written before the pull-only nature of products was pinned down, and a
-- per-terminal counter would hand two tills the same code the first time a
-- second one existed.
--
-- A Postgres sequence is the right shape: monotonic, concurrent-safe, and
-- there is exactly one of it.
--
-- ## The format is not ours to choose
--
-- `app/domain/barcode.py` already defines it, is property-tested, and the
-- parser at the counter reads it:
--
--     21 | ten digits | check digit        (EAN-13)
--
-- The GS1 in-store range 20-29 is partitioned deliberately (architecture
-- 10.2): `21` is product identity, `22` is a weighing. This function must
-- produce exactly what `internal_barcode()` produces, or a code assigned here
-- is a code the till refuses. `scripts/check_sql_barcode_parity.py` fails if
-- the two disagree.
--
-- ## What a code does and does not do
--
-- It makes a product **sellable** - findable, ringable, countable. It does not
-- make it **scannable**: nothing in v1 prints labels (scope fence, 8). A
-- packaged good that simply lacks a manufacturer code needs a printed shelf
-- label before this helps at the counter; something sold loose by weight wants
-- the `22…` weighed flow instead. That triage is per product and is the
-- owner's, not the software's.

-- ── The check digit, permanently ──────────────────────────────────────────
--
-- `legacy.gs1_check_digit` exists but belongs to the one-time catalogue
-- import and its schema goes away with it. This one is here to stay.

create or replace function pos.gs1_check_digit(payload text)
returns int language sql immutable as $$
    -- Weights alternate 3,1 from the right. Same rule for EAN-8, UPC-A,
    -- EAN-13 and ITF-14; only the length differs.
    select (10 - sum(
                substr($1, i, 1)::int
                * case when (length($1) - i) % 2 = 0 then 3 else 1 end
           )::int % 10) % 10
    from generate_series(1, length($1)) as g(i);
$$;

-- ── The allocator ─────────────────────────────────────────────────────────

create sequence if not exists pos.internal_item_code_seq
    as bigint minvalue 1 maxvalue 9999999999 no cycle;

comment on sequence pos.internal_item_code_seq is
    'Item codes behind the 21… in-store range. Ten digits, so it runs out '
    'after ten billion products; no cycle, because reissuing a code would '
    'point an old shelf label at a new product.';

-- `security definer`, and deliberately the only privileged thing here.
--
-- `assign_internal_barcode` is `security invoker` so its INSERT is checked by
-- `product_barcodes_write` like any other catalogue write. But a caller who
-- may write the catalogue would then also need USAGE on the sequence, and
-- granting `authenticated` that means handing every cashier a counter they
-- have no business touching.
--
-- So the privilege is pushed down to the one operation that needs it. This
-- function allocates a number and can write nothing; the row it ends up in is
-- still gated by RLS. A caller without `product.edit` can burn a code and
-- gets refused on the insert - a wasted number out of ten billion, which is
-- cheaper than the alternative.
create or replace function pos.next_internal_barcode()
returns text
language sql
security definer
set search_path = pos, public
as $$
    select payload || pos.gs1_check_digit(payload)::text
    from (select '21' || lpad(nextval('pos.internal_item_code_seq')::text, 10, '0')
                 as payload) built;
$$;

-- ── Assigning one ─────────────────────────────────────────────────────────

create or replace function public.assign_internal_barcode(p_product_id uuid)
returns text
language plpgsql
security invoker
as $$
declare
    fresh   text;
    has_any boolean;
begin
    -- `security invoker`, so `product_barcodes_write` applies exactly as it
    -- would to a direct insert: the caller needs `product.create` or
    -- `product.edit`. A definer here would be a way to write the catalogue
    -- without holding either.
    select exists (
        select 1 from public.product_barcodes
         where product_id = p_product_id and deleted_at is null
    ) into has_any;

    fresh := pos.next_internal_barcode();

    insert into public.product_barcodes
        (product_id, barcode, symbology, pack_size, is_primary)
    values
        -- Primary only when the product had nothing at all. A manufacturer
        -- code that exists is the one a scanner will read; an internal code
        -- added beside it is a second way in, not a replacement.
        (p_product_id, fresh, 'INTERNAL', 1, not has_any);

    return fresh;
end;
$$;

grant execute on function pos.next_internal_barcode() to authenticated;
grant execute on function public.assign_internal_barcode(uuid) to authenticated;

comment on function public.assign_internal_barcode(uuid) is
    'Allocate and attach a 21… identity code. Makes the product sellable, not '
    'scannable - nothing prints these labels yet (execution plan 6 step 5).';

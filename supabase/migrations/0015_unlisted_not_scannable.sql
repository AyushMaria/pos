-- 0015_unlisted_not_scannable - the placeholder is not a thing on a shelf.
--
-- `scripts/assign_internal_codes.sql` walks every product with no barcode and
-- gives it a `21…` in-store code. Run against the pilot catalogue it did that
-- to `SKU-UNLISTED` too, and the exceptions report duly listed
--
--     SKU-UNLISTED | Unlisted item | each | print a label | 2100000118670
--
-- which is a shelf label nobody can stick to anything: that row stands in for
-- many different real products (0014), not for one.
--
-- The label is only the visible half. A code also makes a product
-- **scannable**. Today the placeholder has no price, so `_to_product` returns
-- null and the scan lands on the unknown-item path - noisy but harmless. The
-- day somebody adds a price to it, that same scan becomes a ringable line
-- reading "Unlisted item" at whatever the price happens to be, and the design
-- 0014 exists to protect (identity travels on the *line*) is quietly bypassed.
--
-- So: withdraw any code it already has, and make it impossible to give it
-- another. The script is patched to skip it as well, but a script is a thing
-- somebody re-runs from an old checkout, and this is the fence that holds
-- when they do.

-- ── One place for the id ───────────────────────────────────────────────────
--
-- `app/domain/unlisted.py` carries the same UUID and `tests/test_rls.py`
-- fails if the two disagree. Six copies of a literal in a script is five
-- chances to fix one of them.

create or replace function pos.unlisted_product()
returns uuid language sql immutable as $$
    select '018f0000-0000-7000-8000-0000000000ff'::uuid;
$$;

grant execute on function pos.unlisted_product() to authenticated;

comment on function pos.unlisted_product() is
    'The placeholder every unlisted sale line points at (0014). Not sellable '
    'by scan, not stock-tracked, and never given a barcode.';

-- ── Withdraw what the script already assigned ─────────────────────────────
--
-- Soft-deleted rather than deleted: a code that was live for a day may sit in
-- a `sale_lines.barcode_scanned` somewhere, and the history should still be
-- explicable. `deleted_at` is what every lookup already filters on.

update public.product_barcodes
   set deleted_at = now()
 where product_id = pos.unlisted_product()
   and deleted_at is null;

-- ── And stop it happening again ───────────────────────────────────────────

create or replace function pos.reject_unlisted_barcode()
returns trigger language plpgsql as $$
begin
    if new.product_id = pos.unlisted_product() then
        raise exception
            'the unlisted-item placeholder cannot have a barcode'
            using hint = 'It stands in for many products; see migration 0014.';
    end if;
    return new;
end;
$$;

drop trigger if exists product_barcodes_reject_unlisted on public.product_barcodes;

create trigger product_barcodes_reject_unlisted
    before insert or update of product_id on public.product_barcodes
    for each row execute function pos.reject_unlisted_barcode();

-- 0016_resolve_unknown_scans - let a manager close the queue.
--
-- `unknown_scans` has carried a `resolved` column since 0001 and gained rows
-- since 0014, but 0003 gave it two policies: insert (`sale.create`, so a
-- cashier can file one) and select (`product.read`, so a cashier generally
-- cannot read one back). There is no update policy.
--
-- With RLS on, a missing policy is not an error. `update ... set resolved =
-- true` matches no rows, reports success, and changes nothing. Slice 6 builds
-- the screen that works this queue; without this migration the button would
-- have been indistinguishable from a working one until somebody refreshed.
--
-- Gated on `product.edit` rather than the select policy's `product.read`:
-- working the queue *is* catalogue editing, and the person who closes an
-- entry is the person who created or matched the product behind it.

create policy unknown_scans_update on public.unknown_scans
    for update to authenticated
    using      (pos.in_store(store_id) and pos.has_perm('product.edit'))
    with check (pos.in_store(store_id) and pos.has_perm('product.edit'));

-- ── The scan itself is evidence ───────────────────────────────────────────
--
-- An RLS policy cannot compare the old row to the new one, so `with check`
-- above cannot stop an update that also rewrites the barcode or backdates the
-- scan. A trigger can.
--
-- This matters because the row is the only record of what the cashier
-- actually held. `sale_lines` is already immutable by construction - 0003
-- gives it insert and select and nothing else, so resolving a scan cannot
-- retroactively alter what a customer was charged. `unknown_scans` should be
-- just as honest about what was scanned, and for the same reason: both are
-- evidence about a moment, not a view of the current catalogue.

create or replace function pos.unknown_scan_is_evidence()
returns trigger language plpgsql as $$
begin
    if  new.id          is distinct from old.id
    or  new.store_id    is distinct from old.store_id
    or  new.barcode     is distinct from old.barcode
    or  new.scanned_at  is distinct from old.scanned_at
    or  new.terminal_id is distinct from old.terminal_id
    then
        raise exception
            'an unknown scan records what was scanned; only resolved may change'
            using hint =
                'To correct the catalogue, edit the product. The scan stays.';
    end if;
    return new;
end;
$$;

drop trigger if exists unknown_scans_evidence on public.unknown_scans;

create trigger unknown_scans_evidence
    before update on public.unknown_scans
    for each row execute function pos.unknown_scan_is_evidence();

comment on table public.unknown_scans is
    'Codes that matched nothing, filed by the till (0014). Append-only except '
    'for `resolved`, which a manager flips from the queue screen (0016).';

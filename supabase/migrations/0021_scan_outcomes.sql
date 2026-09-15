-- 0021_scan_outcomes — closing an entry has to mean something.
--
-- 0016 gave `unknown_scans` an update policy and slice 6 gave it a screen.
-- Each row offers three buttons: catalogue the thing, point the code at a
-- product you already have, or "Done". The first two attach the barcode and
-- then set `resolved`. The third only sets `resolved`.
--
-- The third is the one that gets pressed. In the pilot data one code,
-- 8901999000014, has five closed scans and is still not in the catalogue,
-- and `product.created` has never fired once in the life of the database.
-- The audit log cannot show this, because both paths write the same
-- `scan.resolved` row. The screen then reports, with an empty queue:
-- "Nothing waiting. Every scan found a product."
--
-- Nothing was broken. Every layer did exactly what it was asked. The queue
-- simply had no way to tell "catalogued" from "gave up", so it counted them
-- as the same thing and said the flattering one out loud.
--
-- ## What this adds
--
-- An outcome, and a check on the half of it that can be checked:
--
--   * `catalogued` — refused unless the scanned code is on a product at the
--     moment the entry closes. This is the point of the migration: the
--     silent no-op stops being discouraged and starts being impossible.
--   * `dismissed` — always allowed. A torn label, a loyalty card, a code off
--     a delivery note; the queue needs this answer and always did. It just
--     has to be said rather than implied.
--
-- ## What it does not do
--
-- It does not backfill. Rows closed before today keep a null outcome,
-- because the screen genuinely could not tell the difference then and a
-- guess written into the evidence is worse than a gap in it. It does not
-- reopen anything either: a dismissal hides no future scan, since the till
-- files a new row per scan, so the same packet arriving tomorrow queues
-- again on its own.

-- ── The column ────────────────────────────────────────────────────────────

alter table public.unknown_scans
    add column if not exists resolution text;

alter table public.unknown_scans
    add constraint unknown_scans_resolution_known
    check (resolution is null or resolution in ('catalogued', 'dismissed'));

-- The weaker direction of the pair, and deliberately so: "closed implies an
-- outcome" would be false for every row closed before this migration. The
-- trigger below enforces that half going forward, where it can look at the
-- transition rather than at the row.
alter table public.unknown_scans
    add constraint unknown_scans_open_has_no_outcome
    check (resolved or resolution is null);

comment on column public.unknown_scans.resolution is
    'How the entry was closed: catalogued (the code is on a product) or '
    'dismissed (it was never going to be one). Null while open, and null '
    'for rows closed before 0021, when the screen could not tell them apart.';

comment on table public.unknown_scans is
    'Codes that matched nothing, filed by the till (0014). Append-only except '
    'for `resolved` and `resolution`, which a manager sets together from the '
    'queue screen (0016, 0021).';

-- ── 0016's message, brought up to date ────────────────────────────────────
--
-- The evidence trigger lists what may not move and ends with a sentence
-- naming what may. That sentence is now half the truth — an outcome moves
-- with the close — and it is a sentence a shopkeeper can end up reading, so
-- it says both. The list of immutable columns is unchanged, which is the
-- part that matters: `resolution` is meant to be written exactly once.
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
            'an unknown scan records what was scanned; only how it was '
            'closed may change'
            using hint =
                'To correct the catalogue, edit the product. The scan stays.';
    end if;
    return new;
end;
$$;

-- ── The check the client cannot do ────────────────────────────────────────
--
-- Not a CHECK constraint, because the answer lives in another table, and not
-- a `with check` either: an RLS policy cannot see the old row, so it cannot
-- tell a close from an edit.
--
-- SECURITY INVOKER, like everything else on this table. The existence test
-- therefore reads `product_barcodes` as the caller, under
-- `product_barcodes_select`, which asks for `product.read` — and every role
-- holding `product.edit` holds `product.read` too (the matrix in
-- `app/domain/permissions.py`, pinned by a test there). A definer function
-- would buy nothing here and would put a third one on the public API.
create or replace function pos.unknown_scan_outcome_is_honest()
returns trigger
language plpgsql
set search_path = pos, public
as $$
begin
    if  old.resolved   is not distinct from new.resolved
    and old.resolution is not distinct from new.resolution
    then
        return new;                 -- nothing about the close is changing
    end if;

    if not new.resolved then
        -- Reopening drops the outcome with it. A row that is open again has
        -- not been catalogued or dismissed; it is waiting, like any other.
        if old.resolved then
            new.resolution := null;
            return new;
        end if;

        -- Hanging an outcome on an entry that is still open. The check
        -- constraint refuses this too; the difference is that a person reads
        -- this one.
        raise exception 'an entry that is still open has no outcome'
            using errcode = '23514',
                  hint = 'Close it as catalogued or dismissed, or leave it.';
    end if;

    if new.resolution is null then
        raise exception 'closing a scan has to say how'
            using errcode = '23514',
                  hint = 'catalogued, if the code is now on a product; '
                         'dismissed, if it is never going to be.';
    end if;

    if new.resolution = 'catalogued'
       and not exists (
           select 1
             from public.product_barcodes
            where barcode = new.barcode
              and deleted_at is null
       )
    then
        -- `AdminService._close_scan` matches on this sentence to turn it into
        -- a sentence with a next action in it. Change the words here and the
        -- screen falls back to showing the raw PostgREST detail — worse, not
        -- broken, but change both.
        raise exception '% is not on any product yet', new.barcode
            using errcode = '23514',
                  hint = 'Attach the code to a product first, or dismiss the '
                         'scan if it is never going to be one.';
    end if;

    return new;
end;
$$;

-- Fires after `unknown_scans_evidence` (0016), which is what the name buys:
-- an update that both rewrites the barcode and closes the entry should be
-- refused for rewriting the barcode, which is the worse of the two.
drop trigger if exists unknown_scans_outcome on public.unknown_scans;
create trigger unknown_scans_outcome
    before update on public.unknown_scans
    for each row execute function pos.unknown_scan_outcome_is_honest();

-- ── Two closes, two audit rows ────────────────────────────────────────────
--
-- 0019 audited every update to this table as `scan.resolved`, which was the
-- best it could do when the row held nothing to distinguish them. The
-- catalogued case keeps that action name so the rows already in the log stay
-- comparable with the ones written from now on.
--
-- The `when` clauses follow `stock_levels_audit_reorder_point`: fire on the
-- transition, not on the state, so an unrelated update to a closed row does
-- not log the close a second time.

drop trigger if exists unknown_scans_audit_update on public.unknown_scans;

drop trigger if exists unknown_scans_audit_catalogued on public.unknown_scans;
create trigger unknown_scans_audit_catalogued
    after update on public.unknown_scans
    for each row
    when (old.resolution is distinct from new.resolution
          and new.resolution = 'catalogued')
    execute function pos.write_audit('scan.resolved');

drop trigger if exists unknown_scans_audit_dismissed on public.unknown_scans;
create trigger unknown_scans_audit_dismissed
    after update on public.unknown_scans
    for each row
    when (old.resolution is distinct from new.resolution
          and new.resolution = 'dismissed')
    execute function pos.write_audit('scan.dismissed');

-- Nothing in the product reopens an entry today. If something ever does, the
-- log should not be the last place to find out.
drop trigger if exists unknown_scans_audit_reopened on public.unknown_scans;
create trigger unknown_scans_audit_reopened
    after update on public.unknown_scans
    for each row
    when (old.resolved and not new.resolved)
    execute function pos.write_audit('scan.reopened');

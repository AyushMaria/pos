-- 0019_audit_admin_edits — a trail for the one write path that had none.
--
-- Five admin mutations changed the catalogue and left nothing behind: create
-- a product, edit one, set a price, withdraw a barcode, resolve a scan. Every
-- sync path writes `audit_log` rows — 0008, 0010, 0011 and 0014 all do — so
-- the admin screen was the only way to change this shop's data invisibly.
--
-- The execution plan's definition of done says "audit rows exist for anything
-- a manager would need to investigate later". A price change is the example
-- that line exists for.
--
-- ## Why a trigger and not Python
--
-- The admin screen reaches PostgREST directly, carrying the signed-in user's
-- own token (phase 6 decision 2). An audit written by `admin_service.py`
-- would therefore be exactly as honest as the client: anyone with the same
-- token and `curl` writes the row and skips the log. A trigger sits on the
-- table, so the audit is a property of the data changing rather than of the
-- code that asked.
--
-- Deliberately SECURITY INVOKER. The trigger inserts as the caller and must
-- satisfy `audit_log_insert` like anybody else — if that policy would refuse
-- the row, the edit fails rather than succeeding unrecorded. Failing closed
-- is the right direction for an audit.

-- ── Which store did this come from? ───────────────────────────────────────

-- `products` and `product_barcodes` are a global catalogue with no store
-- column, but a person edited them while signed in somewhere, and that is
-- the useful fact. Returns null for a user who holds more than one store,
-- because guessing between them would be worse than saying nothing.
create or replace function pos.acting_store()
returns uuid
language sql
stable
set search_path = pos, public
as $$
  select case
    when jsonb_typeof(auth.jwt() -> 'app_metadata' -> 'store_ids') = 'array'
     and jsonb_array_length(auth.jwt() -> 'app_metadata' -> 'store_ids') = 1
    then ((auth.jwt() -> 'app_metadata' -> 'store_ids') ->> 0)::uuid
  end;
$$;

grant execute on function pos.acting_store() to authenticated;

-- ── A store-less audit row has to be readable ─────────────────────────────

-- `audit_log_insert` already allows `store_id is null` — a catalogue edit
-- belongs to no store. `audit_log_select` did not, and `pos.in_store(null)`
-- is false, so those rows would have been written and then invisible to
-- everyone, forever. An audit nobody can read is not an audit.
--
-- The two policies now mirror each other: you may read the shape of row you
-- are allowed to write, if you may read the log at all.
drop policy if exists audit_log_select on public.audit_log;
create policy audit_log_select on public.audit_log
    for select to authenticated
    using (
        pos.has_perm('user.manage')
        and (store_id is null or pos.in_store(store_id))
    );

-- ── The trigger ───────────────────────────────────────────────────────────

-- One function for every audited table. `tg_argv[0]` is the action; the
-- optional `tg_argv[1]` names the soft-delete case, because "withdrew a
-- barcode" and "changed a barcode" are the same UPDATE to Postgres and
-- different events to a person reading the log.
create or replace function pos.write_audit()
returns trigger
language plpgsql
set search_path = pos, public
as $$
declare
    before_doc jsonb := case when tg_op = 'INSERT' then null else to_jsonb(old) end;
    after_doc  jsonb := case when tg_op = 'DELETE' then null else to_jsonb(new) end;
    action     text  := tg_argv[0];
    row_id     uuid;
    store      uuid;
begin
    -- 0003 revoked `product_prices.cost` from `authenticated` and 0005 built
    -- a security-definer view as the single legitimate route to it. The audit
    -- trail must not become a second one.
    before_doc := before_doc - 'cost';
    after_doc  := after_doc  - 'cost';

    -- `stock_levels` is keyed on (store_id, product_id) and has no `id`,
    -- so the product is the thing the row is about.
    row_id := coalesce(
        (after_doc  ->> 'id')::uuid,         (before_doc ->> 'id')::uuid,
        (after_doc  ->> 'product_id')::uuid, (before_doc ->> 'product_id')::uuid
    );

    -- The row's own store when it has one, otherwise where the editor was.
    store := coalesce(
        (after_doc  ->> 'store_id')::uuid,
        (before_doc ->> 'store_id')::uuid,
        pos.acting_store()
    );

    -- Every audited table carries `deleted_at`, so this needs no per-table
    -- knowledge: a row that gains one was withdrawn rather than edited.
    if tg_argv[1] is not null
       and before_doc ->> 'deleted_at' is null
       and after_doc  ->> 'deleted_at' is not null
    then
        action := tg_argv[1];
    end if;

    insert into public.audit_log (
        id, store_id, actor_id, action, entity, entity_id,
        before_json, after_json, occurred_at
    )
    values (
        gen_random_uuid(), store, auth.uid(), action, tg_table_name, row_id,
        before_doc, after_doc, now()
    );

    return null;  -- AFTER trigger; the return value is discarded.
end;
$$;

-- ── Where it fires ────────────────────────────────────────────────────────

drop trigger if exists products_audit_insert on public.products;
create trigger products_audit_insert
    after insert on public.products
    for each row execute function pos.write_audit('product.created');

drop trigger if exists products_audit_update on public.products;
create trigger products_audit_update
    after update on public.products
    for each row execute function pos.write_audit('product.updated', 'product.deleted');

drop trigger if exists product_barcodes_audit_insert on public.product_barcodes;
create trigger product_barcodes_audit_insert
    after insert on public.product_barcodes
    for each row execute function pos.write_audit('barcode.added');

drop trigger if exists product_barcodes_audit_update on public.product_barcodes;
create trigger product_barcodes_audit_update
    after update on public.product_barcodes
    for each row execute function pos.write_audit('barcode.updated', 'barcode.withdrawn');

drop trigger if exists product_prices_audit_insert on public.product_prices;
create trigger product_prices_audit_insert
    after insert on public.product_prices
    for each row execute function pos.write_audit('price.opened');

-- `set_price` opens the new row before closing the old one, so a single
-- price change writes two rows: one `price.opened`, one `price.closed`.
-- That is the shape of the operation and the log should show it.
drop trigger if exists product_prices_audit_update on public.product_prices;
create trigger product_prices_audit_update
    after update on public.product_prices
    for each row execute function pos.write_audit('price.closed', 'price.deleted');

-- Insert is not audited here: a scan arrives through `sync_push`, which
-- writes its own audit row already (0014). Only the resolve is new.
drop trigger if exists unknown_scans_audit_update on public.unknown_scans;
create trigger unknown_scans_audit_update
    after update on public.unknown_scans
    for each row execute function pos.write_audit('scan.resolved');

-- A reorder point is a decision, not a movement, and nothing else records it.
--
-- The `when` clause is what makes this safe: `apply_stock_delta` upserts
-- `on_hand` and `updated_at` on every single ledger row, so an unguarded
-- trigger here would write an audit row per sale and drown the log in the
-- one thing the ledger already says better.
drop trigger if exists stock_levels_audit_reorder_point on public.stock_levels;
create trigger stock_levels_audit_reorder_point
    after update of reorder_point on public.stock_levels
    for each row
    when (old.reorder_point is distinct from new.reorder_point)
    execute function pos.write_audit('stock.reorder_point_set');

-- ── What is deliberately not audited ──────────────────────────────────────
--
-- Stock movements. `stock_ledger` is already an append-only record of every
-- delta with its reason, document and terminal, and `sync_push` writes an
-- audit row for the ones that arrive from a till. Auditing `stock_levels.
-- on_hand` as well would log the same event twice and make
-- `reconcile_stock.sql` harder to read rather than easier — which is why the
-- trigger above is scoped to `reorder_point` alone.

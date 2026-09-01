-- 0014_unlisted_and_unknown_scans - selling something the till has never seen.
--
-- A customer is at the counter with an item nothing recognises. The basket is
-- open, there is a queue, and "wait while I add this to the system properly"
-- is not an answer. Offline it is not even an available answer: phase 6
-- decided admin is online-only.
--
-- ## Why the sale line does not point at a new product
--
-- The obvious design - invent a local product row and reference it - cannot
-- work, and the schema says so plainly:
--
--     product_id uuid not null references public.products(id)
--
-- A sale line pointing at a product the cloud has never heard of fails that
-- foreign key, so the whole sale quarantines. The cashier's sale would be
-- taken, printed, paid for, and then silently refused by the server, which is
-- the worst of every available outcome. Making it work would mean pushing
-- products as a sync entity, with terminal-generated ids and a puller that
-- stops overwriting unpushed local rows: the two-way catalogue that decision 2
-- deferred.
--
-- ## What it points at instead
--
-- One placeholder product that every unlisted line references. The item's real
-- identity travels on the line itself, in columns that already exist for
-- exactly this:
--
--     sale_lines.description       what the cashier typed
--     sale_lines.barcode_scanned   the code that matched nothing
--
-- The sale is complete and correct in money terms - the right total, the right
-- GST, a receipt that reads properly - and it syncs like any other sale. The
-- catalogue work happens afterwards, from `unknown_scans`, by somebody who is
-- not standing in front of a customer.
--
-- What is given up: stock is not tracked for that item. It cannot be. The item
-- has no product row, so it has no stock row and there is nothing to
-- decrement. When a manager creates the real product it will need an opening
-- count, which is a stock-take either way.
--
-- ## The id is fixed on purpose
--
-- `app/domain/unlisted.py` carries the same UUID and a test fails if the two
-- disagree. A terminal guessing a different id would write sale lines that
-- fail the very foreign key this design exists to avoid.

-- ── The placeholder ───────────────────────────────────────────────────────

-- The rate has to exist before a product can reference it, and migrations run
-- before `seed.sql` does. On a shop already seeded this changes nothing; on a
-- fresh project it is the difference between `db push` working and failing on
-- `products_tax_code_fkey`. Found by the RLS suite, which builds a database
-- from migrations and seed in that order.
insert into public.tax_codes (code, name, rate_bp, is_inclusive)
values ('GST18', 'GST 18%', 1800, true)
on conflict (code) do nothing;

-- GST18 rather than zero-rated. The tax code that matters is the one on the
-- *line*, chosen by the cashier; this is only a fallback. Zero would be the
-- dangerous default - it understates tax owed, and nobody notices until a
-- return is filed.
insert into public.products
    (id, sku, name, short_name, uom, is_weighed, track_stock, tax_code, is_active)
values (
    '018f0000-0000-7000-8000-0000000000ff',
    'SKU-UNLISTED',
    'Unlisted item',
    'Unlisted item',
    'each',
    false,
    -- Nothing to track. This row stands in for many unrelated real products,
    -- so a stock level on it would be the sum of things that have nothing to
    -- do with each other.
    false,
    'GST18',
    true
)
on conflict (id) do nothing;

comment on table public.unknown_scans is
    'Codes that matched nothing at the counter. Written by the till when a '
    'lookup misses and when a cashier sells the item anyway; worked through '
    'later by somebody holding product.edit.';

-- ── sync_push learns a third entity ───────────────────────────────────────
--
-- A separate RPC would never be called: the pusher sends mixed batches to
-- `sync_push` and nothing else. The rest of the function is 0011 verbatim.

create or replace function public.sync_push(items jsonb)
returns jsonb
language plpgsql
security invoker
as $$
declare
    item        jsonb;
    record_data jsonb;
    child       jsonb;
    accepted    int := 0;
    version     int;
begin
    for item in select * from jsonb_array_elements(items)
    loop
        version := (item ->> 'schema_version')::int;
        if version is null or version <> 3 then
            -- Refuse the batch rather than the row. A terminal on the wrong
            -- schema will produce nothing but wrong payloads, so accepting
            -- the ones that happen to fit would scatter half a day's sales
            -- across two shapes.
            raise exception 'outdated_terminal: schema_version % is not supported', version
                using hint = 'update the terminal';
        end if;

        record_data := item -> 'data';

        case item ->> 'entity'
        when 'sale' then
            insert into public.sales (
                id, store_id, terminal_id, session_id, receipt_no, cashier_id,
                type, status, subtotal, discount_total, tax_total,
                rounding_adjustment, grand_total, original_sale_id,
                client_created_at
            )
            select
                (record_data ->> 'id')::uuid,
                (record_data ->> 'store_id')::uuid,
                (record_data ->> 'terminal_id')::uuid,
                nullif(record_data ->> 'session_id', '')::uuid,
                record_data ->> 'receipt_no',
                (record_data ->> 'cashier_id')::uuid,
                coalesce(record_data ->> 'type', 'sale'),
                record_data ->> 'status',
                (record_data ->> 'subtotal')::int,
                (record_data ->> 'discount_total')::int,
                (record_data ->> 'tax_total')::int,
                (record_data ->> 'rounding_adjustment')::int,
                (record_data ->> 'grand_total')::int,
                nullif(record_data ->> 'original_sale_id', '')::uuid,
                (record_data ->> 'client_created_at')::timestamptz
            on conflict (id) do nothing;

            for child in select * from jsonb_array_elements(coalesce(record_data -> 'lines', '[]'::jsonb))
            loop
                insert into public.sale_lines (
                    id, sale_id, line_no, product_id, barcode_scanned, description,
                    qty_milli, unit_price, discount_amount, tax_amount, line_total,
                    tax_code, tax_rate_bp, overridden_by, override_reason
                )
                select
                    (child ->> 'id')::uuid,
                    (child ->> 'sale_id')::uuid,
                    (child ->> 'line_no')::int,
                    (child ->> 'product_id')::uuid,
                    child ->> 'barcode_scanned',
                    child ->> 'description',
                    (child ->> 'qty_milli')::int,
                    (child ->> 'unit_price')::int,
                    (child ->> 'discount_amount')::int,
                    (child ->> 'tax_amount')::int,
                    (child ->> 'line_total')::int,
                    child ->> 'tax_code',
                    coalesce((child ->> 'tax_rate_bp')::int, 0),
                    nullif(child ->> 'overridden_by', '')::uuid,
                    child ->> 'override_reason'
                on conflict (id) do nothing;
            end loop;

            for child in select * from jsonb_array_elements(coalesce(record_data -> 'attempts', '[]'::jsonb))
            loop
                insert into public.payment_attempts (
                    id, sale_id, method, amount, state, txn_ref, qr_payload,
                    started_at, expires_at, resolved_at, failure_reason
                )
                select
                    (child ->> 'id')::uuid,
                    (child ->> 'sale_id')::uuid,
                    child ->> 'method',
                    (child ->> 'amount')::int,
                    child ->> 'state',
                    child ->> 'txn_ref',
                    child ->> 'qr_payload',
                    (child ->> 'started_at')::timestamptz,
                    nullif(child ->> 'expires_at', '')::timestamptz,
                    nullif(child ->> 'resolved_at', '')::timestamptz,
                    child ->> 'failure_reason'
                on conflict (id) do nothing;
            end loop;

            for child in select * from jsonb_array_elements(coalesce(record_data -> 'payments', '[]'::jsonb))
            loop
                insert into public.payments (
                    id, sale_id, attempt_id, method, amount, status, tender_ref,
                    utr, confirmation_method, confirmed_by, verified
                )
                select
                    (child ->> 'id')::uuid,
                    (child ->> 'sale_id')::uuid,
                    nullif(child ->> 'attempt_id', '')::uuid,
                    child ->> 'method',
                    (child ->> 'amount')::int,
                    child ->> 'status',
                    child ->> 'tender_ref',
                    child ->> 'utr',
                    child ->> 'confirmation_method',
                    nullif(child ->> 'confirmed_by', '')::uuid,
                    -- Never trust a terminal's word that money settled. UPI
                    -- attested at the counter stays unverified until
                    -- settlement says otherwise (§13.5).
                    false
                on conflict (id) do nothing;
            end loop;

            for child in select * from jsonb_array_elements(coalesce(record_data -> 'stock_ledger', '[]'::jsonb))
            loop
                insert into public.stock_ledger (
                    id, store_id, product_id, delta_milli, reason, ref_type,
                    ref_id, occurred_at, terminal_id, user_id
                )
                select
                    (child ->> 'id')::uuid,
                    (child ->> 'store_id')::uuid,
                    (child ->> 'product_id')::uuid,
                    (child ->> 'delta_milli')::int,
                    child ->> 'reason',
                    child ->> 'ref_type',
                    nullif(child ->> 'ref_id', '')::uuid,
                    (child ->> 'occurred_at')::timestamptz,
                    nullif(child ->> 'terminal_id', '')::uuid,
                    nullif(child ->> 'user_id', '')::uuid
                on conflict (id) do nothing;
            end loop;

            for child in select * from jsonb_array_elements(coalesce(record_data -> 'audit', '[]'::jsonb))
            loop
                -- A plain insert with the duplicate caught, rather than
                -- `on conflict (id) do nothing`.
                --
                -- `on conflict` makes Postgres check the arbiter index, and
                -- that check is subject to the table's SELECT policy.
                -- `audit_log_select` is `in_store and user.manage`, so a
                -- cashier pushing a sale that carries any audit row was
                -- refused — not for writing the row, which their INSERT
                -- policy allows, but for the conflict check reading it.
                --
                -- The alternative was a permissive SELECT policy, as 0009
                -- needed for `sales`. Not here: 0003 says reading the audit
                -- log is a manager's privilege, and a re-push should not be
                -- the reason a cashier can read who overrode what. Catching
                -- `unique_violation` keeps the re-send a no-op without
                -- granting anyone a read.
                begin
                    insert into public.audit_log (
                        id, store_id, actor_id, approver_id, action, entity,
                        entity_id, before_json, after_json, occurred_at
                    )
                    select
                        (child ->> 'id')::uuid,
                        (record_data ->> 'store_id')::uuid,
                        nullif(child ->> 'actor_id', '')::uuid,
                        nullif(child ->> 'approver_id', '')::uuid,
                        child ->> 'action',
                        child ->> 'entity',
                        -- audit_log.entity_id is uuid. Every other uuid in
                        -- this function is cast; this one was not, so any
                        -- sale carrying an audit row failed the whole push.
                        nullif(child ->> 'entity_id', '')::uuid,
                        (child ->> 'before_json')::jsonb,
                        (child ->> 'after_json')::jsonb,
                        (child ->> 'occurred_at')::timestamptz;
                exception when unique_violation then
                    -- Already here from an earlier send. Same meaning as
                    -- `do nothing`, reached without a read.
                    null;
                end;
            end loop;

        when 'stock_movement' then
            -- A stock movement with no sale behind it: a goods receipt, a
            -- count, an adjustment.
            --
            -- Until this branch existed the only way a ledger row reached the
            -- cloud was nested inside a sale envelope, because that was the
            -- only thing that produced one. A receipt would have sat in the
            -- outbox and come back as `unknown entity` — the queue blocked by
            -- a payload nothing on this side knew how to accept.
            --
            -- `stock_levels` is not touched here. `stock_ledger_apply` from
            -- 0001 maintains it from every delta, and adding a second path
            -- would double every movement, which is exactly the bug 0008
            -- shipped and had to remove.
            insert into public.stock_ledger (
                id, store_id, product_id, delta_milli, reason, ref_type,
                ref_id, occurred_at, terminal_id, user_id
            )
            select
                (record_data ->> 'id')::uuid,
                (record_data ->> 'store_id')::uuid,
                (record_data ->> 'product_id')::uuid,
                (record_data ->> 'delta_milli')::int,
                record_data ->> 'reason',
                record_data ->> 'ref_type',
                nullif(record_data ->> 'ref_id', '')::uuid,
                (record_data ->> 'occurred_at')::timestamptz,
                nullif(record_data ->> 'terminal_id', '')::uuid,
                nullif(record_data ->> 'user_id', '')::uuid
            on conflict (id) do nothing;

            -- Why the stock moved, when a person had to say. An adjustment is
            -- the only movement with no document behind it, so its reason is
            -- the audit trail. Same unique_violation guard as the sale branch,
            -- for the same reason: `audit_log_select` is manager-only, and a
            -- re-push should not be why a cashier can read who adjusted what.
            for child in select * from jsonb_array_elements(coalesce(record_data -> 'audit', '[]'::jsonb))
            loop
                begin
                    insert into public.audit_log (
                        id, store_id, actor_id, approver_id, action, entity,
                        entity_id, before_json, after_json, occurred_at
                    )
                    select
                        (child ->> 'id')::uuid,
                        (record_data ->> 'store_id')::uuid,
                        nullif(child ->> 'actor_id', '')::uuid,
                        nullif(child ->> 'approver_id', '')::uuid,
                        child ->> 'action',
                        child ->> 'entity',
                        nullif(child ->> 'entity_id', '')::uuid,
                        (child ->> 'before_json')::jsonb,
                        (child ->> 'after_json')::jsonb,
                        (child ->> 'occurred_at')::timestamptz;
                exception when unique_violation then
                    null;
                end;
            end loop;

        when 'sale_review' then
            insert into public.sale_reviews (
                id, sale_id, outcome, note, resolved_by, resolved_at
            )
            select
                (record_data ->> 'id')::uuid,
                (record_data ->> 'sale_id')::uuid,
                record_data ->> 'outcome',
                record_data ->> 'note',
                (record_data ->> 'resolved_by')::uuid,
                (record_data ->> 'resolved_at')::timestamptz
            on conflict (id) do nothing;

        when 'unknown_scan' then
            -- A code that matched nothing at the counter.
            --
            -- The queue is only useful if it outlives the terminal. A shop
            -- that loses a machine should not also lose the list of things it
            -- could not identify, and the person who resolves them is not
            -- necessarily standing at the till.
            --
            -- `unknown_scans_insert` already accepts a caller with
            -- `sale.create`, because the cashier who met the unknown item is
            -- the one who records it. No policy changes.
            insert into public.unknown_scans
                (id, store_id, barcode, scanned_at, terminal_id, resolved)
            select
                (record_data ->> 'id')::uuid,
                (record_data ->> 'store_id')::uuid,
                record_data ->> 'barcode',
                (record_data ->> 'scanned_at')::timestamptz,
                nullif(record_data ->> 'terminal_id', '')::uuid,
                coalesce((record_data ->> 'resolved')::boolean, false)
            on conflict (id) do nothing;

        else
            raise exception 'unknown entity %', item ->> 'entity';
        end case;

        accepted := accepted + 1;
    end loop;

    return jsonb_build_object('accepted', accepted);
end;
$$;

grant execute on function public.sync_push(jsonb) to authenticated;

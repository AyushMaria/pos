-- 0010_sync_push_entity_id_cast — one missing cast in the audit branch.
--
-- `public.audit_log.entity_id` is `uuid`. 0008 inserted it as
-- `child ->> 'entity_id'`, which is text, and Postgres will not coerce text to
-- uuid in an INSERT ... SELECT. Every other uuid in that function is cast; this
-- one was missed.
--
-- The blast radius is larger than a single column suggests. `sync_push` is one
-- transaction, so a sale that carries any audit entry — a void, a price
-- override, a supervisor approval — took its lines, payments and stock
-- movements down with it. The terminal saw `HTTP 400: column "entity_id" is of
-- type uuid but expression is of type text`, classified 400 as permanent, and
-- quarantined the sale. Correct handling of a bug that should not have existed.
--
-- Found on a real till, on the first sale that produced an audit row. The
-- automated push tests never sent one: the sale envelope in tests/test_rls.py
-- carries lines, payments and stock movements, and an empty `audit`. A branch
-- with no test is a branch with no type checking either, because plpgsql only
-- resolves these types when the statement first runs.
--
-- The function is otherwise 0008 verbatim.

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

        else
            raise exception 'unknown entity %', item ->> 'entity';
        end case;

        accepted := accepted + 1;
    end loop;

    return jsonb_build_object('accepted', accepted);
end;
$$;

grant execute on function public.sync_push(jsonb) to authenticated;

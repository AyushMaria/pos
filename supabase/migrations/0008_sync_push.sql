-- 0008_sync_push — the terminal's one write endpoint (architecture §9.2).
--
-- Everything a till pushes arrives here, as a batch of versioned envelopes.
-- Three properties matter, and each one exists to prevent a specific way a
-- shop loses money or trust:
--
--   * **Idempotent.** Every insert is `on conflict (id) do nothing` against a
--     client-generated UUID v7 primary key. A dropped acknowledgement makes
--     the terminal re-send, and a re-send must be a no-op — not a second sale
--     on the day's takings.
--
--   * **Not privileged.** `security invoker`, so every insert is still
--     checked by the same RLS policies that would apply to a direct write.
--     A `security definer` RPC here would quietly become a hole straight
--     through the only real trust boundary in the system (§1.3).
--
--   * **Version-checked.** A terminal that has been offline for a fortnight
--     may be behind this schema. It is told so plainly, once, instead of
--     having its sales quarantined with a message about a missing column
--     (§17).
--
-- The whole batch is one transaction — a function body always is — so a sale
-- and its payments arrive together or not at all.

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
                    child ->> 'entity_id',
                    (child ->> 'before_json')::jsonb,
                    (child ->> 'after_json')::jsonb,
                    (child ->> 'occurred_at')::timestamptz
                on conflict (id) do nothing;
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

-- ── Stock levels ──────────────────────────────────────────────────────────
--
-- Nothing here. `stock_ledger_apply` in 0001_core already maintains
-- `stock_levels` from every delta, and this migration originally added a
-- second trigger over the same function — which applied every sale to stock
-- twice. Caught by the test that pushes one sale and checks the level moved
-- by one unit rather than two.
--
-- Left as a comment rather than deleted because the phase 5 plan lists
-- "server-side stock_levels trigger" as work, and the answer is that phase 1
-- already did it.

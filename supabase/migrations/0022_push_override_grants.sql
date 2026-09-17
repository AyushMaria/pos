-- 0022 — push a supervisor override to the cloud.
--
-- Phase 7 slice 3. `AuditRepository.record_override` writes a row naming both
-- the cashier and the supervisor, and until now nothing could carry it off the
-- terminal: `sync_push` accepted `sale`, `stock_movement`, `sale_review` and
-- `unknown_scan`, and audit rows otherwise travel attached to a parent entity.
--
-- A grant has no parent. So the row sat in the terminal's `audit_log` with
-- nothing queued against it — not delayed, *unrecorded*, because the outbox
-- is the only thing that remembers what still has to go. A later upgrade
-- would have found nothing to send, and the overrides taken during whatever
-- incident prompted someone to look would have been precisely the ones lost.
--
-- The whole function is replaced because that is how it has always been
-- amended here (0008, 0010, 0011, 0014). The body below is 0014's, copied
-- mechanically, with one branch added.

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

        when 'override' then
            -- A supervisor lending a cashier a permission for ninety seconds.
            --
            -- The only entity here that is an audit row rather than
            -- something an audit row describes. Every other branch pushes a
            -- business record and carries its audit trail alongside; a grant
            -- has no business record, because it is minted when the supervisor
            -- authorises and may be spent on nothing at all. A grant nobody
            -- used is still a fact about the shop, and "a supervisor was
            -- called to this till eleven times today" is the shape somebody
            -- eventually asks for.
            --
            -- Two columns called `entity_id` meet here and mean different
            -- things. The terminal's outbox pointer holds the `audit_log.id`,
            -- because the audit row is itself the thing being pushed. The
            -- `audit_log.entity_id` below stays null: it is a `uuid`, and what
            -- this row is about is a permission key, which has no uuid
            -- identity. The key lives in `after_json` and `entity` says what
            -- kind of thing it names.
            --
            -- The cast is written anyway. 0010 exists entirely because it was
            -- once missing from this exact column, and plpgsql resolves these
            -- types only when the statement first runs — so an untested branch
            -- is an unchecked one.
            --
            -- `audit_log_insert` already accepts any authenticated caller
            -- writing in their own store, so the cashier's own claim carries
            -- this row. No policy changes.
            -- `on conflict` is not available here, and that is not a style
            -- choice. Detecting a conflict means reading the existing row,
            -- and `audit_log_select` asks for `user.manage` — which the
            -- cashier pushing this does not have. The insert is refused as an
            -- RLS violation before the conflict is ever considered.
            --
            -- The two audit inserts in the sale branch above already handle it
            -- this way, in the same words: same meaning as `do nothing`,
            -- reached without a read. A retry after a dropped connection must
            -- not turn one authorisation into two.
            begin
                insert into public.audit_log (
                    id, store_id, actor_id, approver_id, action, entity,
                    entity_id, after_json, occurred_at
                )
                select
                    (record_data ->> 'id')::uuid,
                    nullif(record_data ->> 'store_id', '')::uuid,
                    nullif(record_data ->> 'actor_id', '')::uuid,
                    nullif(record_data ->> 'approver_id', '')::uuid,
                    record_data ->> 'action',
                    record_data ->> 'entity',
                    nullif(record_data ->> 'entity_id', '')::uuid,
                    (record_data ->> 'after_json')::jsonb,
                    (record_data ->> 'occurred_at')::timestamptz;
            exception when unique_violation then
                null;
            end;

        else
            raise exception 'unknown entity %', item ->> 'entity';
        end case;

        accepted := accepted + 1;
    end loop;

    return jsonb_build_object('accepted', accepted);
end;
$$;

grant execute on function public.sync_push(jsonb) to authenticated;

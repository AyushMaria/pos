-- 0025 — the day-close check (phase 8 slice 3, second half).
--
-- A close carries the figures the till worked out at ten o'clock. This asks
-- the cloud to work them out again from its own rows — `sales`, `payments`,
-- `sale_reviews`, `cash_movements` — and returns the two side by side. When
-- they agree, the till and the cloud saw the same day. When they differ, the
-- terminal explains why from its own outbox (a sale still waiting to push,
-- or one in the failures queue); this function only says *that* they differ.
--
-- **On demand, not a trigger.** The plan sketched a trigger writing to a
-- `shift_close_checks` table on close insert. That records the one moment the
-- answer is most likely to be wrong: the outbox is ordered, but a sale that
-- was quarantined and retried tomorrow arrives after the close, and a stored
-- check would say "one missing" for ever. Computed when asked, the check
-- converges as the late rows land, and there is no second table to keep in
-- step with the first.
--
-- **SECURITY INVOKER.** The caller's own RLS applies: `shift_closes` is read-
-- gated on `report.sales.store`, so anyone without that key gets no rows —
-- the same answer as a close that has not arrived, which is what it is from
-- where they stand. No secret is needed, so this is SQL rather than an Edge
-- Function, and PostgREST exposes it as `rpc/day_close_check`.
--
-- The arithmetic mirrors `app/domain/shift.py::figures` rule for rule:
--   * a sale counts if `completed`, or `requires_review` resolved `paid`;
--   * a review counts only if resolved by the time the shift closed — a
--     review resolved tomorrow changes tomorrow, not tonight (decision 1);
--   * only `approved` payments; UPI splits on `verified`;
--   * rounding is the sum of `rounding_adjustment` over counting sales;
--   * under review = `requires_review` with no review by close time.

create or replace function public.day_close_check(p_session_id uuid)
returns table (figure text, till bigint, cloud bigint)
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
    with c as (
        select * from public.shift_closes where session_id = p_session_id
    ),
    s as (
        select sa.id, sa.status, sa.grand_total, sa.rounding_adjustment,
               (select r.outcome from public.sale_reviews r, c
                 where r.sale_id = sa.id and r.resolved_at <= c.closed_at) as outcome
          from public.sales sa
         where sa.session_id = p_session_id
    ),
    counting as (
        select * from s
         where status = 'completed' or (status = 'requires_review' and outcome = 'paid')
    ),
    p as (
        select p.method, p.verified, p.amount
          from public.payments p join counting k on k.id = p.sale_id
         where p.status = 'approved'
    ),
    m as (
        select direction, amount from public.cash_movements where session_id = p_session_id
    ),
    cloud as (
        select
          (select coalesce(sum(amount), 0) from p where method = 'cash')                    as cash_sales,
          (select coalesce(sum(amount), 0) from p where method = 'upi' and not verified)    as upi_attested,
          (select coalesce(sum(amount), 0) from p where method = 'upi' and verified)        as upi_verified,
          (select coalesce(sum(amount), 0) from m where direction = 'in')                   as cash_in,
          (select coalesce(sum(amount), 0) from m where direction = 'out')                  as cash_out,
          (select coalesce(sum(rounding_adjustment), 0) from counting)                      as rounding,
          (select count(*) from counting)                                                   as sales_count,
          (select count(*) from s where status = 'requires_review' and outcome is null)     as under_review_count,
          (select coalesce(sum(grand_total), 0) from s
            where status = 'requires_review' and outcome is null)                           as under_review_total
    )
    select v.figure, v.till, v.cloud
      from c, cloud,
           lateral (values
             (1, 'sales_count',        c.sales_count::bigint,        cloud.sales_count::bigint),
             (2, 'cash_sales',         c.cash_sales::bigint,         cloud.cash_sales::bigint),
             (3, 'upi_attested',       c.upi_attested::bigint,       cloud.upi_attested::bigint),
             (4, 'upi_verified',       c.upi_verified::bigint,       cloud.upi_verified::bigint),
             (5, 'cash_in',            c.cash_in::bigint,            cloud.cash_in::bigint),
             (6, 'cash_out',           c.cash_out::bigint,           cloud.cash_out::bigint),
             (7, 'rounding',           c.rounding::bigint,           cloud.rounding::bigint),
             (8, 'under_review_count', c.under_review_count::bigint, cloud.under_review_count::bigint),
             (9, 'under_review_total', c.under_review_total::bigint, cloud.under_review_total::bigint)
           ) as v(n, figure, till, cloud)
     order by v.n
$$;

revoke all on function public.day_close_check(uuid) from public;
grant execute on function public.day_close_check(uuid) to authenticated;

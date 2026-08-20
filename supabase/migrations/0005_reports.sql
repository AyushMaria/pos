-- 0005_reports — margin reporting behind `report.margin`.
--
-- 0003 revoked SELECT on product_prices.cost from `authenticated`, so no
-- client can read cost off the table. This file provides the one legitimate
-- route to it: a security-definer view that checks the permission itself.
--
-- The view owner (postgres) can see cost; the caller cannot. The guard is in
-- the WHERE clause, so a caller without the permission gets zero rows rather
-- than an error — which is the right shape for a report, and identical in
-- effect to being denied.

create schema if not exists reports;
grant usage on schema reports to authenticated;

create or replace view reports.product_margin
with (security_invoker = false) as
select
    pp.store_id,
    pp.product_id,
    p.sku,
    p.name,
    pp.price,
    pp.cost,
    pp.price - pp.cost                                     as margin,
    case when pp.price = 0 then null
         else round(((pp.price - pp.cost)::numeric * 10000) / pp.price)::int
    end                                                    as margin_bp
from public.product_prices pp
join public.products p on p.id = pp.product_id
where pp.deleted_at is null
  and pp.valid_to is null
  and auth.has_perm('report.margin')
  and auth.in_store(pp.store_id);

grant select on reports.product_margin to authenticated;

comment on view reports.product_margin is
    'Cost and margin per product. Returns nothing unless the caller holds '
    'report.margin for the store. Cost is otherwise unreadable — SELECT on '
    'product_prices.cost is revoked from authenticated in 0003_rls.sql.';

-- 0026 — the owner's reports (phase 8 slice 4).
--
-- Three questions about what the shop did, read from the cloud because the
-- cloud is the only place that has every till's sales (decision 3 of the
-- phase 8 plan): what came in each day, what sold, and what is on the
-- shelves now. All three are online-only, and the till says so.
--
-- ## Functions, not views — changed from the plan's sketch
--
-- The plan sketched `reports.sales_by_day` and friends as views beside
-- `reports.product_margin`. Two things made that the wrong shape:
--
-- * **A view cannot take a date range.** "Sales by product for last week"
--   aggregates *after* filtering; a view grouped by product has already
--   summed every day there is. The workaround — a view grouped by (day,
--   product) and summed again by the till — ships 30 × 20,000 rows to
--   answer a question with 200 in it.
-- * **`reports` is not a schema PostgREST exposes.** `product_margin` has
--   never been read over REST (its route answered 501). `public` is, and a
--   function there is `rpc/<name>` — the path slice 3's `day_close_check`
--   already proved on the live project.
--
-- ## The guard, in the same shape as `product_margin`
--
-- SECURITY DEFINER, because cost is a column `authenticated` cannot select
-- (0003) and these functions are the legitimate route to it, exactly as the
-- 0005 view is. So the function checks for itself, in its WHERE clause:
--
-- * `report.sales.store` and the store, or **no rows** — the shape a report
--   refusal has always had here;
-- * `report.margin`, or the cost columns come back **null**. The rows are
--   the same rows; only the column set differs. That is the promise "margin
--   columns appear only under `report.margin`, by RLS not by screen": a
--   supervisor holding a REST client and their own token gets the same nulls
--   the screen would have hidden.
--
-- ## The rules, written down once
--
-- * **Date range on `server_received_at`**, never terminal time (§9.4). The
--   caller names local days and its timezone; the day boundaries are worked
--   out here, so a sale at 00:05 IST is on the day an Indian owner thinks it
--   is on, and not on the UTC one before it.
-- * **Which sales count** is the Z-report's rule: `completed`, or
--   `requires_review` resolved `paid`. A sale still under review is a count
--   and a sum beside the takings and never inside them; `not_paid` is in no
--   figure. Unlike the Z, a review counts whenever it was resolved — a
--   report run today knows what the close could not.
-- * **Takings** are approved payments of counting sales, which is what the
--   Z-report's Takings line sums. That is the figure the plan's proof ties
--   to the Z, so it is computed from the same table rather than from
--   `grand_total`.
-- * **Cost** of a line is the latest cost recorded for that product and
--   store *at or before* the sale reached the cloud. Not the cost on the
--   current price row: `set_price` (phase 6) opens a new row without one,
--   so the current row's cost is usually null even for a product whose cost
--   is known. And not a cost recorded after the sale, which would rewrite
--   last month's margin every time a supplier raised a price.
-- * **Margin is over costed lines only**, with the rest named. Most of the
--   migrated catalogue has no cost, and the unlisted placeholder never will;
--   a margin that went null for the whole day because one loose item had no
--   cost would be null every day. So `margin = costed sales − cost`, and
--   `uncosted_sales` says how much of the day the margin does not cover. The
--   same convention as `product_margin`: GST-inclusive price less cost as
--   recorded.

-- ── The sales a report counts ─────────────────────────────────────────────
--
-- Private helpers in `pos`, which PostgREST does not expose (see
-- docs/existing-project-setup.md). They carry no guard of their own and run
-- only inside the definer functions below, so `authenticated` gets no
-- EXECUTE on them.

create or replace function pos.report_sales(
    p_store_id uuid, p_from date, p_to date, p_tz text
)
returns table (
    id uuid, day date, counting boolean, under_review boolean,
    grand_total integer, tax_total integer, discount_total integer,
    rounding_adjustment integer, received_at timestamptz
)
language sql
stable
set search_path = public, pg_temp
as $$
    select sa.id,
           (sa.server_received_at at time zone p_tz)::date,
           sa.status = 'completed' or (sa.status = 'requires_review' and rv.outcome = 'paid'),
           sa.status = 'requires_review' and rv.outcome is null,
           sa.grand_total, sa.tax_total, sa.discount_total, sa.rounding_adjustment,
           sa.server_received_at
      from public.sales sa
      left join lateral (
          select r.outcome from public.sale_reviews r
           where r.sale_id = sa.id
           order by r.resolved_at desc
           limit 1
      ) rv on true
     where sa.store_id = p_store_id
       and sa.server_received_at >= (p_from::timestamp at time zone p_tz)
       and sa.server_received_at <  ((p_to + 1)::timestamp at time zone p_tz)
       and sa.status in ('completed', 'requires_review')
$$;

create or replace function pos.report_lines(
    p_store_id uuid, p_from date, p_to date, p_tz text
)
returns table (
    sale_id uuid, day date, product_id uuid, qty_milli integer,
    line_total integer, tax_amount integer, discount_amount integer,
    line_cost bigint
)
language sql
stable
set search_path = public, pg_temp
as $$
    select s.id, s.day, sl.product_id, sl.qty_milli,
           sl.line_total, sl.tax_amount, sl.discount_amount,
           round(c.cost::numeric * sl.qty_milli / 1000)::bigint
      from pos.report_sales(p_store_id, p_from, p_to, p_tz) s
      join public.sale_lines sl on sl.sale_id = s.id
      left join lateral (
          select pp.cost from public.product_prices pp
           where pp.product_id = sl.product_id
             and pp.store_id = p_store_id
             and pp.cost is not null
             and pp.deleted_at is null
             and pp.valid_from <= s.received_at
           order by pp.valid_from desc
           limit 1
      ) c on true
     where s.counting
$$;

revoke all on function pos.report_sales(uuid, date, date, text) from public, anon, authenticated;
revoke all on function pos.report_lines(uuid, date, date, text) from public, anon, authenticated;

-- ── Sales by day ──────────────────────────────────────────────────────────
--
-- One row per day in the range, including the days nothing sold — a week
-- with a closed Sunday should say so rather than look like six days.

create or replace function public.report_sales_by_day(
    p_store_id uuid, p_from date, p_to date, p_tz text default 'Asia/Kolkata'
)
returns table (
    day                date,
    sales_count        bigint,
    takings            bigint,
    cash               bigint,
    upi_attested       bigint,
    upi_verified       bigint,
    tax                bigint,
    discounts          bigint,
    rounding           bigint,
    under_review_count bigint,
    under_review_total bigint,
    cost               bigint,
    margin             bigint,
    uncosted_sales     bigint
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    -- One aggregate per source, joined onto the calendar: a year's range is
    -- three passes over the year, not one per day per figure.
    with s as (
        select * from pos.report_sales(p_store_id, p_from, p_to, p_tz)
    ),
    by_sale as (
        select day,
               count(*) filter (where counting)                          as sales_count,
               coalesce(sum(tax_total) filter (where counting), 0)       as tax,
               coalesce(sum(discount_total) filter (where counting), 0)  as discounts,
               coalesce(sum(rounding_adjustment) filter (where counting), 0) as rounding,
               count(*) filter (where under_review)                      as under_review_count,
               coalesce(sum(grand_total) filter (where under_review), 0) as under_review_total
          from s
         group by day
    ),
    by_payment as (
        select s.day,
               sum(p.amount)                                                        as takings,
               sum(p.amount) filter (where p.method = 'cash')                       as cash,
               sum(p.amount) filter (where p.method = 'upi' and not p.verified)     as upi_attested,
               sum(p.amount) filter (where p.method = 'upi' and p.verified)         as upi_verified
          from s join public.payments p on p.sale_id = s.id
         where s.counting and p.status = 'approved'
         group by s.day
    ),
    by_line as (
        -- `line_total - line_cost` is null for an uncosted line, so the
        -- margin sums the costed lines only; the rest is `uncosted_sales`.
        select day,
               sum(line_cost)                                    as cost,
               sum(line_total - line_cost)                       as margin,
               sum(line_total) filter (where line_cost is null)  as uncosted_sales
          from pos.report_lines(p_store_id, p_from, p_to, p_tz)
         group by day
    ),
    days as (
        select d::date as day
          from generate_series(p_from::timestamp, p_to::timestamp, interval '1 day') d
    )
    select
        d.day,
        coalesce(bs.sales_count, 0),
        coalesce(bp.takings, 0),
        coalesce(bp.cash, 0),
        coalesce(bp.upi_attested, 0),
        coalesce(bp.upi_verified, 0),
        coalesce(bs.tax, 0),
        coalesce(bs.discounts, 0),
        coalesce(bs.rounding, 0),
        coalesce(bs.under_review_count, 0),
        coalesce(bs.under_review_total, 0),
        case when pos.has_perm('report.margin') then coalesce(bl.cost, 0) end,
        case when pos.has_perm('report.margin') then coalesce(bl.margin, 0) end,
        case when pos.has_perm('report.margin') then coalesce(bl.uncosted_sales, 0) end
      from days d
      left join by_sale bs on bs.day = d.day
      left join by_payment bp on bp.day = d.day
      left join by_line bl on bl.day = d.day
     where pos.has_perm('report.sales.store') and pos.in_store(p_store_id)
     order by d.day
$$;

-- ── Sales by product ──────────────────────────────────────────────────────

create or replace function public.report_sales_by_product(
    p_store_id uuid, p_from date, p_to date, p_tz text default 'Asia/Kolkata'
)
returns table (
    product_id     uuid,
    sku            text,
    name           text,
    uom            text,
    qty_milli      bigint,
    sales_count    bigint,
    sales          bigint,
    tax            bigint,
    discounts      bigint,
    cost           bigint,
    margin         bigint,
    uncosted_sales bigint
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select l.product_id, p.sku, p.name, p.uom,
           sum(l.qty_milli),
           count(distinct l.sale_id),
           sum(l.line_total),
           sum(l.tax_amount),
           sum(l.discount_amount),
           case when pos.has_perm('report.margin') then
               coalesce(sum(l.line_cost), 0)
           end,
           case when pos.has_perm('report.margin') then
               coalesce(sum(l.line_total - l.line_cost), 0)
           end,
           case when pos.has_perm('report.margin') then
               coalesce(sum(l.line_total) filter (where l.line_cost is null), 0)
           end
      from pos.report_lines(p_store_id, p_from, p_to, p_tz) l
      join public.products p on p.id = l.product_id
     where pos.has_perm('report.sales.store') and pos.in_store(p_store_id)
     group by l.product_id, p.sku, p.name, p.uom
     order by sum(l.line_total) desc, p.sku
$$;

-- ── Stock position ────────────────────────────────────────────────────────
--
-- Now, not over a range: the ledger's running balance is `stock_levels`,
-- and a stock position "as of last Tuesday" is a ledger replay nobody has
-- asked for yet. Rooted at `stock_levels` like `low_stock` (0017), so the
-- unlisted placeholder — which never gets a level row — is excluded for
-- free. Value at the current selling price, and at the latest known cost.

create or replace function public.report_stock_position(p_store_id uuid)
returns table (
    product_id     uuid,
    sku            text,
    name           text,
    uom            text,
    on_hand        integer,
    reorder_point  integer,
    price          integer,
    value_at_price bigint,
    unit_cost      integer,
    value_at_cost  bigint
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select sl.product_id, p.sku, p.name, p.uom, sl.on_hand, sl.reorder_point,
           pr.price,
           round(pr.price::numeric * sl.on_hand / 1000)::bigint,
           case when pos.has_perm('report.margin') then c.cost end,
           case when pos.has_perm('report.margin') then
               round(c.cost::numeric * sl.on_hand / 1000)::bigint
           end
      from public.stock_levels sl
      join public.products p on p.id = sl.product_id
      left join lateral (
          select pp.price from public.product_prices pp
           where pp.product_id = sl.product_id and pp.store_id = sl.store_id
             and pp.valid_to is null and pp.deleted_at is null
           order by pp.valid_from desc
           limit 1
      ) pr on true
      left join lateral (
          select pp.cost from public.product_prices pp
           where pp.product_id = sl.product_id and pp.store_id = sl.store_id
             and pp.cost is not null and pp.deleted_at is null
           order by pp.valid_from desc
           limit 1
      ) c on true
     where sl.store_id = p_store_id
       and p.track_stock
       and p.deleted_at is null
       and pos.has_perm('report.sales.store') and pos.in_store(p_store_id)
     order by p.sku
$$;

-- `anon` by name as well: Supabase's default privileges grant it EXECUTE on
-- every new function in `public` directly, not through PUBLIC.
revoke all on function public.report_sales_by_day(uuid, date, date, text) from public, anon;
revoke all on function public.report_sales_by_product(uuid, date, date, text) from public, anon;
revoke all on function public.report_stock_position(uuid) from public, anon;
grant execute on function public.report_sales_by_day(uuid, date, date, text) to authenticated;
grant execute on function public.report_sales_by_product(uuid, date, date, text) to authenticated;
grant execute on function public.report_stock_position(uuid) to authenticated;

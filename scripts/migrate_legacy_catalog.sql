-- migrate_legacy_catalog.sql — the legacy catalogue into the POS tables.
--
-- Run ONCE, in the Supabase SQL editor, after:
--   1. `supabase db push`        (0000 … 0008)
--   2. `supabase/seed/seed.sql`  (the store row this needs for prices)
--
-- It reads `legacy.products` — put there by 0000_legacy_rehome — and writes
-- `public.products`, `public.product_barcodes` and `public.product_prices`.
-- It is a SQL translation of `scripts/import_catalog.py`, which does the same
-- job into the terminal's SQLite. Same rules, same refusals; the difference is
-- that both tables now live in one database, so 32k rows move without a
-- network round trip.
--
-- Safe to re-run: every insert is `on conflict do nothing`, and the guard
-- below stops a second run from creating a duplicate set of products.
--
-- ── TWO HONEST WARNINGS, carried over from import_catalog.py ───────────────
--
-- *Prices.* `import_catalog.py` warns that the legacy `mrp` column is a
-- float and takes considerable care with it. On this project it is `numeric`,
-- so that warning does not apply: the values are exact decimals and there is
-- no binary approximation to defend against. The cast below is kept anyway —
-- it is a no-op on numeric and the correct thing on float8, so this file is
-- right either way. What still applies is the refusal: anything that is not a
-- whole number of paise is skipped and reported, never rounded.
--
-- *Tax codes.* The legacy system has no tax column. Rates here are inferred
-- from `main_group` by the table below. **That is a development assumption,
-- not a fact about the business.** It is a small, printed, editable table so
-- the owner can review it. Replacing it with real per-product rates is the
-- week-18 catalogue migration (execution plan §6 step 3). Do not ship a pilot
-- on inferred GST.

begin;

-- ── Guards ────────────────────────────────────────────────────────────────

do $$
begin
    if to_regclass('legacy.products') is null then
        raise exception 'legacy.products not found — apply 0000_legacy_rehome first';
    end if;
    if to_regclass('public.products') is null then
        raise exception 'public.products not found — run supabase db push first';
    end if;
    if not exists (select 1 from public.stores where code = 'ST01') then
        raise exception 'no store with code ST01 — apply supabase/seed/seed.sql first';
    end if;
    -- The generated SKUs are six zero-padded digits. The demo seed's own
    -- products (SKU-1001 …) are four, so a seeded project does not trip this.
    if exists (select 1 from public.products where sku ~ '^SKU-[0-9]{6}$') then
        raise exception 'legacy catalogue already imported — delete those products first if you mean to redo it';
    end if;
end
$$;

-- ── The barcode reader ────────────────────────────────────────────────────
--
-- A translation of `app/domain/barcode.py::parse`, and the one place this
-- file forks logic that lives somewhere else. It is kept because the
-- alternative — pulling 32k rows out over REST and pushing them back with a
-- service_role key — trades a readable fork for a key that architecture §1.7
-- says must never leave the owner's hands.
--
-- `scripts/check_sql_barcode_parity.py` runs both implementations over the
-- 308-code corpus in tests/fixtures and fails on any disagreement. Run it if
-- you touch either.

create or replace function legacy.gs1_check_digit(payload text)
returns int language sql immutable as $$
    -- Weights alternate 3,1 from the right. Same rule for EAN-8, UPC-A,
    -- EAN-13 and ITF-14; only the length differs.
    select (10 - sum(
                substr($1, i, 1)::int
                * case when (length($1) - i) % 2 = 0 then 3 else 1 end
           )::int % 10) % 10
    from generate_series(1, length($1)) as g(i);
$$;

create or replace function legacy.scan(raw text)
returns table (lookup_key text, symbology text, carries_qty boolean, err text)
language plpgsql immutable as $$
declare
    code     text := btrim(coalesce(raw, ''));
    len      int;
    prefix   text;
    expected int;
    qty      int;
begin
    if code = '' then
        return query select null::text, 'UNKNOWN'::text, false, 'no code entered'::text;
        return;
    end if;

    -- GS1-128 is refused here rather than parsed. The Python parser reads
    -- application identifiers; re-implementing that for a one-time import
    -- would be a second place to be subtly wrong about where one field ends
    -- and the next begins. These land in the exceptions table instead, where
    -- a person can look at them. On the pilot catalogue there are none.
    if position(chr(29) in code) > 0
       or (code ~ '^[0-9]+$' and code like '01%' and length(code) > 14) then
        return query select null::text, 'GS1_128'::text, false,
                            'gs1-128 — not imported, review by hand'::text;
        return;
    end if;

    -- Code128 / Code39 internal SKUs are alphanumeric and carry no structure
    -- to verify. Looked up exactly as typed.
    if code !~ '^[0-9]+$' then
        return query select code, 'UNKNOWN'::text, false, null::text;
        return;
    end if;

    len := length(code);

    -- A supplier or legacy internal code. 0.5% of the pilot catalogue is this
    -- shape, so it is a normal case, not an error.
    if len not in (8, 12, 13, 14) then
        return query select code, 'UNKNOWN'::text, false, null::text;
        return;
    end if;

    expected := legacy.gs1_check_digit(substr(code, 1, len - 1));
    if expected <> substr(code, len, 1)::int then
        -- Never interpreted. Not as a product, not as a weight, not as
        -- anything. Reading a quantity out of a corrupt number is how a till
        -- charges the wrong amount with complete confidence.
        return query select null::text,
            case len when 8 then 'EAN8' when 12 then 'UPC_A'
                     when 13 then 'EAN13' else 'ITF14' end::text,
            false,
            format('check digit is %s, expected %s', substr(code, len, 1), expected);
        return;
    end if;

    if len = 14 then
        return query select code, 'ITF14'::text, false, null::text;
        return;
    end if;

    if len = 13 then
        prefix := substr(code, 1, 2);

        if prefix = '22' then                       -- a weighing, not an identity
            qty := substr(code, 8, 5)::int;
            if qty = 0 then
                return query select null::text, 'WEIGHED'::text, false,
                                    'weighed code carries no quantity'::text;
                return;
            end if;
            -- The item code is the lookup key, not the whole barcode: the
            -- same product weighs differently every time.
            return query select substr(code, 3, 5), 'WEIGHED'::text, true, null::text;
            return;
        end if;

        if prefix = '21' then                       -- in-store product identity
            return query select code, 'INTERNAL'::text, false, null::text;
            return;
        end if;

        if prefix ~ '^2[0-9]$' then
            -- Reserved but unallocated. Refusing beats guessing.
            return query select null::text, 'EAN13'::text, false,
                format('in-store prefix %s is not configured for this store', prefix);
            return;
        end if;

        return query select code, 'EAN13'::text, false, null::text;
        return;
    end if;

    return query select code,
        case len when 8 then 'EAN8' else 'UPC_A' end::text, false, null::text;
end
$$;

-- ── Tax codes ─────────────────────────────────────────────────────────────

insert into public.tax_codes (code, name, rate_bp, is_inclusive) values
    ('GST0',  'GST 0%',     0, true),
    ('GST5',  'GST 5%',   500, true),
    ('GST12', 'GST 12%', 1200, true),
    ('GST18', 'GST 18%', 1800, true),
    ('GST28', 'GST 28%', 2800, true)
on conflict (code) do update set rate_bp = excluded.rate_bp;

-- The development tax assumption. Review this with the owner.
create temp table group_tax (main_group text primary key, tax_code text) on commit drop;
insert into group_tax values
    ('GROCERY & STAPLES',      'GST5'),
    ('MASALE & SPICES',        'GST5'),
    ('DAIRY & BAKERY',         'GST0'),
    ('FRUITS & VEGETABLES',    'GST0'),
    ('BRANDED FOODS & SNACKS', 'GST12'),
    ('BEVERAGES',              'GST12'),
    ('BABY & CHILD CARE',      'GST12'),
    ('TEXTILE AND HOSIERY',    'GST5'),
    ('PERSONAL CARE',          'GST18'),
    ('HOME & KITCHEN',         'GST18'),
    ('HOUSE HOLD PRODUCTS',    'GST18'),
    ('HOUSE HOLD OTHER',       'GST18'),
    ('CROCKERY & APPLINCES',   'GST18'),
    ('TOYS  AND GAMES SET',    'GST18'),
    ('GENERAL',                'GST18');

-- ── Staging ───────────────────────────────────────────────────────────────
--
-- Every legacy row, with its verdict attached, so that what was imported and
-- what was refused come out of the same pass and cannot disagree.

create temp table stage on commit drop as
with source as (
    select
        l.id                                             as legacy_id,
        gen_random_uuid()                                as product_id,
        btrim(coalesce(l.item_name, ''))                 as name,
        btrim(coalesce(l.upc_ean_code, ''))              as raw_barcode,
        upper(btrim(coalesce(l.main_group, '')))         as main_group,
        btrim(coalesce(l.sub_group, ''))                 as sub_group,
        -- Exact on numeric; the shortest round-trip decimal on float8. Never
        -- `mrp * 100` applied to a float directly.
        case when l.mrp is null then null else l.mrp::numeric end as mrp
    from legacy.products l
    -- `quantity` and `brand_specifications` are deliberately not read.
    -- Opening stock is a stock-take, not an import: a count copied from a
    -- retired system is a number nobody has verified standing behind a
    -- shelf. `stock_levels` starts empty and the M2 test measures the change
    -- rather than the absolute figure.
),
priced as (
    select
        s.*,
        case
            when s.mrp is null or s.mrp < 0 then null
            when (s.mrp * 100) <> trunc(s.mrp * 100) then null
            -- `product_prices.price` is an `integer`, so the ceiling is
            -- 2,147,483,647 paise — a shade over two crore rupees. One real
            -- row holds 8,351,262,172,680, which is a barcode typed into the
            -- price field, and without this guard it does not import badly:
            -- it aborts the whole migration on the insert with `integer out
            -- of range`. Refused and reported, like every other price this
            -- file will not vouch for.
            when (s.mrp * 100) > 2147483647 then null
            else (s.mrp * 100)::bigint
        end as price_paise
    from source s
),
scanned as (
    select p.*, b.lookup_key, b.symbology, b.carries_qty, b.err
    from priced p
    left join lateral legacy.scan(p.raw_barcode) b on true
)
select
    sc.*,
    coalesce(gt.tax_code, 'GST18') as tax_code,
    case
        when sc.name = ''                         then 'no name'
        when sc.mrp is null                       then 'no usable price'
        when sc.mrp * 100 > 2147483647            then 'price out of range'
        when sc.price_paise is null               then 'no usable price'
        else null
    end as product_skip_reason,
    case
        when sc.raw_barcode = ''  then 'no barcode (searchable by name only)'
        when sc.carries_qty       then 'barcode reads as a weighed code'
        when sc.err is not null   then sc.err
        else null
    end as barcode_skip_reason
from scanned sc
left join group_tax gt on gt.main_group = sc.main_group;

-- Deterministic SKUs, and the duplicate-barcode verdict.
--
-- Duplicate barcodes are settled here rather than by the unique index, so
-- that the losers can be named in the exceptions table. `ux_product_barcodes_
-- barcode` is unique for a reason: the same code on two products means the
-- till picks one at random. The pilot catalogue has four such codes (`2`,
-- `200`, `450`, `900`) sitting on 28 products, and every one of them is a
-- product that will not be findable by scanning until someone resolves it.
create temp table numbered on commit drop as
with with_sku as (
    select
        legacy_id, product_id, name, raw_barcode, main_group, sub_group,
        price_paise, tax_code, lookup_key, symbology, barcode_skip_reason,
        -- Numbered by the legacy row id. The Python importer numbers by fetch
        -- order, which a paginated query without an ORDER BY does not
        -- actually guarantee; the id is stable across re-runs.
        'SKU-' || lpad(row_number() over (order by legacy_id)::text, 6, '0') as sku
    from stage
    where product_skip_reason is null
)
select
    w.*,
    case
        when w.barcode_skip_reason is not null then w.barcode_skip_reason
        when row_number() over (
                 partition by w.raw_barcode
                 order by w.sku
             ) > 1
            then 'duplicate barcode'
        else null
    end as final_barcode_reason
from with_sku w;

-- ── Categories ────────────────────────────────────────────────────────────
--
-- Not in the Python importer, which leaves category_id null because SQLite
-- had nothing to put there. The grouping is sitting right next to the data
-- here, and `category_id` is nullable, so the cloud catalogue gets the shape
-- the legacy system already knew.

insert into public.categories (name)
select distinct main_group from numbered
where main_group <> ''
on conflict do nothing;

insert into public.categories (name, parent_id)
select distinct n.sub_group, c.id
from numbered n
join public.categories c on c.name = n.main_group and c.parent_id is null
where n.sub_group <> '' and n.main_group <> ''
on conflict do nothing;

-- ── Products ──────────────────────────────────────────────────────────────

insert into public.products
    (id, sku, name, short_name, category_id, uom, is_weighed, track_stock,
     tax_code, is_active)
select
    n.product_id,
    n.sku,
    n.name,
    left(n.name, 24),
    coalesce(
        (select c.id from public.categories c
          join public.categories p on p.id = c.parent_id
         where c.name = n.sub_group and p.name = n.main_group limit 1),
        (select c.id from public.categories c
         where c.name = n.main_group and c.parent_id is null limit 1)
    ),
    'each',
    false,          -- weighed items are configured in the admin screens, not guessed here
    true,
    n.tax_code,
    true
from numbered n
on conflict (id) do nothing;

-- ── Prices ────────────────────────────────────────────────────────────────

insert into public.product_prices (product_id, store_id, price, cost, valid_from)
select n.product_id, s.id, n.price_paise, null, now()
from numbered n
cross join (select id from public.stores where code = 'ST01') s
on conflict do nothing;

-- ── Barcodes ──────────────────────────────────────────────────────────────

insert into public.product_barcodes
    (product_id, barcode, symbology, pack_size, is_primary)
select n.product_id, n.raw_barcode, n.symbology, 1, true
from numbered n
where n.final_barcode_reason is null
on conflict do nothing;

-- ── What was refused ──────────────────────────────────────────────────────
--
-- Kept as a table, not a notice. Every one of these is a row a cashier will
-- eventually fail to find at the counter, and the list is the week-18
-- catalogue-cleanup worklist (execution plan §6 step 2).

-- Which legacy row became which product. `public.products` has nowhere to
-- record it, and without it the exceptions below are a list of names with no
-- way back to the row that produced them.
drop table if exists legacy.product_id_map;
create table legacy.product_id_map as
select legacy_id, product_id, sku from numbered;
alter table legacy.product_id_map add primary key (legacy_id);
create unique index ux_product_id_map_product on legacy.product_id_map(product_id);

drop table if exists legacy.import_exceptions;
create table legacy.import_exceptions as
-- Products that never made it in.
select s.legacy_id, s.name, s.raw_barcode, s.main_group, s.mrp,
       s.product_skip_reason as reason,
       s.product_skip_reason as bucket,
       'product not imported'::text as severity
from stage s
where s.product_skip_reason is not null
union all
-- Products that came in, but cannot be found by scanning.
select n.legacy_id, n.name, n.raw_barcode, n.main_group, null::numeric,
       n.final_barcode_reason,
       -- Bucketed. 78 distinct "expected 7, got 0" lines tell you nothing a
       -- total does not; the per-row detail stays in `reason`.
       case when n.final_barcode_reason like 'check digit%'
            then 'bad check digit' else n.final_barcode_reason end,
       'product imported, barcode not'::text
from numbered n
where n.final_barcode_reason is not null;

drop table if exists legacy.import_summary;
create table legacy.import_summary as
select 'legacy rows' as item, count(*) as count from legacy.products
union all select 'products imported', count(*) from numbered
union all select 'prices imported',   count(*) from numbered
union all select 'barcodes imported', count(*) from numbered
                                      where final_barcode_reason is null
union all select 'products refused',  count(*) from legacy.import_exceptions
                                      where severity = 'product not imported'
union all select 'barcodes refused',  count(*) from legacy.import_exceptions
                                      where severity <> 'product not imported';

commit;

-- ── Read the result ───────────────────────────────────────────────────────

select * from legacy.import_summary;

-- select reason, severity, count(*)
--   from legacy.import_exceptions group by 1, 2 order by 3 desc;

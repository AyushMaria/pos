# Putting the POS schema into the existing Supabase project

The project at `tyajooufoxevshqroirc` already holds the retired billing
system: `public.products` (32,165 rows, real barcodes) and `public.orders`.
The POS schema wants `public.products` for itself. Two tables cannot share one
name, so the legacy pair moves into a `legacy` schema first, and everything
else follows normally.

Keeping both in one project turns out to be the *easier* path, not the harder
one: the catalogue migration becomes a query against a table in the same
database rather than 32k rows pulled over REST and pushed back with a
`service_role` key that architecture §1.7 says should never leave your hands.

Nothing here deletes anything.

## What the project actually looks like

Checked before any of this was written, not assumed:

- `public` holds **exactly** `products` and `orders`. Everything else lives in
  `auth`, `storage`, `realtime`, `vault` and `extensions`, which are
  Supabase's own and are not touched.
- There is **no `supabase_migrations` schema**, so the CLI has never managed
  this project. `db push` applies 0000–0008 from scratch rather than
  reconciling against existing history.
- `legacy.products` columns: `id` (bigint), `item_name`, `upc_ean_code`,
  `main_group`, `sub_group`, `brand_specifications`, `mrp` (**numeric**),
  `quantity`.

Two of those are worth knowing about.

**`mrp` is `numeric`, not a float.** `scripts/import_catalog.py` warns at
length about float precision and takes real care with `Decimal(str(x))`. That
warning does not apply here — the values are exact decimals. The migration
keeps the defensive cast anyway, because it is a no-op on numeric and correct
on float8, so the file is right whichever a future dump turns out to hold.

**`quantity` and `brand_specifications` are deliberately not imported.**
Opening stock is a stock-take, not an import: a count copied out of a retired
system is a number nobody has verified standing in front of a shelf.
`stock_levels` starts empty, and the M2 test measures the *change* in stock
rather than the absolute figure, so nothing there depends on it.

---

## Before you start: take a backup

The one irreversible-feeling step is moving those two tables. It is reversible
(`alter table legacy.products set schema public`) and 0000 moves rather than
drops, so the procedure itself never destroys anything — but this is the only
copy of the catalogue, so take a copy anyway.

**Not `supabase db dump`.** That command shells out to `pg_dump` inside a
container and fails on a machine without Docker Desktop, which is the same
reason `scripts/run_rls_tests.py` exists.

**The snapshot that matters, in ten seconds.** Paste into the SQL editor. It
copies the two tables inside the database, which is exactly the risk being
insured against, and needs no tooling at all:

```sql
create schema if not exists legacy_backup;
create table legacy_backup.products as select * from public.products;
create table legacy_backup.orders   as select * from public.orders;

select 'products' as t, count(*) from legacy_backup.products
union all select 'orders', count(*) from legacy_backup.orders;
```

Drop that schema once the catalogue migration has run and you are happy with
the numbers.

**A copy off the server**, if you want one on your own disk: Table Editor →
`products` → Export → CSV, and the same for `orders`. No tooling required.

**A real `.sql` dump without Docker**, if you would rather have one — `pgserver`
is already in the venv and bundles `pg_dump.exe`. Check the server first,
because that binary is PostgreSQL 16.2 and `pg_dump` refuses to read a server
newer than itself:

```sql
select version();
```

If that says 15.x or 16.x, this works (connection string from Project Settings
→ Database, and use the session pooler if your network blocks direct
connections):

```powershell
$pgd = ".venv\Lib\site-packages\pgserver\pginstall\bin\pg_dump.exe"
& $pgd "postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres" `
    --schema=public --no-owner --no-privileges -f backup-before-pos-schema.sql
```

If it says 17.x, skip this one — the in-database snapshot above already covers
the actual risk.

## 1. Move the legacy tables aside

`supabase/migrations/0000_legacy_rehome.sql` does it. It sorts before
`0001_core`, so `db push` runs it first, and it is a no-op on a project where
those tables do not exist — a fresh project is unaffected.

After the move the legacy tables are in the `legacy` schema, which PostgREST
does not expose. They stop being reachable with the anon key, which is the
right resting place for a retired system that was never written against this
project's RLS.

## 2. Push the schema

```bash
supabase db push          # 0000, then 0001 … 0008
```

**On the `pos` schema.** `0003_rls` used to create its two claim helpers,
`has_perm` and `in_store`, inside `auth`. Supabase reserves that schema to
`supabase_auth_admin` and no longer lets a project's `postgres` role create
objects in it, so the push failed with `permission denied for schema auth`.
The helpers now live in a private `pos` schema instead — `authenticated` gets
USAGE so the policies can evaluate, and because `pos` is not one of
PostgREST's exposed schemas they do not become callable RPCs the way they
would in `public`. Reading `auth.uid()` and `auth.jwt()` is unaffected; that
was never the part Supabase restricts.

If `db push` stopped part-way, nothing is half-applied — the CLI runs each
migration in its own transaction. Fix and re-run; it resumes at the first
migration not recorded in `supabase_migrations.schema_migrations`.

Verify, as the M2 guide's step 1 asks. Run these **one at a time** — the SQL
editor aborts the whole batch on the first error and shows you only the last
result set:

```sql
select proname from pg_proc where proname = 'sync_push';
```
```sql
select count(*) from public.sale_reviews;
```
```sql
select count(*) from information_schema.tables
 where table_schema = 'public' and table_type = 'BASE TABLE';   -- 24
```

## 3. Seed the store, terminal and staff

Paste the contents of `supabase/seed/seed.sql` into the SQL editor and run it.
The editor connects as a superuser, which this needs — it inserts `auth.users`
rows for the three staff accounts.

(If you would rather use `psql`, the connection string is in Project Settings →
Database → Connection string. There is no `supabase db url` command.)

This is what creates the store `ST01` and terminal `T1` — and terminal `T1`'s
UUID is `018f0000-0000-7000-8000-000000000200`, which is already the
`POS_TERMINAL_ID` in your `.env`. Nothing to change there.

It also inserts five demo products (`SKU-1001` … `SKU-1005`). They are
harmless and the catalogue migration works around them, but you can delete
them once the real catalogue is in.

## 4. Enable the JWT hook

Dashboard → Authentication → Hooks → *Customize Access Token (JWT) Claims* →
`public.custom_access_token_hook`.

Without this, tokens carry no `permissions` or `store_ids` claim and every RLS
policy denies everything — which looks exactly like a sync bug and isn't one.

## 4b. Deploy the login function

Nobody can sign in online until this exists. The till does not verify PINs
itself: it posts to the `authenticate-pin` Edge Function, which holds the
`service_role` key server-side and is the reason that key never ships with the
application (§1.7).

```bash
supabase functions deploy authenticate-pin
```

If the CLI asks for Docker, deploy from the dashboard instead: Edge Functions →
Deploy a new function → name it exactly `authenticate-pin` → paste
`supabase/functions/authenticate-pin/index.ts`.

No secrets to set. The three variables it reads — `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY` — are populated automatically
in every Edge Function.

Check it answers, and that it rejects a wrong PIN rather than erroring:

```bash
curl -s -o /dev/null -w "%{http_code}\n" `
  -X POST "https://<ref>.supabase.co/functions/v1/authenticate-pin" `
  -H "apikey: <anon key>" -H "Content-Type: application/json" `
  -d '{"employee_code":"M001","pin":"0000","store_code":"ST01","terminal_code":"T1"}'
```

`401` is the pass — the function is live and refused a wrong PIN. `404` means
it is not deployed.

## 5. Migrate the catalogue

Paste `scripts/migrate_legacy_catalog.sql` into the SQL editor and run it. It
reads `legacy.products` and writes `public.products`, `product_barcodes`,
`product_prices` and `categories`.

It is the SQL translation of `scripts/import_catalog.py` — same rules, same
refusals:

- prices via `numeric`, never by multiplying a float, and anything that is not
  a whole number of paise is refused rather than rounded;
- GST inferred from `main_group` by a small printed table — **a development
  assumption, not a fact about the business** (execution plan §6 step 3);
- barcodes validated by check digit, weighed `22…` codes excluded, duplicates
  resolved to one product and the losers reported.

It refuses to run twice, and reports what it did:

```sql
select * from legacy.import_summary;

select bucket, severity, count(*) from legacy.import_exceptions
 group by 1, 2 order by 3 desc;
```

It also leaves `legacy.product_id_map` behind — which legacy row became which
product and SKU. `public.products` has nowhere to record that, and without it
the exceptions list is a column of names with no way back to the row that
produced them:

```sql
select e.legacy_id, e.name, e.raw_barcode, e.bucket, l.brand_specifications
  from legacy.import_exceptions e
  join legacy.products l on l.id = e.legacy_id
 where e.bucket = 'duplicate barcode'
 order by e.raw_barcode;
```

Expect roughly 12,431 products with no barcode (findable by name only), ~106
with a bad check digit, and 28 products sharing the four junk short codes `2`,
`200`, `450`, `900`. That exceptions table **is** the week-18 catalogue
cleanup worklist — every row in it is a product a cashier will one day fail to
find at the counter.

`categories` is a small addition the Python importer does not make:
`main_group` becomes a parent category and `sub_group` a child. The column was
nullable and the grouping was sitting right there.

## 6. Fix the environment variables — the till is not reading them

This is the one that will otherwise waste an afternoon. `app/config.py` loads
settings with `env_prefix="POS_"`, so it looks for `POS_SUPABASE_URL` and
`POS_SUPABASE_ANON_KEY`. Your `.env` has `SUPABASE_URL` and `SUPABASE_KEY`,
which the app never reads — as far as the till is concerned there is no cloud
project at all, and `POS_ALLOW_OFFLINE_BOOTSTRAP=1` is the only reason it
still starts.

**Add** these two lines — do not replace the existing pair, which
`scripts/import_barcode_corpus.py` still uses to reach the legacy data:

```
POS_SUPABASE_URL=https://tyajooufoxevshqroirc.supabase.co
POS_SUPABASE_ANON_KEY=<the anon key from Settings → API>
```

Check the key you paste is the **anon** key. A `service_role` key here lets
the till read every store's margins and RLS stops meaning anything.

Once both are set, the sync badge appears in the register header — the M2
guide's "no badge where the badge should be" symptom is this and nothing else.

## 7. Then run the M2 test

Steps 1 and 2 of `docs/m2-test-guide.md` are now done. Pick it up at step 3:
start the app, sign in **online**, record the baseline counts, and go.

---

## What was checked before any of this reached you

The whole sequence was rehearsed on a Postgres 16 against a stand-in built to
match this project's real column list — `bigint id`, `numeric mrp`,
`brand_specifications`, `quantity`: rehome → 0001…0008 → seed → catalogue
migration. All eight migrations applied on top of the moved tables, 24 tables
in `public`, the legacy rows untouched, and the catalogue landed with
duplicates, bad check digits and unusable prices all reported rather than
silently dropped. A second run of the migration refuses rather than
duplicating.

`legacy.scan`, the barcode reader inside the migration, is a fork of
`app/domain/barcode.py`. Forks drift, so `scripts/check_sql_barcode_parity.py`
runs both over the 308-code corpus plus twelve synthetic codes covering the
branches the corpus misses, and asserts identical verdicts. It currently
passes on 322 codes with zero disagreements, the only exemption being GS1-128,
which the SQL declines to parse and reports for hand review instead.

```bash
pip install -e ".[pg,dev]"
POS_TEST_PG_DSN=<a scratch postgres> python scripts/check_sql_barcode_parity.py
```

Run it if you touch either implementation.

## Known divergences from `import_catalog.py`

Deliberate, and small:

| | Python importer | This migration |
|---|---|---|
| SKU order | fetch order (paginated, not actually deterministic) | by legacy `id` — stable across re-runs |
| Traceability | none | `legacy.product_id_map` |
| `category_id` | always null | from `main_group` / `sub_group` |
| Duplicate barcodes | counted in a summary | listed per product in `legacy.import_exceptions` |
| GS1-128 codes | parsed | refused and reported |
| ITF-14 `pack_size` | 1 | 1 — unchanged, but note `parse()` marks these as case codes, so pack sizes still need setting by hand before receiving works |

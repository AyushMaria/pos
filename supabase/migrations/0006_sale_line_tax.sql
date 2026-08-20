-- 0006_sale_line_tax — mirror of the local 002 migration.
--
-- The two schemas must stay in step or sync payloads will not validate
-- (see the header of 0001_core.sql). See app/data/migrations/002_sale_line_tax.sql
-- for why the rate is snapshotted rather than re-read from the catalogue.

alter table public.sale_lines add column tax_code text;
alter table public.sale_lines add column tax_rate_bp integer not null default 0;

create index ix_sale_lines_tax on public.sale_lines(sale_id, tax_code);

-- 0007_sale_reviews — mirror of the local 003 migration.
--
-- The two schemas must stay in step or sync payloads will not validate (see
-- the header of 0001_core.sql). See app/data/migrations/003_sale_reviews.sql
-- for why resolving a review is an append-only row rather than an UPDATE on
-- `sales` — in short, because there is no UPDATE policy on `sales` and there
-- never will be.

create table public.sale_reviews (
    id          uuid primary key,
    sale_id     uuid not null references public.sales(id),
    outcome     text not null check (outcome in ('paid', 'not_paid')),
    note        text,
    resolved_by uuid not null references auth.users(id),
    resolved_at timestamptz not null default now(),
    server_received_at timestamptz not null default now()
);

create unique index ux_sale_reviews_sale on public.sale_reviews(sale_id);

alter table public.sale_reviews enable row level security;

-- Insert requires the permission, and the row must name its author. A cashier
-- resolving their own disputed payment is precisely the case this prevents
-- (architecture §13.5) — the permission is not granted to the cashier role.
create policy sale_reviews_insert on public.sale_reviews
    for insert to authenticated
    with check (exists (select 1 from public.sales s
                         where s.id = sale_id and auth.in_store(s.store_id))
                and auth.has_perm('sale.review.resolve')
                and resolved_by = auth.uid());

create policy sale_reviews_select on public.sale_reviews
    for select to authenticated
    using (exists (select 1 from public.sales s
                    where s.id = sale_id and auth.in_store(s.store_id)));

-- No update or delete policy. A resolution that turns out to be wrong is a
-- conversation with the manager, not an edit.

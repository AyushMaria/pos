-- 0003_rls — the trust boundary (architecture §1.3, §11.2).
--
-- Client-side permission checks are UX. This file is security. Every policy
-- reads claims that the custom access token hook (0004) stamped into the JWT
-- at login, so no policy needs a per-row join and none of them are expensive.
--
-- There are deliberately NO update or delete policies on transactional
-- tables. Sales, payments and stock movements are immutable events; a
-- correction is a compensating row, which is also what makes sync merge-free
-- (architecture §1.4).

-- ── Claim helpers ─────────────────────────────────────────────────────────

create or replace function auth.has_perm(p text)
returns boolean
language sql
stable
as $$
  select coalesce(auth.jwt() -> 'app_metadata' -> 'permissions' ? p, false);
$$;

create or replace function auth.in_store(s uuid)
returns boolean
language sql
stable
as $$
  select coalesce(
    auth.jwt() -> 'app_metadata' -> 'store_ids' ? s::text,
    false
  );
$$;

grant execute on function auth.has_perm(text) to authenticated;
grant execute on function auth.in_store(uuid) to authenticated;

-- ── Enable RLS everywhere ─────────────────────────────────────────────────
-- A table with RLS enabled and no policy denies everything, which is the
-- correct default for anything not explicitly opened below.

alter table public.stores            enable row level security;
alter table public.terminals         enable row level security;
alter table public.permissions       enable row level security;
alter table public.roles             enable row level security;
alter table public.role_permissions  enable row level security;
alter table public.employees         enable row level security;
alter table public.user_store_roles  enable row level security;
alter table public.tax_codes         enable row level security;
alter table public.categories        enable row level security;
alter table public.products          enable row level security;
alter table public.product_barcodes  enable row level security;
alter table public.product_prices    enable row level security;
alter table public.promotions        enable row level security;
alter table public.register_sessions enable row level security;
alter table public.sales             enable row level security;
alter table public.sale_lines        enable row level security;
alter table public.payment_attempts  enable row level security;
alter table public.payments          enable row level security;
alter table public.stock_ledger      enable row level security;
alter table public.stock_levels      enable row level security;
alter table public.cash_movements    enable row level security;
alter table public.audit_log         enable row level security;
alter table public.unknown_scans     enable row level security;

-- ── Column-level grants ───────────────────────────────────────────────────
-- RLS filters rows, not columns. Cost is a column, so keeping it away from a
-- cashier is a GRANT problem: revoke the table-wide grant and hand back every
-- column except `cost`. A cashier selecting cost gets 42501 insufficient
-- privilege from Postgres itself, before any policy is consulted.

revoke all on public.product_prices from authenticated;
grant select (id, product_id, store_id, price, valid_from, valid_to,
              updated_at, deleted_at)
    on public.product_prices to authenticated;

-- The argon2id PIN hash is never handed to a client. Verification happens in
-- the authenticate-pin Edge Function, which runs with the service role.
revoke all on public.employees from authenticated;
grant select (user_id, employee_code, full_name, status, created_at, updated_at)
    on public.employees to authenticated;

-- ── Reference data: readable inside your own store ────────────────────────

create policy stores_select on public.stores
    for select to authenticated
    using (auth.in_store(id));

create policy terminals_select on public.terminals
    for select to authenticated
    using (auth.in_store(store_id));

-- The permission catalogue itself is not secret; knowing that
-- `report.margin` exists grants nobody anything.
create policy permissions_select on public.permissions
    for select to authenticated using (true);
create policy roles_select on public.roles
    for select to authenticated using (true);
create policy role_permissions_select on public.role_permissions
    for select to authenticated using (true);

create policy employees_select_self_or_manager on public.employees
    for select to authenticated
    using (
        user_id = auth.uid()
        or exists (
            select 1 from public.user_store_roles usr
             where usr.user_id = public.employees.user_id
               and auth.in_store(usr.store_id)
               and auth.has_perm('user.manage')
        )
    );

create policy user_store_roles_select on public.user_store_roles
    for select to authenticated
    using (user_id = auth.uid() or (auth.in_store(store_id) and auth.has_perm('user.manage')));

create policy tax_codes_select on public.tax_codes
    for select to authenticated using (auth.has_perm('product.read'));
create policy categories_select on public.categories
    for select to authenticated using (auth.has_perm('product.read'));
create policy products_select on public.products
    for select to authenticated using (auth.has_perm('product.read'));
create policy product_barcodes_select on public.product_barcodes
    for select to authenticated using (auth.has_perm('product.read'));

create policy product_prices_select on public.product_prices
    for select to authenticated
    using (auth.in_store(store_id) and auth.has_perm('product.read'));

create policy promotions_select on public.promotions
    for select to authenticated
    using (store_id is null or auth.in_store(store_id));

create policy products_write on public.products
    for insert to authenticated
    with check (auth.has_perm('product.create'));
create policy products_update on public.products
    for update to authenticated
    using (auth.has_perm('product.edit')) with check (auth.has_perm('product.edit'));

create policy product_barcodes_write on public.product_barcodes
    for insert to authenticated
    with check (auth.has_perm('product.create') or auth.has_perm('product.edit'));

-- ── Transactional: insert-only, scoped to your store ──────────────────────

create policy register_sessions_insert on public.register_sessions
    for insert to authenticated
    with check (auth.in_store(store_id) and auth.has_perm('sale.create')
                and user_id = auth.uid());

create policy register_sessions_select on public.register_sessions
    for select to authenticated
    using (auth.in_store(store_id)
           and (user_id = auth.uid() or auth.has_perm('report.sales.store')));

-- Closing a session is the one permitted mutation of an open row, and it is
-- gated on shift.close rather than on ownership.
create policy register_sessions_close on public.register_sessions
    for update to authenticated
    using (auth.in_store(store_id) and auth.has_perm('shift.close'))
    with check (auth.in_store(store_id) and auth.has_perm('shift.close'));

create policy sales_insert on public.sales
    for insert to authenticated
    with check (auth.in_store(store_id)
                and auth.has_perm('sale.create')
                and cashier_id = auth.uid());

create policy sales_select on public.sales
    for select to authenticated
    using (auth.in_store(store_id)
           and (cashier_id = auth.uid() or auth.has_perm('report.sales.store')));

-- No update or delete policy on sales. A void is a new row referencing
-- original_sale_id, so the history stays intact and the sync stays merge-free.

create policy sale_lines_insert on public.sale_lines
    for insert to authenticated
    with check (exists (select 1 from public.sales s
                         where s.id = sale_id and auth.in_store(s.store_id)
                           and auth.has_perm('sale.create')));

create policy sale_lines_select on public.sale_lines
    for select to authenticated
    using (exists (select 1 from public.sales s
                    where s.id = sale_id and auth.in_store(s.store_id)));

create policy payment_attempts_insert on public.payment_attempts
    for insert to authenticated
    with check (exists (select 1 from public.sales s
                         where s.id = sale_id and auth.in_store(s.store_id)
                           and auth.has_perm('sale.create')));

create policy payment_attempts_select on public.payment_attempts
    for select to authenticated
    using (exists (select 1 from public.sales s
                    where s.id = sale_id and auth.in_store(s.store_id)));

create policy payments_insert on public.payments
    for insert to authenticated
    with check (exists (select 1 from public.sales s
                         where s.id = sale_id and auth.in_store(s.store_id)
                           and auth.has_perm('sale.create')));

create policy payments_select on public.payments
    for select to authenticated
    using (exists (select 1 from public.sales s
                    where s.id = sale_id and auth.in_store(s.store_id)));

create policy stock_ledger_insert on public.stock_ledger
    for insert to authenticated
    with check (auth.in_store(store_id)
                and (auth.has_perm('sale.create')
                     or auth.has_perm('stock.receive')
                     or auth.has_perm('stock.count')
                     or auth.has_perm('stock.adjust')));

create policy stock_ledger_select on public.stock_ledger
    for select to authenticated
    using (auth.in_store(store_id) and auth.has_perm('product.read'));

-- Derived server-side; a terminal reads it and never writes it.
create policy stock_levels_select on public.stock_levels
    for select to authenticated
    using (auth.in_store(store_id) and auth.has_perm('product.read'));

create policy cash_movements_insert on public.cash_movements
    for insert to authenticated
    with check (auth.has_perm('cash.payout') and actor_id = auth.uid());

create policy cash_movements_select on public.cash_movements
    for select to authenticated
    using (exists (select 1 from public.register_sessions rs
                    where rs.id = session_id and auth.in_store(rs.store_id)));

-- Anyone may write an audit row — that is the point of an audit log. Reading
-- it is a manager's privilege.
create policy audit_log_insert on public.audit_log
    for insert to authenticated
    with check (store_id is null or auth.in_store(store_id));

create policy audit_log_select on public.audit_log
    for select to authenticated
    using (auth.in_store(store_id) and auth.has_perm('user.manage'));

create policy unknown_scans_insert on public.unknown_scans
    for insert to authenticated
    with check (auth.in_store(store_id) and auth.has_perm('sale.create'));

create policy unknown_scans_select on public.unknown_scans
    for select to authenticated
    using (auth.in_store(store_id) and auth.has_perm('product.read'));

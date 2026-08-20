-- 0001_core — reference tables, identity, catalog and the transactional
-- ledger. Mirrors the local SQLite schema in app/data/migrations/001_init.sql;
-- the two must stay in step or sync payloads will not validate.
--
-- Money is INTEGER minor units (paise). Quantities are INTEGER thousandths.
-- numeric/float never appears in a money column (architecture §6).

-- gen_random_uuid() is core since Postgres 13, so no extension is needed.
-- Note that terminals generate their own UUID v7 keys anyway; these
-- defaults only serve rows created server-side (architecture §7).

-- ── Stores and terminals ──────────────────────────────────────────────────

create table public.stores (
    id         uuid primary key default gen_random_uuid(),
    code       text not null unique,
    name       text not null,
    gstin      text,
    is_active  boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- One VPA per terminal: settlement matching against a bank statement is far
-- easier when each till has its own payee address (execution plan §2).
create table public.terminals (
    id         uuid primary key default gen_random_uuid(),
    store_id   uuid not null references public.stores(id) on delete restrict,
    code       text not null,
    name       text,
    upi_vpa    text,
    is_active  boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (store_id, code)
);

-- ── Identity, roles and permissions ───────────────────────────────────────

create table public.permissions (
    key         text primary key,
    description text not null
);

create table public.roles (
    key         text primary key,
    name        text not null,
    description text
);

create table public.role_permissions (
    role_key       text not null references public.roles(key) on delete cascade,
    permission_key text not null references public.permissions(key) on delete cascade,
    primary key (role_key, permission_key)
);

-- The employee record behind an auth.users row. `pin_hash` is argon2id and is
-- never readable by the `authenticated` role — see 0003_rls.sql. PIN
-- verification happens inside the authenticate-pin Edge Function.
create table public.employees (
    user_id       uuid primary key references auth.users(id) on delete cascade,
    employee_code text not null unique,
    full_name     text not null,
    pin_hash      text not null,
    status        text not null default 'active'
                  check (status in ('active', 'suspended', 'terminated')),
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- Roles are assigned per store (architecture §11.1).
create table public.user_store_roles (
    user_id  uuid not null references public.employees(user_id) on delete cascade,
    store_id uuid not null references public.stores(id) on delete cascade,
    role_key text not null references public.roles(key) on delete restrict,
    primary key (user_id, store_id, role_key)
);

create index ix_user_store_roles_user on public.user_store_roles(user_id);

-- ── Catalog ───────────────────────────────────────────────────────────────

create table public.tax_codes (
    code         text primary key,
    name         text not null,
    rate_bp      integer not null check (rate_bp >= 0),   -- 18% GST = 1800
    is_inclusive boolean not null default true,
    updated_at   timestamptz not null default now(),
    deleted_at   timestamptz
);

create table public.categories (
    id         uuid primary key default gen_random_uuid(),
    name       text not null,
    parent_id  uuid references public.categories(id),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz
);

create table public.products (
    id          uuid primary key default gen_random_uuid(),
    sku         text not null unique,
    name        text not null,
    short_name  text,
    category_id uuid references public.categories(id),
    uom         text not null default 'each',
    is_weighed  boolean not null default false,
    track_stock boolean not null default true,
    tax_code    text not null references public.tax_codes(code),
    is_active   boolean not null default true,
    updated_at  timestamptz not null default now(),
    deleted_at  timestamptz
);

create index ix_products_updated_at on public.products(updated_at);

create table public.product_barcodes (
    id         uuid primary key default gen_random_uuid(),
    product_id uuid not null references public.products(id) on delete cascade,
    barcode    text not null,
    symbology  text not null,
    pack_size  integer not null default 1 check (pack_size >= 1),
    is_primary boolean not null default false,
    updated_at timestamptz not null default now(),
    deleted_at timestamptz
);

-- The most common catalog defect is the same code on two products. The
-- database refuses it rather than letting the till pick one at random.
create unique index ux_product_barcodes_barcode
    on public.product_barcodes(barcode) where deleted_at is null;
create index ix_product_barcodes_updated_at on public.product_barcodes(updated_at);

create table public.product_prices (
    id         uuid primary key default gen_random_uuid(),
    product_id uuid not null references public.products(id) on delete cascade,
    store_id   uuid not null references public.stores(id) on delete cascade,
    price      integer not null check (price >= 0),
    -- Margin data. Column-level grants keep this out of a cashier's reach.
    cost       integer check (cost >= 0),
    valid_from timestamptz not null default now(),
    valid_to   timestamptz,
    updated_at timestamptz not null default now(),
    deleted_at timestamptz
);

create index ix_product_prices_lookup on public.product_prices(store_id, product_id);
create index ix_product_prices_updated_at on public.product_prices(updated_at);

create table public.promotions (
    id         uuid primary key default gen_random_uuid(),
    store_id   uuid references public.stores(id) on delete cascade,
    type       text not null,
    scope_json jsonb not null default '{}'::jsonb,
    value      integer not null,
    starts_at  timestamptz,
    ends_at    timestamptz,
    priority   integer not null default 0,
    updated_at timestamptz not null default now(),
    deleted_at timestamptz
);

-- ── Transactional (append-only) ───────────────────────────────────────────

create table public.register_sessions (
    id                 uuid primary key,
    store_id           uuid not null references public.stores(id),
    terminal_id        uuid not null references public.terminals(id),
    user_id            uuid not null references public.employees(user_id),
    opened_at          timestamptz not null,
    opening_float      integer not null default 0,
    closed_at          timestamptz,
    counted_cash       integer,
    expected_cash      integer,
    variance           integer,
    status             text not null default 'open',
    server_received_at timestamptz not null default now()
);

create table public.sales (
    id                  uuid primary key,
    store_id            uuid not null references public.stores(id),
    terminal_id         uuid not null references public.terminals(id),
    session_id          uuid references public.register_sessions(id),
    receipt_no          text,
    cashier_id          uuid not null references public.employees(user_id),
    type                text not null default 'sale',
    status              text not null
                        check (status in ('parked','completed','voided','requires_review')),
    subtotal            integer not null default 0,
    discount_total      integer not null default 0,
    tax_total           integer not null default 0,
    rounding_adjustment integer not null default 0,
    grand_total         integer not null default 0,
    original_sale_id    uuid references public.sales(id),
    client_created_at   timestamptz not null,
    -- Reporting uses this, never terminal time: clocks drift and cashiers
    -- change them (architecture §9.4).
    server_received_at  timestamptz not null default now()
);

create unique index ux_sales_receipt_no
    on public.sales(store_id, receipt_no) where receipt_no is not null;
create index ix_sales_received on public.sales(store_id, server_received_at);

create table public.sale_lines (
    id              uuid primary key,
    sale_id         uuid not null references public.sales(id) on delete cascade,
    line_no         integer not null,
    product_id      uuid not null references public.products(id),
    barcode_scanned text,
    description     text not null,
    qty_milli       integer not null,
    unit_price      integer not null,
    discount_amount integer not null default 0,
    tax_amount      integer not null default 0,
    line_total      integer not null,
    overridden_by   uuid references public.employees(user_id),
    override_reason text,
    unique (sale_id, line_no)
);

create table public.payment_attempts (
    id             uuid primary key,
    sale_id        uuid not null references public.sales(id) on delete cascade,
    method         text not null check (method in ('cash','upi')),
    amount         integer not null,
    state          text not null
                   check (state in ('pending','approved','declined','cancelled','expired','unknown')),
    txn_ref        text,
    qr_payload     text,
    started_at     timestamptz not null,
    expires_at     timestamptz,
    resolved_at    timestamptz,
    failure_reason text
);

create table public.payments (
    id                  uuid primary key,
    sale_id             uuid not null references public.sales(id) on delete cascade,
    attempt_id          uuid references public.payment_attempts(id),
    method              text not null check (method in ('cash','upi')),
    amount              integer not null,
    status              text not null check (status in ('approved','voided')),
    tender_ref          text,
    utr                 text,
    confirmation_method text not null
                        check (confirmation_method in
                               ('immediate','manual_attestation','psp_webhook')),
    confirmed_by        uuid references public.employees(user_id),
    -- Stays 0 until settlement matches. Unverified UPI is a distinct figure on
    -- the Z-report, never folded into takings (architecture §13.5).
    verified            boolean not null default false,
    server_received_at  timestamptz not null default now()
);

create index ix_payments_sale on public.payments(sale_id);

-- Stock is a ledger of deltas, never a counter (architecture §1.5).
create table public.stock_ledger (
    id                 uuid primary key,
    store_id           uuid not null references public.stores(id),
    product_id         uuid not null references public.products(id),
    delta_milli        integer not null,
    reason             text not null,
    ref_type           text,
    ref_id             uuid,
    occurred_at        timestamptz not null,
    terminal_id        uuid references public.terminals(id),
    user_id            uuid references public.employees(user_id),
    server_received_at timestamptz not null default now()
);

create index ix_stock_ledger_product on public.stock_ledger(store_id, product_id, occurred_at);

-- Derived server-side from the ledger; never pushed by a terminal.
create table public.stock_levels (
    store_id      uuid not null references public.stores(id),
    product_id    uuid not null references public.products(id),
    on_hand       integer not null default 0,
    reorder_point integer not null default 0,
    updated_at    timestamptz not null default now(),
    primary key (store_id, product_id)
);

create table public.cash_movements (
    id          uuid primary key,
    session_id  uuid not null references public.register_sessions(id),
    direction   text not null check (direction in ('in','out')),
    amount      integer not null,
    reason      text,
    actor_id    uuid not null references public.employees(user_id),
    approved_by uuid references public.employees(user_id),
    occurred_at timestamptz not null
);

create table public.audit_log (
    id                 uuid primary key,
    store_id           uuid references public.stores(id),
    actor_id           uuid references public.employees(user_id),
    approver_id        uuid references public.employees(user_id),
    action             text not null,
    entity             text,
    entity_id          uuid,
    before_json        jsonb,
    after_json         jsonb,
    occurred_at        timestamptz not null,
    server_received_at timestamptz not null default now()
);

create table public.unknown_scans (
    id          uuid primary key,
    store_id    uuid not null references public.stores(id),
    barcode     text not null,
    scanned_at  timestamptz not null,
    terminal_id uuid references public.terminals(id),
    resolved    boolean not null default false
);

-- ── stock_levels maintenance ──────────────────────────────────────────────
-- The aggregate is derived here so that two terminals selling the last unit
-- offline reconcile to the truth rather than to whichever synced last.

create or replace function public.apply_stock_delta()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.stock_levels (store_id, product_id, on_hand, updated_at)
    values (new.store_id, new.product_id, new.delta_milli, now())
    on conflict (store_id, product_id) do update
        set on_hand    = public.stock_levels.on_hand + excluded.on_hand,
            updated_at = now();
    return new;
end;
$$;

create trigger stock_ledger_apply
    after insert on public.stock_ledger
    for each row execute function public.apply_stock_delta();

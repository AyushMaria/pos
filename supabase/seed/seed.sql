-- Development seed. One store, one terminal, three employees, a handful of
-- products. Never applied to production.
--
-- The PIN hashes below are argon2id with the parameters committed in
-- app/config.py (m=65536, t=12, p=4). They are published in this repository
-- and are therefore development credentials only.
--
--   C001 / 4913   Anita Rao      cashier
--   S001 / 7261   Vikram Shah    supervisor
--   M001 / 5820   Priya Nair     manager
--
-- Apply after the migrations:  supabase db reset

insert into public.stores (id, code, name, gstin) values
    ('018f0000-0000-7000-8000-000000000100', 'ST01', 'Demo Kirana', '29ABCDE1234F1Z5')
on conflict (id) do nothing;

insert into public.terminals (id, store_id, code, name, upi_vpa) values
    ('018f0000-0000-7000-8000-000000000200',
     '018f0000-0000-7000-8000-000000000100',
     'T1', 'Front counter', 'demokirana.t1@okhdfcbank')
on conflict (id) do nothing;

-- ── Staff ─────────────────────────────────────────────────────────────────
-- authenticate-pin signs employees in by email via generateLink, so every
-- employee needs an auth.users row. The address is synthetic; nobody reads it.

insert into auth.users (id, email) values
    ('018f0000-0000-7000-8000-000000000001', 'c001@st01.pos.local'),
    ('018f0000-0000-7000-8000-000000000002', 's001@st01.pos.local'),
    ('018f0000-0000-7000-8000-000000000003', 'm001@st01.pos.local')
on conflict (id) do nothing;

insert into public.employees (user_id, employee_code, full_name, pin_hash, status) values
    ('018f0000-0000-7000-8000-000000000001', 'C001', 'Anita Rao',
     '$argon2id$v=19$m=65536,t=12,p=4$3IE7eHs9K9YpmpfFUcBi3w$A7U/ORMqOsDsKHIB/YswSGy5k4BiUkovJN4CiH8VVuk',
     'active'),
    ('018f0000-0000-7000-8000-000000000002', 'S001', 'Vikram Shah',
     '$argon2id$v=19$m=65536,t=12,p=4$DZcEK7lNz6/8JjDFsqqsMw$f1Vlicu3pnGDrfgBbbYXmcH0UjFxe5uOydD/YmKwGRw',
     'active'),
    ('018f0000-0000-7000-8000-000000000003', 'M001', 'Priya Nair',
     '$argon2id$v=19$m=65536,t=12,p=4$Szb2KNf5F61GESkNsgVKGw$ULij2XFBbN7vEjvdX1tcUxpEQdo2lt1lkGfY0KyE+ms',
     'active')
on conflict (user_id) do update set
    employee_code = excluded.employee_code,
    full_name     = excluded.full_name,
    pin_hash      = excluded.pin_hash,
    status        = excluded.status;

insert into public.user_store_roles (user_id, store_id, role_key) values
    ('018f0000-0000-7000-8000-000000000001',
     '018f0000-0000-7000-8000-000000000100', 'cashier'),
    ('018f0000-0000-7000-8000-000000000002',
     '018f0000-0000-7000-8000-000000000100', 'supervisor'),
    ('018f0000-0000-7000-8000-000000000003',
     '018f0000-0000-7000-8000-000000000100', 'manager')
on conflict do nothing;

-- ── Catalog ───────────────────────────────────────────────────────────────
-- GST-inclusive pricing, rates in basis points. A mixed-rate basket is the
-- week-5 risk checkpoint, so the seed deliberately spans three rates.

insert into public.tax_codes (code, name, rate_bp, is_inclusive) values
    ('GST0',  'GST 0%',   0,    true),
    ('GST5',  'GST 5%',   500,  true),
    ('GST12', 'GST 12%',  1200, true),
    ('GST18', 'GST 18%',  1800, true)
on conflict (code) do nothing;

insert into public.products (id, sku, name, short_name, uom, is_weighed, tax_code) values
    ('018f0000-0000-7000-8000-000000001001', 'SKU-1001',
     'Aashirvaad Atta 5 kg',      'Atta 5kg',   'each', false, 'GST5'),
    ('018f0000-0000-7000-8000-000000001002', 'SKU-1002',
     'Amul Taaza Milk 1 L',       'Milk 1L',    'each', false, 'GST0'),
    ('018f0000-0000-7000-8000-000000001003', 'SKU-1003',
     'Colgate Strong Teeth 200 g','Colgate',    'each', false, 'GST18'),
    ('018f0000-0000-7000-8000-000000001004', 'SKU-1004',
     'Tomatoes (loose)',          'Tomato',     'kg',   true,  'GST0'),
    ('018f0000-0000-7000-8000-000000001005', 'SKU-1005',
     'Parle-G Biscuits 250 g',    'Parle-G',    'each', false, 'GST12')
on conflict (id) do nothing;

-- pack_size drives the receiving multiplier: scanning the ITF-14 case code
-- for Parle-G receives 24 units, not one (architecture §10.2).
insert into public.product_barcodes
    (product_id, barcode, symbology, pack_size, is_primary) values
    ('018f0000-0000-7000-8000-000000001001', '8901030865275', 'EAN13',  1,  true),
    ('018f0000-0000-7000-8000-000000001002', '8901262010016', 'EAN13',  1,  true),
    ('018f0000-0000-7000-8000-000000001003', '8901314700100', 'EAN13',  1,  true),
    ('018f0000-0000-7000-8000-000000001005', '8901719101250', 'EAN13',  1,  true),
    ('018f0000-0000-7000-8000-000000001005', '18901719101257', 'ITF14', 24, false)
on conflict do nothing;

insert into public.product_prices (product_id, store_id, price, cost) values
    ('018f0000-0000-7000-8000-000000001001',
     '018f0000-0000-7000-8000-000000000100', 27500, 24200),
    ('018f0000-0000-7000-8000-000000001002',
     '018f0000-0000-7000-8000-000000000100',  3300,  3000),
    ('018f0000-0000-7000-8000-000000001003',
     '018f0000-0000-7000-8000-000000000100', 11500,  8900),
    ('018f0000-0000-7000-8000-000000001004',
     '018f0000-0000-7000-8000-000000000100',  4000,  2800),
    ('018f0000-0000-7000-8000-000000001005',
     '018f0000-0000-7000-8000-000000000100',  2500,  2050)
on conflict do nothing;

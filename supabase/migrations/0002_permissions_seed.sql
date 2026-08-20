-- 0002_permissions_seed — GENERATED FILE, DO NOT EDIT BY HAND.
--
-- Source of truth: app/domain/permissions.py (architecture §11.1).
-- Regenerate with:  python scripts/gen_permission_seed.py
-- CI fails if this file and the Python matrix disagree.

insert into public.permissions (key, description) values
    ('cash.payout', 'Take cash out of the drawer'),
    ('payment.attest', 'Attest that a UPI payment was received'),
    ('price.override', 'Override the price on a line'),
    ('product.create', 'Create a product'),
    ('product.edit', 'Edit a product'),
    ('product.read', 'Read the product catalog'),
    ('report.margin', 'Read cost and margin figures'),
    ('report.sales.store', 'Read store sales reports'),
    ('sale.create', 'Ring up a sale'),
    ('sale.discount.line', 'Discount a line up to 10%'),
    ('sale.discount.unlimited', 'Discount a line by any amount'),
    ('sale.refund', 'Refund a sale'),
    ('sale.review.resolve', 'Resolve a sale held for review'),
    ('sale.void', 'Void a completed sale'),
    ('settings.manage', 'Change system settings'),
    ('shift.close', 'Close a register session'),
    ('stock.adjust', 'Adjust stock outside a count'),
    ('stock.count', 'Perform a stock count'),
    ('stock.receive', 'Receive stock against a purchase order'),
    ('user.manage', 'Manage employees and their roles')
on conflict (key) do update set description = excluded.description;

insert into public.roles (key, name, description) values
    ('cashier', 'Cashier', 'Rings up sales and attests UPI receipts'),
    ('supervisor', 'Supervisor', 'Authorises voids, discounts and shift close'),
    ('inventory', 'Inventory', 'Maintains the catalog and receives stock'),
    ('manager', 'Manager', 'Full store control including margin reporting'),
    ('admin', 'Admin', 'System settings in addition to manager rights')
on conflict (key) do update set
    name = excluded.name, description = excluded.description;

-- Rebuilt wholesale so that a permission removed from the matrix is
-- actually revoked, not merely absent from the insert.
delete from public.role_permissions;

insert into public.role_permissions (role_key, permission_key) values
    ('cashier', 'payment.attest'),
    ('cashier', 'product.read'),
    ('cashier', 'sale.create'),
    ('supervisor', 'cash.payout'),
    ('supervisor', 'payment.attest'),
    ('supervisor', 'price.override'),
    ('supervisor', 'product.read'),
    ('supervisor', 'report.sales.store'),
    ('supervisor', 'sale.create'),
    ('supervisor', 'sale.discount.line'),
    ('supervisor', 'sale.refund'),
    ('supervisor', 'sale.review.resolve'),
    ('supervisor', 'sale.void'),
    ('supervisor', 'shift.close'),
    ('inventory', 'product.create'),
    ('inventory', 'product.edit'),
    ('inventory', 'product.read'),
    ('inventory', 'stock.count'),
    ('inventory', 'stock.receive'),
    ('manager', 'cash.payout'),
    ('manager', 'payment.attest'),
    ('manager', 'price.override'),
    ('manager', 'product.create'),
    ('manager', 'product.edit'),
    ('manager', 'product.read'),
    ('manager', 'report.margin'),
    ('manager', 'report.sales.store'),
    ('manager', 'sale.create'),
    ('manager', 'sale.discount.line'),
    ('manager', 'sale.discount.unlimited'),
    ('manager', 'sale.refund'),
    ('manager', 'sale.review.resolve'),
    ('manager', 'sale.void'),
    ('manager', 'shift.close'),
    ('manager', 'stock.adjust'),
    ('manager', 'stock.count'),
    ('manager', 'stock.receive'),
    ('manager', 'user.manage'),
    ('admin', 'cash.payout'),
    ('admin', 'payment.attest'),
    ('admin', 'price.override'),
    ('admin', 'product.create'),
    ('admin', 'product.edit'),
    ('admin', 'product.read'),
    ('admin', 'report.margin'),
    ('admin', 'report.sales.store'),
    ('admin', 'sale.create'),
    ('admin', 'sale.discount.line'),
    ('admin', 'sale.discount.unlimited'),
    ('admin', 'sale.refund'),
    ('admin', 'sale.review.resolve'),
    ('admin', 'sale.void'),
    ('admin', 'settings.manage'),
    ('admin', 'shift.close'),
    ('admin', 'stock.adjust'),
    ('admin', 'stock.count'),
    ('admin', 'stock.receive'),
    ('admin', 'user.manage')
on conflict do nothing;

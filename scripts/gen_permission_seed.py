"""Generate the Postgres roles/permissions seed from the domain matrix.

``app/domain/permissions.py`` is the single source of truth (architecture
§11.1). Hand-maintaining the same table in SQL guarantees the two drift, and a
drifted permission table means the UI hides a control the database happily
allows — or worse, the reverse. So the SQL is generated, committed, and a test
fails the build if the committed file stops matching.

    python scripts/gen_permission_seed.py          # rewrite the migration
    python scripts/gen_permission_seed.py --check  # exit 1 if stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.domain import permissions as perms  # noqa: E402

TARGET = REPO_ROOT / "supabase" / "migrations" / "0002_permissions_seed.sql"

DESCRIPTIONS = {
    perms.SALE_CREATE: "Ring up a sale",
    perms.SALE_DISCOUNT_LINE: "Discount a line up to 10%",
    perms.SALE_DISCOUNT_UNLIMITED: "Discount a line by any amount",
    perms.SALE_VOID: "Void a completed sale",
    perms.SALE_REFUND: "Refund a sale",
    perms.SALE_REVIEW_RESOLVE: "Resolve a sale held for review",
    perms.PRICE_OVERRIDE: "Override the price on a line",
    perms.PRODUCT_READ: "Read the product catalog",
    perms.PRODUCT_CREATE: "Create a product",
    perms.PRODUCT_EDIT: "Edit a product",
    perms.STOCK_RECEIVE: "Receive stock against a purchase order",
    perms.STOCK_COUNT: "Perform a stock count",
    perms.STOCK_ADJUST: "Adjust stock outside a count",
    perms.PAYMENT_ATTEST: "Attest that a UPI payment was received",
    perms.CASH_PAYOUT: "Take cash out of the drawer",
    perms.SHIFT_CLOSE: "Close a register session",
    perms.REPORT_SALES_STORE: "Read store sales reports",
    perms.REPORT_MARGIN: "Read cost and margin figures",
    perms.USER_MANAGE: "Manage employees and their roles",
    perms.SETTINGS_MANAGE: "Change system settings",
}

ROLE_NAMES = {
    perms.CASHIER: ("Cashier", "Rings up sales and attests UPI receipts"),
    perms.SUPERVISOR: ("Supervisor", "Authorises voids, discounts and shift close"),
    perms.INVENTORY: ("Inventory", "Maintains the catalog and receives stock"),
    perms.MANAGER: ("Manager", "Full store control including margin reporting"),
    perms.ADMIN: ("Admin", "System settings in addition to manager rights"),
}


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render() -> str:
    lines: list[str] = [
        "-- 0002_permissions_seed — GENERATED FILE, DO NOT EDIT BY HAND.",
        "--",
        "-- Source of truth: app/domain/permissions.py (architecture §11.1).",
        "-- Regenerate with:  python scripts/gen_permission_seed.py",
        "-- CI fails if this file and the Python matrix disagree.",
        "",
        "insert into public.permissions (key, description) values",
    ]

    perm_rows = [
        f"    ({_quote(key)}, {_quote(DESCRIPTIONS[key])})"
        for key in sorted(perms.ALL_PERMISSIONS)
    ]
    lines.append(",\n".join(perm_rows))
    lines += [
        "on conflict (key) do update set description = excluded.description;",
        "",
        "insert into public.roles (key, name, description) values",
    ]

    role_rows = [
        f"    ({_quote(role)}, {_quote(ROLE_NAMES[role][0])}, "
        f"{_quote(ROLE_NAMES[role][1])})"
        for role in perms.ROLES
    ]
    lines.append(",\n".join(role_rows))
    lines += [
        "on conflict (key) do update set",
        "    name = excluded.name, description = excluded.description;",
        "",
        "-- Rebuilt wholesale so that a permission removed from the matrix is",
        "-- actually revoked, not merely absent from the insert.",
        "delete from public.role_permissions;",
        "",
        "insert into public.role_permissions (role_key, permission_key) values",
    ]

    grant_rows = [
        f"    ({_quote(role)}, {_quote(key)})"
        for role in perms.ROLES
        for key in sorted(perms.ROLE_PERMISSIONS[role])
    ]
    lines.append(",\n".join(grant_rows))
    lines.append("on conflict do nothing;")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args()

    rendered = render()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print(
                f"{TARGET.relative_to(REPO_ROOT)} is out of date with "
                "app/domain/permissions.py.\n"
                "Run: python scripts/gen_permission_seed.py",
                file=sys.stderr,
            )
            return 1
        print("permission seed is up to date")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

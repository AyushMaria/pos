"""The permission model — architecture §11.1.

This module is the single source of truth for the matrix. The Postgres seed,
the SQLite seed, the FastAPI dependency and the generated TypeScript types are
all derived from it, so the three enforcement points (UI, API, RLS) cannot
drift apart. Never test a role string; always test a permission key.
"""

from __future__ import annotations

from typing import Final

# ── Permission keys ─────────────────────────────────────────────────────────

SALE_CREATE: Final = "sale.create"
SALE_DISCOUNT_LINE: Final = "sale.discount.line"
SALE_DISCOUNT_UNLIMITED: Final = "sale.discount.unlimited"
SALE_VOID: Final = "sale.void"
SALE_REFUND: Final = "sale.refund"
SALE_REVIEW_RESOLVE: Final = "sale.review.resolve"
PRICE_OVERRIDE: Final = "price.override"
PRODUCT_READ: Final = "product.read"
PRODUCT_CREATE: Final = "product.create"
PRODUCT_EDIT: Final = "product.edit"
STOCK_RECEIVE: Final = "stock.receive"
STOCK_COUNT: Final = "stock.count"
STOCK_ADJUST: Final = "stock.adjust"
PAYMENT_ATTEST: Final = "payment.attest"
CASH_PAYOUT: Final = "cash.payout"
SHIFT_CLOSE: Final = "shift.close"
REPORT_SALES_STORE: Final = "report.sales.store"
REPORT_MARGIN: Final = "report.margin"
USER_MANAGE: Final = "user.manage"
SETTINGS_MANAGE: Final = "settings.manage"

# ── Roles ───────────────────────────────────────────────────────────────────

CASHIER: Final = "cashier"
SUPERVISOR: Final = "supervisor"
INVENTORY: Final = "inventory"
MANAGER: Final = "manager"
ADMIN: Final = "admin"

ROLES: Final[tuple[str, ...]] = (CASHIER, SUPERVISOR, INVENTORY, MANAGER, ADMIN)

# ── The matrix ──────────────────────────────────────────────────────────────
# Transcribed from architecture §11.1. Roles are assigned *per store* via
# user_store_roles; `user.manage` is own-store-only for manager, which is a
# row-scope concern handled by RLS rather than by this table.

ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    CASHIER: frozenset(
        {
            SALE_CREATE,
            PRODUCT_READ,
            PAYMENT_ATTEST,
        }
    ),
    SUPERVISOR: frozenset(
        {
            SALE_CREATE,
            SALE_DISCOUNT_LINE,
            SALE_VOID,
            SALE_REFUND,
            SALE_REVIEW_RESOLVE,
            PRICE_OVERRIDE,
            PRODUCT_READ,
            PAYMENT_ATTEST,
            CASH_PAYOUT,
            SHIFT_CLOSE,
            REPORT_SALES_STORE,
        }
    ),
    INVENTORY: frozenset(
        {
            PRODUCT_READ,
            PRODUCT_CREATE,
            PRODUCT_EDIT,
            STOCK_RECEIVE,
            STOCK_COUNT,
        }
    ),
    MANAGER: frozenset(
        {
            SALE_CREATE,
            SALE_DISCOUNT_LINE,
            SALE_DISCOUNT_UNLIMITED,
            SALE_VOID,
            SALE_REFUND,
            SALE_REVIEW_RESOLVE,
            PRICE_OVERRIDE,
            PRODUCT_READ,
            PRODUCT_CREATE,
            PRODUCT_EDIT,
            STOCK_RECEIVE,
            STOCK_COUNT,
            STOCK_ADJUST,
            PAYMENT_ATTEST,
            CASH_PAYOUT,
            SHIFT_CLOSE,
            REPORT_SALES_STORE,
            REPORT_MARGIN,
            USER_MANAGE,
        }
    ),
    ADMIN: frozenset(
        {
            SALE_CREATE,
            SALE_DISCOUNT_LINE,
            SALE_DISCOUNT_UNLIMITED,
            SALE_VOID,
            SALE_REFUND,
            SALE_REVIEW_RESOLVE,
            PRICE_OVERRIDE,
            PRODUCT_READ,
            PRODUCT_CREATE,
            PRODUCT_EDIT,
            STOCK_RECEIVE,
            STOCK_COUNT,
            STOCK_ADJUST,
            PAYMENT_ATTEST,
            CASH_PAYOUT,
            SHIFT_CLOSE,
            REPORT_SALES_STORE,
            REPORT_MARGIN,
            USER_MANAGE,
            SETTINGS_MANAGE,
        }
    ),
}

ALL_PERMISSIONS: Final[frozenset[str]] = frozenset().union(*ROLE_PERMISSIONS.values())


def permissions_for(roles: frozenset[str]) -> frozenset[str]:
    """Union of the permissions granted by ``roles``. Unknown roles grant nothing."""
    granted: frozenset[str] = frozenset()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return granted


def has(permissions: frozenset[str], permission: str) -> bool:
    """Local permission test. UX only — Postgres RLS is the security boundary."""
    return permission in permissions

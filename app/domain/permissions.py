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

# ── What a supervisor may lend ──────────────────────────────────────────────
#
# A 90-second grant lives on this terminal. RLS only ever sees the JWT, and
# there is no way to mint a new one offline — which is precisely the case the
# phase 7 exit criteria insist must work. So a granted permission passes
# `require()` here and then meets Postgres at the sync boundary holding
# nothing but the cashier's own claim.
#
# **The rule: a permission is overridable only if the write it authorises is
# already accepted under the cashier's own claim.** Anything else produces the
# worst failure this system can have — the sale completes, the customer
# leaves, and the row quarantines hours later with nobody watching.
#
# Checked against the real policies rather than reasoned about, in
# `tests/test_rls.py::test_the_overridable_set_is_exactly_what_rls_accepts`.
# The four below are accepted because RLS never asks *which* key justified a
# row; it asks for `sale.create`, which the cashier already holds:
#
#   sale.discount.line       sale_lines   -> sale_lines_insert  (sale.create)
#   sale.discount.unlimited  sale_lines   -> sale_lines_insert  (sale.create)
#   price.override           sale_lines   -> sale_lines_insert  (sale.create)
#   sale.void                sales        -> sales_insert       (sale.create
#                                             + cashier_id = auth.uid())
#
# `cash.payout` is the counter-example and the reason this is a rule rather
# than a list: `cash_movements_insert` asks for `cash.payout` by name, so a
# granted payout would be refused at push time. `shift.close` and
# `sale.refund` have no write path yet; when they get one, the test decides.
OVERRIDABLE: Final[frozenset[str]] = frozenset(
    {
        SALE_DISCOUNT_LINE,
        SALE_DISCOUNT_UNLIMITED,
        PRICE_OVERRIDE,
        SALE_VOID,
    }
)


def is_overridable(permission: str) -> bool:
    """May a supervisor lend this for ninety seconds?

    False for anything unknown, so a typo grants nothing.
    """
    return permission in OVERRIDABLE


def permissions_for(roles: frozenset[str]) -> frozenset[str]:
    """Union of the permissions granted by ``roles``. Unknown roles grant nothing."""
    granted: frozenset[str] = frozenset()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return granted


def has(permissions: frozenset[str], permission: str) -> bool:
    """Local permission test. UX only — Postgres RLS is the security boundary."""
    return permission in permissions

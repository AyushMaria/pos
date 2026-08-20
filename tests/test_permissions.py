"""The permission matrix, asserted directly against architecture §11.1.

This table is transcribed from the architecture document rather than derived
from ROLE_PERMISSIONS, so a mistake in the matrix fails here instead of being
faithfully reproduced by the test.
"""

from __future__ import annotations

import pytest

from app.domain import permissions as perms

# permission -> roles that hold it, per architecture §11.1
EXPECTED: dict[str, set[str]] = {
    perms.SALE_CREATE: {"cashier", "supervisor", "manager", "admin"},
    perms.SALE_DISCOUNT_LINE: {"supervisor", "manager", "admin"},
    perms.SALE_DISCOUNT_UNLIMITED: {"manager", "admin"},
    perms.SALE_VOID: {"supervisor", "manager", "admin"},
    perms.SALE_REFUND: {"supervisor", "manager", "admin"},
    perms.PRICE_OVERRIDE: {"supervisor", "manager", "admin"},
    perms.PRODUCT_READ: {"cashier", "supervisor", "inventory", "manager", "admin"},
    perms.PRODUCT_CREATE: {"inventory", "manager", "admin"},
    perms.PRODUCT_EDIT: {"inventory", "manager", "admin"},
    perms.STOCK_RECEIVE: {"inventory", "manager", "admin"},
    perms.STOCK_COUNT: {"inventory", "manager", "admin"},
    perms.STOCK_ADJUST: {"manager", "admin"},
    perms.PAYMENT_ATTEST: {"cashier", "supervisor", "manager", "admin"},
    perms.SALE_REVIEW_RESOLVE: {"supervisor", "manager", "admin"},
    perms.CASH_PAYOUT: {"supervisor", "manager", "admin"},
    perms.SHIFT_CLOSE: {"supervisor", "manager", "admin"},
    perms.REPORT_SALES_STORE: {"supervisor", "manager", "admin"},
    perms.REPORT_MARGIN: {"manager", "admin"},
    perms.USER_MANAGE: {"manager", "admin"},
    perms.SETTINGS_MANAGE: {"admin"},
}


@pytest.mark.parametrize("permission", sorted(EXPECTED))
@pytest.mark.parametrize("role", perms.ROLES)
def test_matrix_matches_architecture(role: str, permission: str) -> None:
    should_have = role in EXPECTED[permission]
    does_have = permission in perms.ROLE_PERMISSIONS[role]
    assert does_have is should_have, (
        f"{role} should {'have' if should_have else 'not have'} {permission}"
    )


def test_matrix_covers_every_permission() -> None:
    assert set(EXPECTED) == set(perms.ALL_PERMISSIONS)


def test_cashier_cannot_see_margin() -> None:
    """The rule phase 1 has to prove end to end, stated once at the source."""
    assert perms.REPORT_MARGIN not in perms.ROLE_PERMISSIONS[perms.CASHIER]


def test_permissions_for_unions_roles() -> None:
    combined = perms.permissions_for(frozenset({perms.CASHIER, perms.INVENTORY}))
    assert perms.SALE_CREATE in combined
    assert perms.STOCK_RECEIVE in combined
    assert perms.STOCK_ADJUST not in combined


def test_unknown_role_grants_nothing() -> None:
    assert perms.permissions_for(frozenset({"owner's nephew"})) == frozenset()


def test_no_role_string_checks_in_the_codebase() -> None:
    """Architecture §11.1: never ``if user.role == "manager"``.

    Cheap to enforce, and the failure mode it prevents — a role comparison
    that quietly bypasses the matrix — is expensive to find later.
    """
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    pattern = re.compile(r"""\.role\s*==\s*['"]""")

    offenders = [
        path
        for path in app_dir.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"role-string comparison found in: {offenders}"

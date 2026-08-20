"""Session and snapshot rules — architecture §11.3, §11.4.

The two rules that matter: an outage never stops the till trading, and an
expired snapshot always does.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.domain import permissions as perms
from app.domain.identity import (
    OVERRIDE_GRANT_TTL,
    SNAPSHOT_TTL,
    CachedIdentity,
    PermissionDenied,
    Session,
    SnapshotExpired,
    snapshot_expiry,
)

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)


def cashier(**overrides: object) -> CachedIdentity:
    signed = overrides.pop("signed_at", NOW - timedelta(days=1))
    assert isinstance(signed, datetime)
    defaults = {
        "user_id": "u1",
        "employee_code": "C001",
        "full_name": "Anita Rao",
        "store_id": "s1",
        "pin_hash": "$argon2id$...",
        "status": "active",
        "roles": frozenset({perms.CASHIER}),
        "permissions": perms.permissions_for(frozenset({perms.CASHIER})),
        "snapshot_signed_at": signed,
        "snapshot_expires_at": snapshot_expiry(signed),
    }
    defaults.update(overrides)
    return CachedIdentity(**defaults)  # type: ignore[arg-type]


def test_fresh_snapshot_opens_a_session() -> None:
    session = cashier().to_session(now=NOW)
    assert session.offline is True
    assert perms.SALE_CREATE in session.permissions


def test_snapshot_still_valid_one_minute_before_expiry() -> None:
    identity = cashier(signed_at=NOW - SNAPSHOT_TTL + timedelta(minutes=1))
    assert identity.is_usable(now=NOW) is True


def test_snapshot_expires_exactly_at_the_ttl() -> None:
    identity = cashier(signed_at=NOW - SNAPSHOT_TTL)
    assert identity.is_usable(now=NOW) is False
    with pytest.raises(SnapshotExpired):
        identity.to_session(now=NOW)


def test_terminated_employee_cannot_open_a_session() -> None:
    identity = cashier(status="terminated")
    assert identity.is_usable(now=NOW) is False
    with pytest.raises(SnapshotExpired):
        identity.to_session(now=NOW)


def test_ttl_is_fourteen_days() -> None:
    assert SNAPSHOT_TTL == timedelta(days=14)


# ── Overrides ───────────────────────────────────────────────────────────────


def session_for(role: str) -> Session:
    return Session(
        user_id="u1",
        employee_code="C001",
        full_name="Anita Rao",
        store_id="s1",
        roles=frozenset({role}),
        permissions=perms.permissions_for(frozenset({role})),
        authenticated_at=NOW,
    )


def test_cashier_is_denied_a_void() -> None:
    with pytest.raises(PermissionDenied) as exc:
        session_for(perms.CASHIER).require(perms.SALE_VOID, now=NOW)
    assert exc.value.permission == perms.SALE_VOID


def test_supervisor_grant_lets_the_cashier_void() -> None:
    """The most-used RBAC flow in a real shop (architecture §11.3).

    The supervisor authorises at the same terminal without signing the
    cashier out, so the grant attaches to the existing session.
    """
    granted = replace(
        session_for(perms.CASHIER),
        overrides={perms.SALE_VOID: NOW + OVERRIDE_GRANT_TTL},
    )
    granted.require(perms.SALE_VOID, now=NOW + timedelta(seconds=30))


def test_grant_stops_working_after_ninety_seconds() -> None:
    granted = replace(
        session_for(perms.CASHIER),
        overrides={perms.SALE_VOID: NOW + OVERRIDE_GRANT_TTL},
    )
    with pytest.raises(PermissionDenied):
        granted.require(perms.SALE_VOID, now=NOW + timedelta(seconds=91))


def test_grant_is_ninety_seconds() -> None:
    assert OVERRIDE_GRANT_TTL == timedelta(seconds=90)


def test_manager_needs_no_grant() -> None:
    session_for(perms.MANAGER).require(perms.REPORT_MARGIN, now=NOW)


def test_cashier_is_denied_margin() -> None:
    with pytest.raises(PermissionDenied):
        session_for(perms.CASHIER).require(perms.REPORT_MARGIN, now=NOW)

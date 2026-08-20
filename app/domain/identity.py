"""Session and offline-snapshot rules — architecture §11.4.

Pure. The TTL arithmetic lives here rather than in the auth service so that
"can this cached identity still open a till?" is a unit test with no clock, no
database and no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.domain.permissions import has

# A dismissed employee can open the till for at most this long before one
# online authentication is required (architecture §11.4).
SNAPSHOT_TTL = timedelta(days=14)

# Manager overrides are granted for 90 seconds (architecture §11.3).
OVERRIDE_GRANT_TTL = timedelta(seconds=90)


class PermissionDenied(Exception):
    """Raised when a session lacks a required permission."""

    def __init__(self, permission: str) -> None:
        super().__init__(permission)
        self.permission = permission


class SnapshotExpired(Exception):
    """Raised when an offline permission snapshot is too old to be trusted."""


@dataclass(frozen=True, slots=True)
class Session:
    """An authenticated cashier at this terminal."""

    user_id: str
    employee_code: str
    full_name: str
    store_id: str
    roles: frozenset[str]
    permissions: frozenset[str]
    authenticated_at: datetime
    #: True when the identity came from the local cache rather than Supabase.
    offline: bool = False
    #: Short-lived supervisor grants, permission key -> expiry.
    overrides: dict[str, datetime] = field(default_factory=dict)

    def allows(self, permission: str, *, now: datetime) -> bool:
        if has(self.permissions, permission):
            return True
        granted_until = self.overrides.get(permission)
        return granted_until is not None and now < granted_until

    def require(self, permission: str, *, now: datetime) -> None:
        if not self.allows(permission, now=now):
            raise PermissionDenied(permission)


@dataclass(frozen=True, slots=True)
class CachedIdentity:
    """A locally cached identity, as stored in ``cached_users``."""

    user_id: str
    employee_code: str
    full_name: str
    store_id: str
    pin_hash: str
    status: str
    roles: frozenset[str]
    permissions: frozenset[str]
    snapshot_signed_at: datetime
    snapshot_expires_at: datetime

    def is_usable(self, *, now: datetime) -> bool:
        return self.status == "active" and now < self.snapshot_expires_at

    def to_session(self, *, now: datetime) -> Session:
        if self.status != "active":
            raise SnapshotExpired(f"user {self.employee_code} is {self.status}")
        if now >= self.snapshot_expires_at:
            raise SnapshotExpired(
                f"snapshot for {self.employee_code} expired at "
                f"{self.snapshot_expires_at.isoformat()}"
            )
        return Session(
            user_id=self.user_id,
            employee_code=self.employee_code,
            full_name=self.full_name,
            store_id=self.store_id,
            roles=self.roles,
            permissions=self.permissions,
            authenticated_at=now,
            offline=True,
        )


def snapshot_expiry(signed_at: datetime, ttl: timedelta = SNAPSHOT_TTL) -> datetime:
    """When a snapshot signed at ``signed_at`` stops being usable offline."""
    return signed_at + ttl


def utcnow() -> datetime:
    """Wall clock, always timezone-aware.

    Terminal time is never authoritative for reporting (architecture §9.4) —
    it is used here only for local TTL checks.
    """
    return datetime.now(timezone.utc)

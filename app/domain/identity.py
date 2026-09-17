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


class NotOverridable(Exception):
    """Raised when a permission is asked for that a supervisor may not lend.

    Not the same as being refused: this says the key is outside
    ``permissions.OVERRIDABLE`` entirely, because the write it authorises
    would be refused by RLS at push time under the cashier's own claim. The
    modal must never offer it, and the endpoint must never mint it — a grant
    that passes here and quarantines at 2am is worse than a refusal at the
    counter.
    """

    def __init__(self, permission: str) -> None:
        super().__init__(permission)
        self.permission = permission


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
class OverrideGrant:
    """One supervisor lending one permission to one cashier, for 90 seconds.

    Names both people, because that is the entire point. An override is the
    one path in this system where somebody deliberately exceeds their
    permissions, and a row naming only the cashier would launder an escalation
    into ordinary work.

    Minted when the supervisor authorises, not when the cashier spends it: a
    grant nobody used is still a fact about the shop, and the shape of "a
    supervisor was called to this till eleven times today" is exactly what an
    audit log is for.
    """

    permission: str
    granted_at: datetime
    expires_at: datetime
    #: The store the till belongs to. Carried because the row has no parent
    #: entity to take it from, and a manager scopes the audit log by store.
    store_id: str
    #: The cashier the grant was given to.
    actor_id: str
    actor_code: str
    #: The supervisor who gave it.
    approver_id: str
    approver_code: str

    def is_live(self, *, now: datetime) -> bool:
        return now < self.expires_at


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

"""Login, offline fallback and the terminal's active session.

The flow a cashier actually sees:

    employee code + PIN
        ONLINE : authenticate-pin verifies the PIN server-side, returns a
                 session and a signed snapshot; the snapshot is cached so
                 tomorrow's login works with the network down.
        OFFLINE: verify the PIN against cached_users.pin_hash and read the
                 permission snapshot, provided it is inside its TTL.

An outage must never stop the till trading, and an expired snapshot must
always stop it — those two rules are the whole design (architecture §11.4).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime

from app.config import Settings, get_settings
from app.data.repositories.audit import AuditRepository
from app.data.repositories.users import CachedUserRepository
from app.domain import permissions
from app.domain.identity import (
    OVERRIDE_GRANT_TTL,
    CachedIdentity,
    NotOverridable,
    OverrideGrant,
    Session,
    SnapshotExpired,
    snapshot_expiry,
    utcnow,
)
from app.security import keychain, pins
from app.services.supabase_auth import (
    AccountDisabled,
    AuthUnavailable,
    CloudSession,
    InvalidCredentials,
    SupabaseAuthClient,
)

log = logging.getLogger(__name__)


class LoginFailed(RuntimeError):
    """Credentials rejected, by the server or by the local cache."""


class NoOfflineIdentity(RuntimeError):
    """The cloud is unreachable and this employee has never logged in here."""


class NothingToAuthorise(RuntimeError):
    """The cashier already holds the permission being authorised.

    Refused rather than waved through, because the alternative is an audit row
    describing an escalation that never happened — and a modal that appears to
    do something when it is doing nothing at all.
    """

    def __init__(self, permission: str) -> None:
        super().__init__(f"the signed-in session already holds {permission}")
        self.permission = permission


class CannotAuthoriseSelf(RuntimeError):
    """Somebody tried to approve their own override.

    This is the control, not an edge case. Without it a cashier who knows
    their own PIN approves their own void, and the audit row names the same
    person twice while looking entirely ordinary.
    """

    def __init__(self, permission: str) -> None:
        super().__init__(f"a person cannot authorise their own {permission}")
        self.permission = permission


class ApproverLacksPermission(RuntimeError):
    """The approver does not hold what they were asked to lend."""

    def __init__(self, permission: str, approver_code: str) -> None:
        super().__init__(f"{approver_code} does not hold {permission}")
        self.permission = permission
        self.approver_code = approver_code


class NotSignedIn(RuntimeError):
    """A grant was minted with nobody at the till to receive it.

    Its own exception rather than a silent no-op: an override authorised into
    an empty store is a supervisor who typed their PIN for nothing, and the
    screen has to say so.
    """

    def __init__(self, permission: str) -> None:
        super().__init__(f"nobody is signed in to receive {permission}")
        self.permission = permission


class SessionStore:
    """The one session currently signed in at this terminal.

    Single terminal, single till, one cashier at a time. Screen lock and the
    supervisor override (phase 7) both operate on this object rather than
    replacing it, so a supervisor authorising a void never signs the cashier
    out.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: Session | None = None
        self._access_token: str | None = None

    @property
    def current(self) -> Session | None:
        with self._lock:
            return self._session

    def set(self, session: Session, access_token: str | None) -> None:
        with self._lock:
            self._session = session
            self._access_token = access_token

    def clear(self) -> None:
        with self._lock:
            self._session = None
            self._access_token = None

    @property
    def access_token(self) -> str | None:
        with self._lock:
            return self._access_token

    def grant(
        self,
        permission: str,
        *,
        until: datetime,
        now: datetime | None = None,
    ) -> Session:
        """Lend the signed-in cashier a permission until ``until``.

        `Session` is frozen, so this replaces it inside the store rather than
        mutating it — and replaces it *in place*, which is the whole point:
        the cashier stays signed in, their cart survives, and the supervisor
        walks away. A supervisor authorising a void never signs the cashier
        out (architecture §11.3).

        **Time-boxed, not use-boxed.** The grant is a window, not a token: a
        supervisor who authorises one void authorises every void for the next
        ninety seconds. That is the architecture's choice and it is defensible
        at a counter — the alternative is a supervisor standing there for a
        three-line correction — but it should be a sentence somebody wrote
        down rather than a property of the data structure nobody noticed.

        Refuses three things rather than appearing to work:

        * a permission outside `OVERRIDABLE`, because RLS would refuse the
          write it authorises hours later, into the failures queue;
        * a grant nobody is signed in to receive;
        * a window that has already closed, which would mint a grant that
          reads as success and does nothing.

        Returns the session as it now stands, so a caller writing the audit
        row records what was actually granted rather than what it asked for.
        """
        if not permissions.is_overridable(permission):
            raise NotOverridable(permission)

        now = now or utcnow()
        if until <= now:
            raise ValueError(
                f"grant for {permission} expires at {until.isoformat()}, "
                f"which is not after {now.isoformat()}"
            )

        with self._lock:
            session = self._session
            if session is None:
                raise NotSignedIn(permission)

            # Expired grants are dropped on the way past. `Session.allows`
            # already ignores them, so this changes no behaviour — it keeps
            # the dict from growing all shift and keeps a diagnostics screen
            # from showing a list of things that are not true any more.
            live = {
                key: expiry
                for key, expiry in session.overrides.items()
                if expiry > now
            }

            # Never shorten a window. Two supervisors authorising the same key
            # in the same minute should not leave the cashier with less time
            # than the first one gave them.
            held = live.get(permission)
            live[permission] = max(until, held) if held else until

            self._session = replace(session, overrides=live)
            return self._session


class AuthService:
    def __init__(
        self,
        users: CachedUserRepository,
        sessions: SessionStore,
        cloud: SupabaseAuthClient | None,
        store_code: str,
        terminal_code: str,
        settings: Settings | None = None,
        audit: AuditRepository | None = None,
    ) -> None:
        self.users = users
        self.audit = audit
        self.sessions = sessions
        self.cloud = cloud
        self.store_code = store_code
        self.terminal_code = terminal_code
        # Carried explicitly so that the argon2 cost parameters actually in
        # force are the ones this terminal was configured with, rather than
        # whatever the process-wide default happens to be.
        self.settings = settings or get_settings()

    # ── Login ───────────────────────────────────────────────────────────────

    async def login(self, employee_code: str, pin: str) -> Session:
        employee_code = employee_code.strip().upper()

        if self.cloud is not None:
            try:
                cloud_session = await self.cloud.authenticate_pin(
                    employee_code, pin, self.store_code, self.terminal_code
                )
            except InvalidCredentials as exc:
                # The server is reachable and says no. Do not consult the
                # cache — that would let a revoked PIN keep working.
                raise LoginFailed("invalid employee code or PIN") from exc
            except AccountDisabled as exc:
                self._revoke_cached(employee_code)
                raise LoginFailed("this account is not active") from exc
            except AuthUnavailable as exc:
                log.warning("cloud auth unavailable, falling back to cache: %s", exc)
            else:
                return self._accept_cloud_session(cloud_session)

        return self._login_offline(employee_code, pin)

    def _accept_cloud_session(self, cloud: CloudSession) -> Session:
        now = utcnow()
        identity = CachedIdentity(
            user_id=cloud.user_id,
            employee_code=cloud.employee_code,
            full_name=cloud.full_name,
            store_id=cloud.store_id,
            pin_hash=cloud.pin_hash,
            status=cloud.status,
            roles=cloud.roles,
            permissions=cloud.permissions,
            snapshot_signed_at=cloud.snapshot_signed_at,
            snapshot_expires_at=cloud.snapshot_expires_at,
        )
        self.users.upsert(identity)

        try:
            keychain.save_refresh_token(
                self.store_code, self.terminal_code, cloud.refresh_token
            )
        except keychain.KeychainUnavailable as exc:
            # Not fatal: it only costs an extra online login next launch.
            log.warning("could not store refresh token: %s", exc)

        session = Session(
            user_id=cloud.user_id,
            employee_code=cloud.employee_code,
            full_name=cloud.full_name,
            store_id=cloud.store_id,
            roles=cloud.roles,
            permissions=cloud.permissions,
            authenticated_at=now,
            offline=False,
        )
        self.sessions.set(session, cloud.access_token)
        return session

    def _login_offline(self, employee_code: str, pin: str) -> Session:
        identity = self.users.get_by_employee_code(employee_code)
        if identity is None:
            raise NoOfflineIdentity(
                "No connection, and this employee has not signed in on this "
                "terminal before. Connect to the internet and try again."
            )

        if not pins.verify_pin(identity.pin_hash, pin, self.settings):
            raise LoginFailed("invalid employee code or PIN")

        try:
            session = identity.to_session(now=utcnow())
        except SnapshotExpired as exc:
            raise LoginFailed(
                "This terminal has been offline too long. Connect to the "
                "internet once to sign in."
            ) from exc

        self.sessions.set(session, access_token=None)
        return session

    # ── Session lifecycle ───────────────────────────────────────────────────

    def logout(self) -> None:
        self.sessions.clear()

    def _revoke_cached(self, employee_code: str) -> None:
        identity = self.users.get_by_employee_code(employee_code)
        if identity is not None:
            self.users.revoke(identity.user_id)

    # ── Seeding ─────────────────────────────────────────────────────────────

    def seed_local_user(
        self,
        *,
        user_id: str,
        employee_code: str,
        full_name: str,
        store_id: str,
        pin: str,
        roles: frozenset[str],
        permissions: frozenset[str],
        signed_at: datetime | None = None,
    ) -> CachedIdentity:
        """Write a usable identity straight into the cache.

        Used by ``scripts/seed_local.py`` and by tests. It is how a terminal
        gets its first cashier before the cloud project exists — phase 1 has to
        demonstrate a login without waiting on phase 5's sync.
        """
        signed = signed_at or utcnow()
        identity = CachedIdentity(
            user_id=user_id,
            employee_code=employee_code.strip().upper(),
            full_name=full_name,
            store_id=store_id,
            pin_hash=pins.hash_pin(pin, self.settings),
            status="active",
            roles=roles,
            permissions=permissions,
            snapshot_signed_at=signed,
            snapshot_expires_at=snapshot_expiry(signed),
        )
        self.users.upsert(identity)
        return identity

    # ── Supervisor override (architecture §11.3) ────────────────────────────

    def authorize_override(
        self,
        *,
        approver_code: str,
        pin: str,
        permission: str,
        now: datetime | None = None,
    ) -> OverrideGrant:
        """Verify a second person and lend the cashier a permission.

        The second use `pins.verify_pin` has ever had. The first,
        `_login_offline`, verifies somebody and then *becomes* them. This one
        verifies somebody and then walks away: the session in the store is
        still the cashier's, with one extra key on it for ninety seconds.

        Offline-first like everything else on the sale path. It reads
        `cached_users`, so a supervisor who has never signed in on this
        terminal cannot authorise anything here — which is a real limitation
        of a real shop, and the reason it gets a sentence of its own rather
        than "wrong PIN". The cloud path (`authorize-override`) is what covers
        the supervisor who has never touched this till.

        The order of the checks is deliberate. Anything that can be refused
        without reading a PIN is refused first, so an impossible request never
        becomes a reason to type a credential.
        """
        now = now or utcnow()
        approver_code = approver_code.strip().upper()

        # 1. Is this key lendable at all? Slice 3's rule, checked before
        #    anybody is asked for anything.
        if not permissions.is_overridable(permission):
            raise NotOverridable(permission)

        # 2. Is there a cashier to lend it to? `grant()` would refuse this
        #    later anyway, but only after a supervisor had typed their PIN.
        cashier = self.sessions.current
        if cashier is None:
            raise NotSignedIn(permission)

        # 3. Does the cashier already hold it? Then nothing is being
        #    authorised, and pretending otherwise would put a row in the audit
        #    log describing an escalation that did not happen.
        if permission in cashier.permissions:
            raise NothingToAuthorise(permission)

        identity = self.users.get_by_employee_code(approver_code)
        if identity is None:
            raise NoOfflineIdentity(
                f"{approver_code} has not signed in on this terminal before, "
                "so their PIN cannot be checked without the internet. Sign in "
                "once on this till, or reconnect."
            )

        # 4. A person may not authorise themselves. This is the whole control:
        #    a cashier who knows their own PIN would otherwise be able to
        #    approve their own void, and the audit row would name them twice
        #    and look perfectly ordinary.
        if identity.user_id == cashier.user_id:
            raise CannotAuthoriseSelf(permission)

        if not pins.verify_pin(identity.pin_hash, pin, self.settings):
            raise LoginFailed("that PIN was not recognised")

        # 5. Is the approver still someone this terminal may believe? An
        #    inactive employee, or one whose snapshot has aged out, cannot
        #    authorise — the same rule that stops them signing in.
        if not identity.is_usable(now=now):
            raise LoginFailed(
                f"{approver_code} cannot authorise from this terminal. Either "
                "the account is not active, or this till has been offline too "
                "long to trust what it remembers."
            )

        # 6. Does the approver actually hold what they are lending? Nobody can
        #    give away what they do not have, and without this a second
        #    cashier could authorise a void for the first.
        if permission not in identity.permissions:
            raise ApproverLacksPermission(permission, approver_code)

        expires_at = now + OVERRIDE_GRANT_TTL
        self.sessions.grant(permission, until=expires_at, now=now)

        grant = OverrideGrant(
            permission=permission,
            granted_at=now,
            expires_at=expires_at,
            store_id=cashier.store_id,
            actor_id=cashier.user_id,
            actor_code=cashier.employee_code,
            approver_id=identity.user_id,
            approver_code=identity.employee_code,
        )

        # The row is written where the grant is minted, not where it is spent.
        # A grant nobody used is still a fact about the shop, and "a supervisor
        # was called to this till eleven times today" is exactly the shape an
        # audit log exists to show.
        if self.audit is not None:
            self.audit.record_override(grant)
        else:  # pragma: no cover - only a terminal built without a database
            log.error(
                "override granted with no audit repository: %s to %s by %s",
                permission,
                grant.actor_code,
                grant.approver_code,
            )

        log.info(
            "override granted: %s to %s by %s until %s",
            permission,
            grant.actor_code,
            grant.approver_code,
            expires_at.isoformat(),
        )
        return grant

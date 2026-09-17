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
from app.data.repositories.users import CachedUserRepository
from app.domain import permissions
from app.domain.identity import (
    CachedIdentity,
    NotOverridable,
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
    ) -> None:
        self.users = users
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

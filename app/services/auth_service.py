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
from dataclasses import dataclass, replace
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
    ApproverNotPermitted,
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


class PinLocked(RuntimeError):
    """Too many wrong PINs in a row for this account.

    Its own exception rather than a `LoginFailed`, because the screen has
    something different to say: waiting will help, and trying again now will
    not. Carries the moment the lock lifts so the message can name it.
    """

    def __init__(self, employee_code: str, until: datetime | None) -> None:
        super().__init__(f"{employee_code} is locked until {until}")
        self.employee_code = employee_code
        self.until = until


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
        #: Set when the cloud says the person signed in here no longer works
        #: for this shop. Not a sign-out: see `mark_revoked`.
        self._revoked = False

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
            self._revoked = False

    def mark_revoked(self) -> None:
        """The person at this till has been deactivated in the cloud.

        **Deliberately not `clear()`.** Signing them out here is the obvious
        implementation and the wrong one: at a counter it means a customer
        with eleven items scanned watches the screen drop to a login prompt
        because somebody in an office processed a leaver at 11:40. A
        deactivation is almost never an emergency — it is a leaver or a role
        change — and finishing the sale in front of you is nearly always
        right.

        So this is a fact about the session, and the refusal lands at the next
        sale rather than the current one. Their cached identity has already
        been purged by then, so they cannot sign in again or authorise
        anything; what they can still do is take the money for the basket that
        is already on the screen.
        """
        with self._lock:
            self._revoked = True

    @property
    def revoked(self) -> bool:
        with self._lock:
            return self._revoked

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

    def _ensure_not_locked(
        self, employee_code: str, *, now: datetime
    ) -> CachedIdentity | None:
        """Refuse a locked account **before** choosing how to verify it.

        The lock is a fact about the approver, not about the transport. If this
        ran inside the offline branch, plugging in the network would clear it —
        the most ordinary action in the shop would disable the throttle, and
        the attacker is the one holding the cable.

        The cloud's own limiter does not cover for it: `authenticate-pin`
        counts in a `Map`, per function instance, with no knowledge of which
        till is asking or how many times that till has already been refused.
        Being online changes *who verifies the PIN*. It must not change
        *whether this approver is currently allowed to be asked*.

        Returns the cached identity when there is one, since every caller
        wants it next. None means this terminal has never seen the code, which
        is not a lock and not an error here.
        """
        identity = self.users.get_by_employee_code(employee_code)
        if identity is not None and identity.is_locked(now=now):
            raise PinLocked(identity.employee_code, identity.pin_locked_until)
        return identity

    # ── Login ───────────────────────────────────────────────────────────────

    async def login(self, employee_code: str, pin: str) -> Session:
        employee_code = employee_code.strip().upper()

        # Before the transport is chosen, not inside the offline branch.
        self._ensure_not_locked(employee_code, now=utcnow())

        if self.cloud is not None:
            try:
                cloud_session = await self.cloud.authenticate_pin(
                    employee_code, pin, self.store_code, self.terminal_code
                )
            except InvalidCredentials as exc:
                # The server is reachable and says no. Do not consult the
                # cache — that would let a revoked PIN keep working — but do
                # count the attempt, because it was made here. Without this,
                # leaving the network plugged in is a way to guess for ever
                # without ever reaching a lockout.
                self._count_cloud_failure(employee_code, now=utcnow())
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
        # The server accepted this PIN, so whatever the till had counted
        # against it was somebody getting it wrong, not somebody guessing.
        self.users.clear_pin_failures(cloud.user_id)

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
            # The same bound as an offline sign-in. An online session is not
            # exempt: it is the one that stays open longest, because a till
            # that reaches the cloud has no reason to sign anybody out.
            snapshot_expires_at=cloud.snapshot_expires_at,
            offline=False,
        )
        self.sessions.set(session, cloud.access_token)
        return session


    def _record_refusal(
        self,
        *,
        permission: str,
        cashier: Session,
        approver_code: str,
        approver_id: str | None,
        now: datetime,
        reason: str = "pin_rejected",
    ) -> None:
        if self.audit is None:  # pragma: no cover - terminal with no database
            return
        self.audit.record_override_refused(
            permission=permission,
            store_id=cashier.store_id,
            actor_id=cashier.user_id,
            actor_code=cashier.employee_code,
            approver_code=approver_code,
            approver_id=approver_id,
            reason=reason,
            occurred_at=now,
        )

    # ── The one place a cached PIN is checked ───────────────────────────────

    def _verify_cached_pin(
        self, identity: CachedIdentity, pin: str, *, now: datetime
    ) -> None:
        """Check a PIN against the cache, counting failures and honouring locks.

        Both offline paths go through here, and that is the point. The one
        that looks dangerous is `authorize_override`, because a guess mints a
        grant the audit log will record as a legitimate supervisor
        authorisation. The one that is actually worse is `_login_offline`,
        which has been reachable since phase 1: it looks an employee up by
        code, so a cashier who guesses a supervisor's PIN offline does not get
        ninety seconds, they get that supervisor's whole session.

        `authenticate-pin` has rate-limited the cloud path since it was
        written. This side had nothing, and argon2id at the shipped parameters
        verifies in 22.8 ms natively — a four-digit space in four minutes with
        the cable out. The Edge Function's WebAssembly takes about 1.8 s for
        the same work, which is where the comfortable intuition came from.

        Raises `PinLocked` before touching argon2, so a locked account costs an
        attacker nothing to discover and nothing to grind.
        """
        if identity.is_locked(now=now):
            raise PinLocked(identity.employee_code, identity.pin_locked_until)

        if pins.verify_pin(identity.pin_hash, pin, self.settings):
            # Only write when there is something to forget, so an ordinary
            # sign-in is still one read and no write.
            if identity.consecutive_pin_failures or identity.pin_locked_until:
                self.users.clear_pin_failures(identity.user_id)
            return

        self._count_failure(identity, now=now)
        raise LoginFailed("invalid employee code or PIN")

    def _count_failure(self, identity: CachedIdentity, *, now: datetime) -> None:
        """Count one wrong PIN against this person, and lock if that was enough.

        Raises `PinLocked` when the count reaches the threshold, and returns
        quietly otherwise — the caller decides what a not-yet-locking failure
        means, because that differs between "wrong PIN" and "the server said
        no".

        Its own method rather than the tail of `_verify_cached_pin`, because
        the count is about **attempts made at this terminal**, not about which
        side adjudicated them. See `_count_cloud_failure`.
        """
        until = self.users.record_pin_failure(identity.user_id, now=now)
        if until is None:
            return

        log.warning(
            "pin locked for %s until %s", identity.employee_code, until.isoformat()
        )
        if self.audit is not None:
            # Queued, unlike the individual refusals: one row per lockout
            # event, generated by the throttle rather than by whoever is
            # typing, so it cannot be used to flood the outbox. An owner
            # should find a supervisor's PIN locking out at 11pm without
            # being told to go looking for it.
            self.audit.record_lockout(
                store_id=identity.store_id,
                locked_user_id=identity.user_id,
                locked_code=identity.employee_code,
                consecutive_failures=identity.consecutive_pin_failures + 1,
                locked_until=until,
                occurred_at=now,
            )
        raise PinLocked(identity.employee_code, until)

    def _count_cloud_failure(self, employee_code: str, *, now: datetime) -> None:
        """A PIN the *server* rejected still counts against the local throttle.

        The first half of this problem was found earlier: the lock was checked
        inside the offline branch, so being online cleared it. This is the
        other half, and it survived that fix. With a network up, every wrong
        PIN went to `authenticate-pin`, came back 401, and the local counter
        was never touched — so an attacker at the till could guess all night
        without ever accumulating towards a lockout, simply by leaving the
        cable in.

        The cloud's own limiter does not cover for that. It is a `Map` in one
        Deno isolate: nominal, per instance, and reset by a cold start. Being
        online was therefore the *looser* path, which is the inversion the
        parity test asserts against in the other direction.

        The counter is a fact about attempts arriving at this terminal,
        whoever said no to them.

        One boundary, stated rather than hidden: this can only count against
        somebody the terminal has cached. An employee code this till has never
        seen has no row to increment — and no cached hash to guess at either,
        so what is unthrottled there is the cloud's own rate limit rather than
        anything on this machine.
        """
        identity = self.users.get_by_employee_code(employee_code)
        if identity is not None:
            self._count_failure(identity, now=now)

    def _login_offline(self, employee_code: str, pin: str) -> Session:
        identity = self.users.get_by_employee_code(employee_code)
        if identity is None:
            raise NoOfflineIdentity(
                "No connection, and this employee has not signed in on this "
                "terminal before. Connect to the internet and try again."
            )

        now = utcnow()
        self._verify_cached_pin(identity, pin, now=now)

        try:
            session = identity.to_session(now=now)
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

    @dataclass(frozen=True)
    class _Approver:
        """Who authorised, whichever side checked the PIN.

        The two paths below return this rather than their own shapes, so
        everything after the verification — the grant, the audit row, the
        refusal trail — is written once and cannot drift between transports.
        """

        user_id: str
        employee_code: str

    async def _authorize_online(
        self,
        *,
        approver_code: str,
        pin: str,
        permission: str,
        identity: CachedIdentity | None,
        now: datetime,
    ) -> AuthService._Approver:
        """Verify the approver against `authorize-override`.

        This is what a supervisor who has never signed in on this till is for.
        It also means a demoted supervisor cannot authorise on the strength of
        a stale cached snapshot, because the Edge Function reads
        `role_permissions` at the moment of asking.

        The function returns no session and no PIN hash, so nothing here
        caches the approver: authorising is not signing in, and the two stay
        different acts.

        An outage falls back to the cache, like login. A supervisor standing
        at a till with a void to authorise is not helped by being told the
        internet is down when their PIN is on this machine already.
        """
        assert self.cloud is not None
        try:
            approver = await self.cloud.authorize_override(
                approver_code, pin, permission, self.store_code
            )
        except InvalidCredentials as exc:
            # Counted here, not only next door. The server adjudicated it, but
            # the attempt was made at this terminal, and the terminal's counter
            # is the one that escalates and survives a restart.
            self._count_cloud_failure(approver_code, now=now)
            raise LoginFailed("invalid employee code or PIN") from exc
        except ApproverNotPermitted as exc:
            raise ApproverLacksPermission(permission, approver_code) from exc
        except AccountDisabled as exc:
            self._revoke_cached(approver_code)
            raise LoginFailed(
                f"{approver_code} is not an active account and cannot "
                "authorise anything."
            ) from exc
        except AuthUnavailable as exc:
            log.warning("authorize-override unavailable, using the cache: %s", exc)
            return self._authorize_offline(
                approver_code=approver_code,
                pin=pin,
                permission=permission,
                identity=identity,
                now=now,
            )

        # The server said yes, so whatever this till had counted against them
        # was somebody getting it wrong rather than somebody guessing.
        if identity is not None:
            self.users.clear_pin_failures(identity.user_id)

        return AuthService._Approver(
            user_id=approver.user_id, employee_code=approver.employee_code
        )

    def _authorize_offline(
        self,
        *,
        approver_code: str,
        pin: str,
        permission: str,
        identity: CachedIdentity | None,
        now: datetime,
    ) -> AuthService._Approver:
        """Verify the approver against `cached_users`.

        A real limitation of a real shop: the PIN hash being checked is the
        one this terminal remembers, so a supervisor who has never signed in
        here cannot authorise here. That gets a sentence of its own rather
        than "wrong PIN", because the two have completely different remedies.
        """
        if identity is None:
            raise NoOfflineIdentity(
                f"{approver_code} has not signed in on this terminal before, "
                "so their PIN cannot be checked without the internet. Sign in "
                "once on this till, or reconnect."
            )

        self._verify_cached_pin(identity, pin, now=now)

        # Is the approver still someone this terminal may believe? An inactive
        # employee, or one whose snapshot has aged out, cannot authorise — the
        # same rule that stops them signing in.
        if not identity.is_usable(now=now):
            raise LoginFailed(
                f"{approver_code} cannot authorise from this terminal. Either "
                "the account is not active, or this till has been offline too "
                "long to trust what it remembers."
            )

        # Does the approver actually hold what they are lending? Nobody can
        # give away what they do not have, and without this a second cashier
        # could authorise a void for the first. The online path asks the same
        # question of `role_permissions`; neither side takes the caller's word
        # for it.
        if permission not in identity.permissions:
            raise ApproverLacksPermission(permission, approver_code)

        return AuthService._Approver(
            user_id=identity.user_id, employee_code=identity.employee_code
        )


    async def authorize_override(
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

        The order of the checks is deliberate, and the lock is part of the
        order. Anything refusable without a credential is refused before
        anybody is asked for one, and the lock is refused before a transport
        is chosen — so the online path this grows later changes who verifies
        the PIN, not whether this approver may be asked at all. A lockout that
        a network cable clears is not a lockout.
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

        # 4. Is this approver locked out? Before the lookup branches into a
        #    transport, and before argon2 is touched, so a locked account
        #    costs an attacker nothing to discover and nothing to grind.
        identity = self._ensure_not_locked(approver_code, now=now)

        # 5. A person may not authorise themselves. This is the whole control:
        #    a cashier who knows their own PIN would otherwise be able to
        #    approve their own void, and the audit row would name them twice
        #    and look perfectly ordinary.
        #
        #    Compared by code rather than by user id, because the id needs a
        #    cached identity and the online path may not have one. Codes are
        #    unique and both sides are upper-cased; an approver this terminal
        #    has never seen cannot be the person signed in at it, because
        #    signing in is what caches somebody.
        if approver_code == cashier.employee_code or (
            identity is not None and identity.user_id == cashier.user_id
        ):
            raise CannotAuthoriseSelf(permission)

        # 6. Verify the second person — online if there is a cloud, from the
        #    cache if there is not. Everything above this line is refusable
        #    without asking anybody for a credential; everything below it is
        #    the same whichever way the PIN was checked: one grant, one audit
        #    row, one session that still belongs to the cashier.
        try:
            if self.cloud is not None:
                approver = await self._authorize_online(
                    approver_code=approver_code,
                    pin=pin,
                    permission=permission,
                    identity=identity,
                    now=now,
                )
            else:
                approver = self._authorize_offline(
                    approver_code=approver_code,
                    pin=pin,
                    permission=permission,
                    identity=identity,
                    now=now,
                )
        except (LoginFailed, PinLocked, ApproverLacksPermission):
            # Five failures against a supervisor's PIN is the most interesting
            # thing that could happen at a till all week, and until now nothing
            # would have recorded it. Written before the exception continues,
            # so a refusal leaves a trail even though no grant was minted.
            self._record_refusal(
                permission=permission,
                cashier=cashier,
                approver_code=approver_code,
                approver_id=identity.user_id if identity is not None else None,
                now=now,
            )
            raise

        # 7. Ask again, now that somebody authoritative has said who this is.
        #
        #    Step 5 compares codes, which is what it can do before a PIN is
        #    spent. This compares the identity the verifier actually returned,
        #    and it is the one that cannot be fooled by the cache: a till whose
        #    cached row for the cashier has been revoked mid-shift — which
        #    `_revoke_cached` does — would otherwise let that cashier approve
        #    their own void online, because `identity` is None and there is
        #    nothing for step 5's second clause to compare.
        #
        #    Costs the attacker a correct PIN and buys nothing. The check is
        #    cheap; the audit row it prevents is one that names the same person
        #    twice and reads as entirely ordinary.
        if approver.user_id == cashier.user_id:
            raise CannotAuthoriseSelf(permission)

        expires_at = now + OVERRIDE_GRANT_TTL
        self.sessions.grant(permission, until=expires_at, now=now)

        grant = OverrideGrant(
            permission=permission,
            granted_at=now,
            expires_at=expires_at,
            store_id=cashier.store_id,
            actor_id=cashier.user_id,
            actor_code=cashier.employee_code,
            approver_id=approver.user_id,
            approver_code=approver.employee_code,
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

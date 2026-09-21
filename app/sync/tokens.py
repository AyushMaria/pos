"""Keeping the cloud session alive — the half of login nobody had written.

The access token Supabase mints at sign-in lives for an hour. Every push,
pull and revocation sweep carries it, and RLS evaluates it, which is the
whole point (architecture §11.2). The refresh token that comes with it was
saved to the OS credential store at login — and then never read. Nothing
called `SupabaseAuthClient.refresh`; nothing called
`keychain.load_refresh_token`.

So a till signed in at 8am stopped syncing at 9am. Every push came back 401,
the pusher correctly read an expired JWT as transient, and it retried a
credential that could never work again until somebody signed in. Nothing was
lost — the queue keeps everything — and nothing left either. Found while
writing phase 7's revocation sweep, which needed the token for something and
noticed it was dead.

Two paths, and both are needed:

  * **Proactively**, before a cycle, when the token is inside its last few
    minutes. This is the one that runs in practice.
  * **Reactively**, when a push is refused with 401, once per drain. This is
    the backstop for a clock that is wrong, a token minted elsewhere, or a
    refresh that raced the expiry.

Everything that touches the session store or the auth client arrives as a
callable. `app.sync` and `app.services` are independent siblings in the
import contract, and the session crosses that line the same way the
revocation sweep's does.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt

from app.security import keychain

log = logging.getLogger(__name__)

#: Refresh when this much of the token's life is left. Ten minutes against a
#: one-hour token means a cycle every ninety seconds has several chances
#: before the edge, and a clock a few minutes wrong still lands inside the
#: window rather than outside it.
REFRESH_MARGIN = timedelta(minutes=10)

Outcome = Literal["refreshed", "dead", "unreachable", "nothing"]

#: The one sentence both paths say when the refresh token is dead too. The
#: engine matches on it to set `needs_signin`, and the indicator repeats it,
#: so it lives in exactly one place.
SIGN_IN_NEEDED = (
    "This till's cloud session has expired. Sales are safe on this machine; "
    "sign in again to resume sending them."
)


def expires_at(access_token: str) -> datetime | None:
    """The `exp` claim, or None for a token that does not say.

    Unverified, like every other claim the terminal reads: the signature is
    Postgres's business (architecture §1.3), and a forged `exp` buys an
    attacker a refresh attempt the server will refuse.
    """
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=timezone.utc)


@dataclass
class TokenRefresher:
    #: Exchanges a refresh token for (access, refresh). Raises whatever the
    #: auth client raises; `refresh()` sorts those into the three outcomes.
    exchange: Callable[[str], Awaitable[tuple[str, str]]]
    #: The session store's current access token, or None when signed out.
    current_token: Callable[[], str | None]
    #: Hands the store a new access token without touching the session —
    #: the person at the till is the same person, just re-credentialed.
    replace_token: Callable[[str], None]
    store_code: str
    terminal_code: str
    #: Which exceptions from `exchange` mean the refresh token itself is
    #: dead, as opposed to the network being down. Injected because the
    #: classes live in `app.services`, which this package may not import.
    is_rejection: Callable[[BaseException], bool]
    margin: timedelta = REFRESH_MARGIN

    def expiring(self, now: datetime) -> bool:
        """Is the current token inside its final margin, or already gone?"""
        token = self.current_token()
        if token is None:
            return False
        exp = expires_at(token)
        if exp is None:
            return False
        return exp - now <= self.margin

    async def refresh(self) -> Outcome:
        """Try once. Never raises: the caller decides what each outcome means."""
        if self.current_token() is None:
            return "nothing"

        # `load_refresh_token` already turns a missing credential store into
        # None, so an unreadable keychain and an empty one land in the same
        # place: nothing to exchange, and only a sign-in will fix it.
        stored = keychain.load_refresh_token(self.store_code, self.terminal_code)
        if not stored:
            return "dead"

        try:
            access, new_refresh = await self.exchange(stored)
        except Exception as exc:  # sorted below, never re-raised
            if self.is_rejection(exc):
                log.warning("refresh token rejected; an online sign-in is needed")
                return "dead"
            log.info("refresh deferred: %s", exc)
            return "unreachable"

        self.replace_token(access)
        try:
            keychain.save_refresh_token(self.store_code, self.terminal_code, new_refresh)
        except keychain.KeychainUnavailable as exc:
            # The new access token is live for an hour regardless; this only
            # costs the *next* refresh, which will find the old token rejected
            # and ask for a sign-in. Said in the log so that is not a mystery.
            log.warning("refreshed, but could not store the new refresh token: %s", exc)
        log.info("cloud session refreshed")
        return "refreshed"

    async def ensure_fresh(self, now: datetime) -> Outcome:
        """The proactive path: refresh only if the token is near its end."""
        if not self.expiring(now):
            return "nothing"
        return await self.refresh()

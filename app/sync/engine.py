"""The sync loop — architecture §9, §8.

One asyncio task, running inside the server's event loop, doing three things
on a cycle: drain the outbox, pull reference data, and report what happened.

Two constraints shape it:

  * **Nothing here may block the event loop.** SQLite writes go through the
    write lock, which is a threading lock, so repository calls are dispatched
    with `run_in_threadpool` rather than awaited directly (architecture §8).
  * **Nothing here may interrupt a sale.** A failure to sync is not a failure
    to sell. Every error is caught, logged, and turned into a status the UI
    can show — the cashier finds out from an indicator, never from a dialog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.data.repositories.outbox import OutboxRepository
from app.domain.identity import utcnow
from app.sync.puller import Puller
from app.sync.pusher import DrainResult, Pusher
from app.sync.revocations import RevocationSweep
from app.sync.tokens import SIGN_IN_NEEDED, TokenRefresher

log = logging.getLogger(__name__)

#: How often to look, when nothing is going wrong. Price changes propagating
#: in ~90s instead of ~1s is an acceptable trade for a till (§9.3).
IDLE_INTERVAL = 90.0

#: How soon to look again after pushing something — a busy counter should not
#: wait a minute and a half to send the next sale.
BUSY_INTERVAL = 5.0


@dataclass
class SyncStatus:
    """What the register shows about the cloud."""

    online: bool = False
    backlog: int = 0
    oldest_pending_at: str | None = None
    failures: int = 0
    last_push_at: datetime | None = None
    last_pull_at: datetime | None = None
    last_error: str | None = None
    #: Set when the server refuses this build's payloads (§17). The only
    #: status the cashier cannot resolve by waiting.
    needs_update: bool = False
    #: Set when the cloud session cannot be renewed — the access token has
    #: expired and the refresh token is rejected or gone. The other status
    #: waiting will not fix: a person has to sign in. Sales keep working.
    needs_signin: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "backlog": self.backlog,
            "oldest_pending_at": self.oldest_pending_at,
            "failures": self.failures,
            "last_push_at": self.last_push_at.isoformat() if self.last_push_at else None,
            "last_pull_at": self.last_pull_at.isoformat() if self.last_pull_at else None,
            "last_error": self.last_error,
            "needs_update": self.needs_update,
            "needs_signin": self.needs_signin,
        }


@dataclass
class SyncEngine:
    outbox: OutboxRepository
    pusher: Pusher
    puller: Puller
    #: Called with the status dict after every cycle, so the UI hears about a
    #: backlog without polling for it.
    #: Optional so a terminal with no cloud, and every test that builds an
    #: engine to watch the outbox, keeps working unchanged.
    revocations: RevocationSweep | None = None
    #: Renews the access token before it expires. Optional for the same
    #: reason as `revocations`.
    tokens: TokenRefresher | None = None
    publish: Any = None
    status: SyncStatus = field(default_factory=SyncStatus)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="sync")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def nudge(self) -> None:
        """A sale was just posted; do not wait for the next tick."""
        self._wake.set()

    async def _run(self) -> None:
        while True:
            try:
                result = await self.cycle()
                delay = BUSY_INTERVAL if result.did_anything else IDLE_INTERVAL
                if self.pusher.backoff.is_backing_off:
                    delay = max(delay, self.pusher.backoff.seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sync cycle failed")
                delay = IDLE_INTERVAL

            try:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
            finally:
                self._wake.clear()

    # ── One pass ────────────────────────────────────────────────────────────

    async def cycle(self) -> DrainResult:
        await self._keep_session_alive()
        result = await self._push()
        if not result.stopped_early:
            await self._pull()
            await self._sweep_revocations()
        await self._refresh()
        return result

    async def _keep_session_alive(self) -> None:
        """Refresh the access token before it runs out, not after.

        The pusher can recover from a 401 by refreshing on the spot, but that
        is the backstop. This is the path that runs in practice: a cycle
        every ninety seconds, and a token that gets renewed inside its last
        ten minutes rather than found dead at the counter.
        """
        if self.tokens is None:
            return
        outcome = await self.tokens.ensure_fresh(utcnow())
        if outcome == "refreshed":
            self.status.needs_signin = False
        elif outcome == "dead":
            self.status.needs_signin = True
            self.status.last_error = SIGN_IN_NEEDED

    async def _sweep_revocations(self) -> None:
        """Drop cached identities the cloud no longer recognises.

        **Fails open, and the whole cycle is built that way.** Nothing here may
        interrupt a sale, so an unreachable function is logged and forgotten —
        which means the sealed snapshot TTL is the only thing bounding a
        dismissed employee while this path is down. That is a load-bearing
        role rather than a backstop, and `app/sync/revocations.py` says so at
        the call it makes.

        Deliberately after the pull rather than before: a terminal that cannot
        reach Supabase to pull will not reach it to ask this either, and
        putting it second keeps one failure from looking like two.
        """
        if self.revocations is None:
            return
        try:
            await self.revocations.run()
        except Exception as exc:  # pragma: no cover - defence in depth
            log.warning("revocation sweep failed: %s", exc)

    async def _push(self) -> DrainResult:
        try:
            result = await self.pusher.drain()
        except Exception as exc:
            log.warning("push failed: %s", exc)
            self.status.online = False
            self.status.last_error = str(exc)
            return DrainResult(stopped_early=True, error=str(exc))

        if result.pushed:
            self.status.last_push_at = utcnow()
        self.status.online = not result.stopped_early
        self.status.last_error = result.error
        self.status.needs_update = bool(
            result.error and "needs updating" in result.error
        )
        if result.error == SIGN_IN_NEEDED:
            self.status.needs_signin = True
        elif result.pushed:
            # Something got through under the current token, so whatever the
            # last cycle said about the session no longer applies.
            self.status.needs_signin = False
        return result

    async def _pull(self) -> None:
        try:
            await self.puller.pull_all()
        except Exception as exc:
            log.warning("pull failed: %s", exc)
            self.status.online = False
            self.status.last_error = str(exc)
            return
        self.status.last_pull_at = utcnow()
        self.status.online = True
        # A pull that returned is proof the cloud accepted this token —
        # `raise_for_status` means a 401 would have landed above. That is
        # what clears the badge, and it has to be proof rather than a clock:
        # a token can be inside its hour and still be refused.
        #
        # Without this the only thing that cleared `needs_signin` was a
        # successful *push*, so a till signed in again on a quiet morning
        # went on saying "Sign in needed" until somebody made a sale. The
        # one instruction on screen was the one thing that would not help,
        # which is worse than no badge at all.
        self.status.needs_signin = False

    async def _refresh(self) -> None:
        self.status.backlog = await run_in_threadpool(self.outbox.backlog)
        self.status.oldest_pending_at = await run_in_threadpool(
            self.outbox.oldest_pending_at
        )
        self.status.failures = await run_in_threadpool(
            self.outbox.unacknowledged_failures
        )
        if self.publish is not None:
            await self.publish("sync.status", self.status.as_dict())

    # ── For the status endpoint, without waiting for a cycle ────────────────

    def snapshot(self) -> SyncStatus:
        self.status.backlog = self.outbox.backlog()
        self.status.oldest_pending_at = self.outbox.oldest_pending_at()
        self.status.failures = self.outbox.unacknowledged_failures()
        return self.status

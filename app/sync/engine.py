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
        }


@dataclass
class SyncEngine:
    outbox: OutboxRepository
    pusher: Pusher
    puller: Puller
    #: Called with the status dict after every cycle, so the UI hears about a
    #: backlog without polling for it.
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
        result = await self._push()
        if not result.stopped_early:
            await self._pull()
        await self._refresh()
        return result

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

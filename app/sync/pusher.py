"""Draining the outbox — architecture §9.2.

    while batch := next_pending(200):
        push  ->  mark synced
        transient failure  ->  back off, stop, try again later
        permanent failure  ->  quarantine, keep going

The distinction between those last two is the whole design. A dropped network
must never lose a sale; a payload the server will never accept must never
block the sales behind it. Getting them the wrong way round gives you either a
queue that silently discards, or one that stops forever behind a single bad
row — and the second is the one a shop notices a week later.

**Idempotency lives at the other end.** Client-generated UUID v7 primary keys
plus `ON CONFLICT (id) DO NOTHING` mean a replay is a no-op. So the risky
window — server committed, acknowledgement lost, terminal retries — costs one
redundant round trip and nothing else. That is why `mark_synced` runs *after*
the response rather than optimistically before it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.data.repositories.outbox import OutboxRepository, OutboxRow
from app.domain.identity import utcnow
from app.sync.backoff import Backoff
from app.sync.envelope import SCHEMA_VERSION, Envelope
from app.sync.payloads import PayloadBuilder, PayloadError
from app.sync.tokens import SIGN_IN_NEEDED

log = logging.getLogger(__name__)

BATCH_SIZE = 200

#: Long enough for a slow rural link, short enough that a dead one is noticed
#: within a poll interval rather than wedging the loop.
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)


class TransientError(RuntimeError):
    """The network, or the server, was briefly unavailable. Retry."""


class AuthRefused(TransientError):
    """401 or 403 from the server.

    An expired JWT looks like this and a genuine RLS denial looks like this.
    Transient by inheritance — the safe direction — but distinguishable, so
    the drain loop can try one refresh before giving up for the cycle.
    """


class PermanentError(RuntimeError):
    """The server will never accept this. Quarantine it."""


class SchemaTooOld(PermanentError):
    """This terminal is behind the server and must be updated (§17).

    Distinguished from an ordinary rejection because the answer is different:
    nothing is wrong with the data, and retrying after an update will work.
    """


@dataclass(frozen=True, slots=True)
class DrainResult:
    pushed: int = 0
    quarantined: int = 0
    batches: int = 0
    stopped_early: bool = False
    error: str | None = None

    @property
    def did_anything(self) -> bool:
        return bool(self.pushed or self.quarantined)


class Pusher:
    def __init__(
        self,
        outbox: OutboxRepository,
        payloads: PayloadBuilder,
        *,
        base_url: str,
        anon_key: str,
        token_provider: Any,
        backoff: Backoff | None = None,
        client: httpx.AsyncClient | None = None,
        refresh: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self.outbox = outbox
        self.payloads = payloads
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        #: Callable returning the current user JWT, or None when signed out.
        #: RLS runs as the cashier, so a push is only ever as privileged as
        #: the person who made the sale (architecture §11.2).
        self.token_provider = token_provider
        self.backoff = backoff or Backoff()
        self._client = client
        #: Tries to renew the access token; returns one of the outcomes in
        #: `app.sync.tokens`. Optional, so a terminal with no cloud and every
        #: test that builds a pusher to watch the outbox keeps working.
        self.refresh = refresh

    # ── The loop ────────────────────────────────────────────────────────────

    async def drain(self) -> DrainResult:
        """Push everything pending, or stop at the first transient failure."""
        pushed = quarantined = batches = 0
        refreshed_this_drain = False

        while True:
            batch = self.outbox.next_pending(BATCH_SIZE)
            if not batch:
                self.backoff.reset()
                return DrainResult(pushed=pushed, quarantined=quarantined,
                                   batches=batches)

            envelopes, unbuildable = self._build(batch)
            if unbuildable:
                # A pointer to a record that is gone. It cannot be fixed by
                # retrying and it must not be pushed as a gap in the sequence.
                self.outbox.quarantine(
                    [row for row, _ in unbuildable],
                    "; ".join(reason for _, reason in unbuildable),
                    at=utcnow(),
                )
                quarantined += len(unbuildable)

            if not envelopes:
                continue

            rows = [row for row, _ in envelopes]
            try:
                await self._post([envelope for _, envelope in envelopes])
            except AuthRefused as exc:
                # One refresh per drain. If it works, the same batch goes
                # again with the new token — nothing was marked, so nothing
                # is lost by looping. If the refresh token is dead as well,
                # say so in a sentence a person can act on, and stop: a
                # credential that cannot be renewed is not a network blip.
                if self.refresh is not None and not refreshed_this_drain:
                    refreshed_this_drain = True
                    outcome = await self.refresh()
                    if outcome == "refreshed":
                        log.info("access token refreshed after a 401; retrying the batch")
                        continue
                    if outcome == "dead":
                        self.outbox.record_attempt([row.id for row in rows], SIGN_IN_NEEDED)
                        log.warning(SIGN_IN_NEEDED)
                        return DrainResult(
                            pushed=pushed, quarantined=quarantined, batches=batches,
                            stopped_early=True, error=SIGN_IN_NEEDED,
                        )
                self.outbox.record_attempt([row.id for row in rows], str(exc))
                wait = self.backoff.fail()
                log.info("sync push deferred (%s); next attempt in %.0fs", exc, wait)
                return DrainResult(
                    pushed=pushed, quarantined=quarantined, batches=batches,
                    stopped_early=True, error=str(exc),
                )
            except TransientError as exc:
                self.outbox.record_attempt([row.id for row in rows], str(exc))
                wait = self.backoff.fail()
                log.info("sync push deferred (%s); next attempt in %.0fs", exc, wait)
                return DrainResult(
                    pushed=pushed, quarantined=quarantined, batches=batches,
                    stopped_early=True, error=str(exc),
                )
            except SchemaTooOld as exc:
                # Every item in the batch fails for the same reason, and so
                # would every item sent alone. Nothing to isolate.
                self._quarantine(envelopes, str(exc))
                quarantined += len(rows)
                batches += 1
                log.error("sync push quarantined %d rows: %s", len(rows), exc)
                continue
            except PermanentError as exc:
                if len(envelopes) == 1:
                    self._quarantine(envelopes, str(exc))
                    quarantined += 1
                    batches += 1
                    log.error("sync push quarantined 1 row: %s", exc)
                    continue

                # `sync_push` is one transaction: it raises on the first item
                # it cannot accept and rolls back the lot. The batch was
                # refused, but the *batch* is not what was wrong — one item
                # was, and its error came back naming it. Quarantining all
                # two hundred rows with that one sentence records a sale as
                # having failed for an entity it does not contain, and takes
                # every innocent row out of the queue with the guilty one.
                # Resend them one at a time so each row gets its own verdict.
                try:
                    sent, bad = await self._isolate(envelopes)
                except TransientError as inner:
                    self.outbox.record_attempt([row.id for row in rows], str(inner))
                    wait = self.backoff.fail()
                    log.info(
                        "sync push deferred mid-isolation (%s); next attempt in %.0fs",
                        inner, wait,
                    )
                    return DrainResult(
                        pushed=pushed, quarantined=quarantined, batches=batches,
                        stopped_early=True, error=str(inner),
                    )
                pushed += sent
                quarantined += bad
                batches += 1
                log.error(
                    "sync push refused a batch of %d: %d sent alone, %d quarantined "
                    "(first refusal: %s)", len(rows), sent, bad, exc,
                )
                # Deliberately keep going. One bad row must not stop the
                # queue behind it, which is the failure this whole split
                # between permanent and transient exists to prevent.
                continue

            self.outbox.mark_synced([row.id for row in rows], at=utcnow())
            self.backoff.reset()
            pushed += len(rows)
            batches += 1
            log.info("pushed %d rows", len(rows))

    async def _isolate(
        self, envelopes: list[tuple[OutboxRow, Envelope]]
    ) -> tuple[int, int]:
        """Resend a refused batch one row at a time. Returns (sent, quarantined).

        Idempotency at the far end is what makes this safe: any item the
        server *had* accepted before rolling back is a no-op on resend. The
        cost is one round trip per row for the rare batch that carries a bad
        one — a refused batch is already the slow path, and a wrong answer
        recorded against a good sale is the expensive one.

        A transient failure mid-way propagates: the rows already handled are
        marked, the rest stay pending, and the next drain resumes them.
        """
        sent = quarantined = 0
        for row, envelope in envelopes:
            try:
                await self._post([envelope])
            except PermanentError as exc:
                self._quarantine([(row, envelope)], str(exc))
                quarantined += 1
                continue
            self.outbox.mark_synced([row.id], at=utcnow())
            sent += 1
        return sent, quarantined

    def _quarantine(
        self, envelopes: list[tuple[OutboxRow, Envelope]], error: str
    ) -> None:
        self.outbox.quarantine(
            [row for row, _ in envelopes],
            error,
            at=utcnow(),
            envelopes={row.id: envelope.to_dict() for row, envelope in envelopes},
        )

    # ── Building ────────────────────────────────────────────────────────────

    def _build(
        self, batch: list[OutboxRow]
    ) -> tuple[list[tuple[OutboxRow, Envelope]], list[tuple[OutboxRow, str]]]:
        built: list[tuple[OutboxRow, Envelope]] = []
        failed: list[tuple[OutboxRow, str]] = []
        for row in batch:
            try:
                built.append(
                    (
                        row,
                        self.payloads.build(
                            row.entity, row.entity_id, row.op, row.client_seq
                        ),
                    )
                )
            except PayloadError as exc:
                failed.append((row, str(exc)))
        return built, failed

    # ── The wire ────────────────────────────────────────────────────────────

    async def _post(self, envelopes: list[Envelope]) -> None:
        token = self.token_provider()
        if not token:
            # Not an error: the till is simply signed out, or was never signed
            # in online. The queue waits, which is what offline-first means.
            raise TransientError("no cloud session yet")

        body = {"items": [envelope.to_dict() for envelope in envelopes]}
        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        client = self._client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            response = await client.post(
                f"{self.base_url}/rest/v1/rpc/sync_push", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise TransientError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        self._raise_for(response)

    def _raise_for(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return

        detail = _detail(response)

        # 5xx and 429 are the server having a moment, not a verdict on the
        # data. Anything else in the 4xx range is a verdict.
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(f"HTTP {response.status_code}: {detail}")

        if "schema_version" in detail or "outdated_terminal" in detail:
            raise SchemaTooOld(
                f"this terminal is behind the server (built for schema "
                f"v{SCHEMA_VERSION}) and needs updating: {detail}"
            )

        if response.status_code in (401, 403):
            # An expired JWT looks like this and is transient; a genuine RLS
            # denial also looks like this and is not. Treating both as
            # transient is the safe way round — a real denial keeps failing
            # and stays visible in `attempts` and `last_error`, whereas
            # quarantining a whole batch on an expired token would set aside
            # sales that were never actually refused. `AuthRefused` is the
            # same verdict with a name, so the drain loop can try a refresh.
            raise AuthRefused(f"HTTP {response.status_code}: {detail}")

        raise PermanentError(f"HTTP {response.status_code}: {detail}")


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "hint"):
            if key in payload:
                return str(payload[key])[:500]
    return str(payload)[:500]


def isoformat(value: datetime) -> str:
    return value.isoformat()

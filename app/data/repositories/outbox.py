"""The outbox — architecture §9.2.

Every transactional row that must reach the cloud is queued here **inside the
transaction that created it**. A sale that is durable but unqueued would never
be pushed and nobody would notice, which is the one failure this table exists
to make impossible.

Rows hold a *pointer*, not a snapshot: `{"sale_id": ...}` rather than the whole
sale. The pusher re-reads the record at drain time (see `app.sync.payloads`).
That is safe here for a specific reason rather than by luck — transactional
rows are append-only, so nothing can change underneath a queued pointer — and
it keeps the write path, which runs inside the customer-facing transaction,
down to one small INSERT.

The cost is that a quarantined row in `sync_failures` records what was *sent*
rather than what was *queued*, so it stores the built envelope, not the
pointer. Otherwise a failure would be a note saying "sale 123 failed" with no
way to see what went out.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.data.repositories.base import Repository


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    entity: str
    entity_id: str
    op: str
    payload: dict[str, Any]
    client_seq: int
    created_at: str
    attempts: int

    @classmethod
    def of(cls, row: sqlite3.Row) -> OutboxRow:
        return cls(
            id=int(row["id"]),
            entity=row["entity"],
            entity_id=row["entity_id"],
            op=row["op"],
            payload=json.loads(row["payload_json"]),
            client_seq=int(row["client_seq"]),
            created_at=row["created_at"],
            attempts=int(row["attempts"]),
        )


class OutboxRepository(Repository):
    def next_pending(self, limit: int = 200) -> list[OutboxRow]:
        """The oldest unsent rows, in the order they were created.

        `client_seq` preserves causality per terminal (architecture §9.2), and
        `id` is monotonic in the same order, so ordering by id is ordering by
        `client_seq` without a second index.
        """
        rows = self._rows(
            "SELECT * FROM outbox WHERE synced_at IS NULL ORDER BY id LIMIT ?",
            (limit,),
        )
        return [OutboxRow.of(row) for row in rows]

    def mark_synced(self, ids: list[int], *, at: datetime) -> None:
        """Acknowledge a batch.

        Called only after the server has confirmed. If the process dies
        between the server's commit and this call, the batch is simply sent
        again — and `ON CONFLICT DO NOTHING` server-side means a replay is not
        a duplicate. That is the whole idempotency story (§9.2).
        """
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._execute(
            f"UPDATE outbox SET synced_at = ? WHERE id IN ({placeholders})",
            (at.isoformat(), *ids),
        )

    def record_attempt(self, ids: list[int], error: str) -> None:
        """A transient failure. The rows stay pending and will be retried."""
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._execute(
            f"UPDATE outbox SET attempts = attempts + 1, last_error = ? "
            f" WHERE id IN ({placeholders})",
            (error[:500], *ids),
        )

    def quarantine(
        self,
        rows: list[OutboxRow],
        error: str,
        *,
        at: datetime,
        envelopes: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        """Set a permanently-failing row aside so the queue keeps moving.

        A payload the server will never accept — an RLS denial, a validation
        error, a sale referencing a product that was deleted — must not block
        every sale behind it. It goes to `sync_failures` for a manager, and
        the queue continues (architecture §9.2).

        Marked synced in the same transaction, because "handled" and "sent"
        need to be the same state as far as the drain loop is concerned; the
        `sync_failures` row is what says which of the two actually happened.
        """
        if not rows:
            return
        envelopes = envelopes or {}
        with self.transaction() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO sync_failures (outbox_id, payload_json, error, failed_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        row.id,
                        json.dumps(envelopes.get(row.id, row.payload)),
                        error[:2000],
                        at.isoformat(),
                    ),
                )
                conn.execute(
                    "UPDATE outbox SET synced_at = ?, last_error = ? WHERE id = ?",
                    (at.isoformat(), error[:500], row.id),
                )

    # ── What the status endpoint reports ────────────────────────────────────

    def backlog(self) -> int:
        return int(
            self._scalar("SELECT COUNT(*) FROM outbox WHERE synced_at IS NULL") or 0
        )

    def oldest_pending_at(self) -> str | None:
        value = self._scalar(
            "SELECT created_at FROM outbox WHERE synced_at IS NULL ORDER BY id LIMIT 1"
        )
        return str(value) if value else None

    def unacknowledged_failures(self) -> int:
        return int(
            self._scalar(
                "SELECT COUNT(*) FROM sync_failures WHERE acknowledged = 0"
            )
            or 0
        )

    def failures(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._rows(
            "SELECT * FROM sync_failures WHERE acknowledged = 0 "
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        )

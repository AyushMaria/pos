"""Codes that matched nothing - architecture 10.2.

The table has existed since phase 1 with nothing writing to it. It would
already have been collecting the most useful evidence in the project: which
codes real customers present that this shop's catalogue cannot answer.

Two things put a row here, and they mean different things:

  * a lookup missed and the cashier moved on - the item was not sold, but the
    shop met it;
  * a lookup missed and the cashier sold it anyway - there is now a sale line
    against the placeholder, and a real product needs creating to match.

Both are the same row. What distinguishes them is whether a sale references
that barcode, which is a question for whoever works the queue.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.data.repositories.base import Repository
from app.domain.ids import new_id


class UnknownScanRepository(Repository):
    def record(
        self, barcode: str, *, store_id: str, terminal_id: str, at: datetime
    ) -> str:
        """Note a code that resolved to nothing, and queue it.

        Deliberately not deduplicated. The same unknown code presented five
        times in a week is a stronger signal than the same code once, and
        collapsing them would throw that away - it is the difference between
        "somebody once had an odd item" and "we are turning this away daily".
        """
        scan_id = new_id()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO unknown_scans (id, store_id, barcode, scanned_at, "
                "terminal_id, resolved) VALUES (?, ?, ?, ?, ?, 0)",
                (scan_id, store_id, barcode, at.isoformat(), terminal_id),
            )
            conn.execute(
                """
                INSERT INTO outbox (entity, entity_id, op, payload_json,
                                    client_seq, created_at)
                VALUES ('unknown_scan', ?, 'insert', ?, ?, ?)
                """,
                (
                    scan_id,
                    json.dumps({"scan_id": scan_id, "barcode": barcode}),
                    self._next_client_seq(conn),
                    at.isoformat(),
                ),
            )
        return scan_id

    def pending(self, limit: int = 100) -> list[sqlite3.Row]:
        """What is still waiting for somebody to identify it."""
        return self._rows(
            "SELECT * FROM unknown_scans WHERE resolved = 0 "
            "ORDER BY scanned_at DESC LIMIT ?",
            (limit,),
        )

    def count_pending(self) -> int:
        return int(
            self._scalar("SELECT COUNT(*) FROM unknown_scans WHERE resolved = 0") or 0
        )

    def _next_client_seq(self, conn: sqlite3.Connection) -> int:
        """Per-terminal ordering (9.2), shared with sales and movements so the
        three keep their order relative to each other."""
        row = conn.execute(
            "SELECT value FROM terminal_state WHERE key = 'client_seq'"
        ).fetchone()
        nxt = int(row[0]) + 1
        conn.execute(
            "UPDATE terminal_state SET value = ? WHERE key = 'client_seq'", (str(nxt),)
        )
        return nxt

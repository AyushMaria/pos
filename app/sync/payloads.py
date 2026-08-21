"""Building what gets pushed, at the moment it gets pushed — architecture §9.2.

The outbox holds pointers, so this module does the re-reading. A sale is not
one row: it is the sale, its lines, its attempts, its payments, its stock
ledger deltas and its audit entries, and all of them travel together because
they were written together and mean nothing apart.

**Re-reading is safe here for a specific reason.** Transactional rows are
append-only (architecture §1.4) — no UPDATE policy exists on any of them, and
nothing in this codebase writes one — so what is read at drain time is exactly
what was committed. The moment that stops being true, this becomes wrong and
the payload must be snapshotted at write time instead.

One thing this module fixes on the way past: local rows carry the terminal
*code* (`T1`), because that is what a receipt number is built from and what a
cashier would recognise. The server keys terminals by UUID. The translation
has to happen somewhere, and here — at the boundary, once, with a clear error
when the terminal has no identity yet — is better than storing a UUID nobody
at the counter could match to a machine.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.data.db import Database
from app.sync.envelope import Envelope


class PayloadError(RuntimeError):
    """The record could not be built, and never will be.

    Raised for a pointer whose record has vanished, or a terminal with no
    cloud identity. Both quarantine rather than retry: a thousand attempts
    will not make a missing sale reappear.
    """


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    # `row.keys()` is sqlite3.Row's column list, not a dict — iterating the
    # Row itself would give values.
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


class PayloadBuilder:
    """Turns an outbox pointer into the record the server expects."""

    def __init__(self, db: Database, *, terminal_id: str) -> None:
        self.db = db
        #: The server's UUID for this till, not its counter-facing code.
        self.terminal_id = terminal_id

    def build(self, entity: str, entity_id: str, op: str, client_seq: int) -> Envelope:
        builders = {
            "sale": self._sale,
            "sale_review": self._sale_review,
        }
        builder = builders.get(entity)
        if builder is None:
            raise PayloadError(f"nothing knows how to push a {entity!r}")
        return Envelope(
            entity=entity,
            op=op,
            id=entity_id,
            client_seq=client_seq,
            data=builder(entity_id),
        )

    # ── Entities ────────────────────────────────────────────────────────────

    def _sale(self, sale_id: str) -> dict[str, Any]:
        sale = self.db.query_one("SELECT * FROM sales WHERE id = ?", (sale_id,))
        if sale is None:
            raise PayloadError(f"sale {sale_id} is queued but no longer exists")

        record = _dict(sale)
        record.pop("posted", None)  # terminal bookkeeping; the cloud has no column
        record["terminal_id"] = self._terminal()

        record["lines"] = [
            _dict(row)
            for row in self.db.query(
                "SELECT * FROM sale_lines WHERE sale_id = ? ORDER BY line_no",
                (sale_id,),
            )
        ]
        record["attempts"] = [
            _dict(row)
            for row in self.db.query(
                "SELECT * FROM payment_attempts WHERE sale_id = ? ORDER BY started_at",
                (sale_id,),
            )
        ]
        record["payments"] = [
            _dict(row)
            for row in self.db.query(
                "SELECT * FROM payments WHERE sale_id = ?", (sale_id,)
            )
        ]

        ledger = []
        for row in self.db.query(
            "SELECT * FROM stock_ledger WHERE ref_type = 'sale' AND ref_id = ?",
            (sale_id,),
        ):
            delta = _dict(row)
            delta["terminal_id"] = self._terminal()
            ledger.append(delta)
        record["stock_ledger"] = ledger

        record["audit"] = [
            _dict(row)
            for row in self.db.query(
                "SELECT * FROM audit_log WHERE entity = 'sale' AND entity_id = ?",
                (sale_id,),
            )
        ]
        return record

    def _sale_review(self, review_id: str) -> dict[str, Any]:
        review = self.db.query_one(
            "SELECT * FROM sale_reviews WHERE id = ?", (review_id,)
        )
        if review is None:
            raise PayloadError(f"review {review_id} is queued but no longer exists")
        return _dict(review)

    # ── Identity ────────────────────────────────────────────────────────────

    def _terminal(self) -> str:
        if not self.terminal_id:
            raise PayloadError(
                "this terminal has no cloud identity: set POS_TERMINAL_ID to the "
                "UUID from the terminals table. Until then a sale cannot name "
                "the machine that took it, and the server will not accept it."
            )
        return self.terminal_id


def as_json(envelopes: list[Envelope]) -> str:
    """Serialise a batch. Separate so a quarantine can log exactly what went."""
    return json.dumps({"items": [envelope.to_dict() for envelope in envelopes]})

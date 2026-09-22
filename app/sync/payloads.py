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

    #: Everything this builder knows how to turn into an envelope, and the
    #: set `sync_push` must accept on the other side. Declared rather than
    #: derived from the table below so that a test can name it without
    #: building anything, and cross-checked against the table on every call.
    SUPPORTED_ENTITIES: frozenset[str] = frozenset(
        {
            "sale", "sale_review", "stock_movement", "unknown_scan", "override",
            "audit", "register_session", "cash_movement", "shift_close",
        }
    )

    def __init__(self, db: Database, *, terminal_id: str) -> None:
        self.db = db
        #: The server's UUID for this till, not its counter-facing code.
        self.terminal_id = terminal_id

    def build(self, entity: str, entity_id: str, op: str, client_seq: int) -> Envelope:
        builders = {
            "sale": self._sale,
            "sale_review": self._sale_review,
            "stock_movement": self._stock_movement,
            "unknown_scan": self._unknown_scan,
            "override": self._audit_row,
            "audit": self._audit_row,
            "register_session": self._register_session,
            "cash_movement": self._cash_movement,
            "shift_close": self._shift_close,
        }
        assert set(builders) == self.SUPPORTED_ENTITIES, (
            "SUPPORTED_ENTITIES and the builder table have drifted: "
            f"{sorted(set(builders) ^ self.SUPPORTED_ENTITIES)}"
        )
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

    def _stock_movement(self, movement_id: str) -> dict[str, Any]:
        """One ledger row, pushed on its own.

        A receipt, a count or an adjustment — a delta with no sale behind it.
        Deltas belonging to a sale are *not* pushed this way: they travel
        inside the sale envelope, with the sale that caused them, because they
        were written in the same transaction and mean nothing apart from it.
        Pushing one twice, once here and once nested, would move stock twice.
        `_sale` selects `WHERE ref_type = 'sale'`, and nothing enqueues a
        movement whose `ref_type` is `sale`, which is what keeps the two sets
        disjoint.
        """
        movement = self.db.query_one(
            "SELECT * FROM stock_ledger WHERE id = ?", (movement_id,)
        )
        if movement is None:
            raise PayloadError(
                f"stock movement {movement_id} is queued but no longer exists"
            )

        record = _dict(movement)
        # Local rows carry the terminal *code*; the cloud keys terminals by
        # UUID. Same translation the sale path makes, for the same reason.
        record["terminal_id"] = self._terminal()

        # An adjustment's reason lives in an audit row, and this is the only
        # route it has to the cloud — `_sale` gathers audit rows by
        # `entity = 'sale'`, so a movement's would never be picked up. Without
        # this the cloud records that stock moved and not why, which is
        # precisely the column shrinkage hides in.
        record["audit"] = [
            _dict(row)
            for row in self.db.query(
                "SELECT * FROM audit_log WHERE entity = 'stock_ledger' "
                "AND entity_id = ?",
                (movement_id,),
            )
        ]
        return record

    def _unknown_scan(self, scan_id: str) -> dict[str, Any]:
        """A code that matched nothing, on its way to somebody who can fix it.

        The queue is only useful if it outlives the terminal: a shop that loses
        a machine should not also lose the list of things it could not
        identify, and whoever resolves them is not necessarily at the till.
        """
        scan = self.db.query_one(
            "SELECT * FROM unknown_scans WHERE id = ?", (scan_id,)
        )
        if scan is None:
            raise PayloadError(f"unknown scan {scan_id} is queued but has gone")

        record = _dict(scan)
        record["terminal_id"] = self._terminal()
        record["resolved"] = bool(record.get("resolved"))
        return record

    def _audit_row(self, audit_id: str) -> dict[str, Any]:
        """A standalone audit row, keyed on itself.

        Two entity names reach this: `override` for a minted grant and `audit`
        for a lockout. Both are audit rows with no parent and the same shape,
        so they share a builder; the names are kept apart on the wire because
        `override` was already queued by terminals in the field before
        lockouts existed, and renaming a value some till has in its outbox is
        how a queue stops draining.

        Every other builder here is handed the id of a business record and
        re-reads it — a sale, a movement, a scan — and any audit rows travel
        alongside as part of that record's envelope. This one is different and
        the difference is the reason it exists: a grant has no business record.
        It is minted when the supervisor authorises and may be spent on
        nothing, so `entity_id` is the `audit_log.id`, and this is the first
        builder whose subject is an audit row rather than something an audit
        row describes.

        Worth saying out loud, because that asymmetry is what made the entity
        whitelist look complete when it was not. Four entities covered every
        business record the terminal writes, and the fifth kind of row — the
        one that is only ever a record of who allowed what — had nowhere to go.
        """
        row = self.db.query_one("SELECT * FROM audit_log WHERE id = ?", (audit_id,))
        if row is None:
            raise PayloadError(f"override {audit_id} is queued but no longer exists")

        record = _dict(row)
        # `before_json` is always null for a grant: nothing existed before it.
        record.pop("before_json", None)
        return record

    def _register_session(self, session_id: str) -> dict[str, Any]:
        """A shift, opened. The parent of every sale that follows it.

        Same terminal-code-to-UUID translation as a sale, for the same
        reason. `status` is local bookkeeping and is not sent: the cloud's
        row says `open` for ever, and the close is its own entity.
        """
        row = self.db.query_one(
            "SELECT * FROM register_sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise PayloadError(f"session {session_id} is queued but no longer exists")
        record = _dict(row)
        record["terminal_id"] = self._terminal()
        for local_only in ("status", "closed_at", "counted_cash", "expected_cash", "variance"):
            record.pop(local_only, None)
        return record

    def _cash_movement(self, movement_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM cash_movements WHERE id = ?", (movement_id,))
        if row is None:
            raise PayloadError(f"cash movement {movement_id} is queued but no longer exists")
        return _dict(row)

    def _shift_close(self, close_id: str) -> dict[str, Any]:
        """The close as it was signed. Nothing is recomputed on the way out."""
        row = self.db.query_one("SELECT * FROM shift_closes WHERE id = ?", (close_id,))
        if row is None:
            raise PayloadError(f"shift close {close_id} is queued but no longer exists")
        return _dict(row)

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

"""Register sessions, cash movements and shift closes — phase 8.

Three tables, one rule: **nothing here is ever updated.** A session is opened
(insert), cash moves (insert), the shift closes (insert). The columns 001 put
on `register_sessions` for closing in place are left alone; migration 008
says why. Every write that the cloud needs to see is queued to the outbox in
the same transaction, so a close taken with the line down reaches Supabase
when the line comes back and not before.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.data.repositories.base import Repository
from app.domain.ids import new_id
from app.domain.money import Money
from app.domain.shift import CashMovementRow, PaymentRow, SaleRow, ShiftFigures


@dataclass(frozen=True, slots=True)
class OpenShift:
    id: str
    terminal_id: str
    user_id: str
    opened_at: datetime
    opening_float: Money


@dataclass(frozen=True, slots=True)
class ClosedShift:
    """A `shift_closes` row as it was written — figures stored, not recomputed."""

    close_id: str
    session_id: str
    terminal_id: str
    opened_by: str
    opened_at: datetime
    closed_by: str
    closed_at: datetime
    counted_cash: Money
    note: str | None
    figures: ShiftFigures


class ShiftRepository(Repository):
    # ── Sessions ────────────────────────────────────────────────────────────

    def current(self, terminal_id: str) -> OpenShift | None:
        """The one open session on this terminal, or None."""
        row = self._row(
            "SELECT * FROM register_sessions WHERE terminal_id = ? AND status = 'open'",
            (terminal_id,),
        )
        return None if row is None else _to_open(row)

    def open(
        self,
        *,
        terminal_id: str,
        user_id: str,
        opening_float: Money,
        opened_at: datetime,
        store_id: str,
    ) -> OpenShift:
        """Open a session. The partial unique index in 008 refuses a second one.

        Queued at once, ahead of every sale on it: `sales.session_id` is a
        foreign key in the cloud, and the outbox sequence is what puts the
        parent before its children.
        """
        session_id = new_id()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO register_sessions (
                    id, terminal_id, user_id, opened_at, opening_float, status,
                    store_id
                ) VALUES (?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    session_id, terminal_id, user_id, opened_at.isoformat(),
                    opening_float.paise, store_id,
                ),
            )
            self._queue(conn, "register_session", session_id, {}, opened_at)
        return OpenShift(
            id=session_id,
            terminal_id=terminal_id,
            user_id=user_id,
            opened_at=opened_at,
            opening_float=opening_float,
        )

    # ── Cash movements ──────────────────────────────────────────────────────

    def record_cash_movement(
        self,
        *,
        session_id: str,
        direction: str,
        amount: Money,
        reason: str,
        actor_id: str,
        approved_by: str | None,
        occurred_at: datetime,
    ) -> str:
        movement_id = new_id()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO cash_movements (
                    id, session_id, direction, amount, reason, actor_id,
                    approved_by, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    movement_id, session_id, direction, amount.paise, reason,
                    actor_id, approved_by, occurred_at.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_log (
                    id, actor_id, approver_id, action, entity, entity_id,
                    before_json, after_json, occurred_at
                ) VALUES (?, ?, ?, ?, 'cash_movement', ?, NULL, ?, ?)
                """,
                (
                    new_id(), actor_id, approved_by, f"cash.{direction}", movement_id,
                    json.dumps({"amount_paise": amount.paise, "reason": reason}),
                    occurred_at.isoformat(),
                ),
            )
            self._queue(conn, "cash_movement", movement_id, {}, occurred_at)
        return movement_id

    # ── What the close reads ────────────────────────────────────────────────

    def sales_for(self, session_id: str) -> list[SaleRow]:
        """Every sale on the session, with its review outcome if it has one.

        A sale can have more than one review row in theory; the newest wins,
        which is the same rule the reviews screen uses.
        """
        rows = self._rows(
            """
            SELECT s.id, s.status, s.grand_total, s.rounding_adjustment,
                   (SELECT r.outcome FROM sale_reviews r
                     WHERE r.sale_id = s.id
                     ORDER BY r.resolved_at DESC LIMIT 1) AS outcome
              FROM sales s
             WHERE s.session_id = ?
            """,
            (session_id,),
        )
        return [
            SaleRow(
                sale_id=row["id"],
                status=row["status"],
                grand_total=Money(int(row["grand_total"])),
                rounding_adjustment=Money(int(row["rounding_adjustment"])),
                review_outcome=row["outcome"],
            )
            for row in rows
        ]

    def payments_for(self, session_id: str) -> list[PaymentRow]:
        rows = self._rows(
            """
            SELECT p.sale_id, p.method, p.amount, p.verified
              FROM payments p JOIN sales s ON s.id = p.sale_id
             WHERE s.session_id = ? AND p.status = 'approved'
            """,
            (session_id,),
        )
        return [
            PaymentRow(
                sale_id=row["sale_id"],
                method=row["method"],
                amount=Money(int(row["amount"])),
                verified=bool(row["verified"]),
            )
            for row in rows
        ]

    def movements_for(self, session_id: str) -> list[CashMovementRow]:
        rows = self._rows(
            "SELECT direction, amount FROM cash_movements WHERE session_id = ?",
            (session_id,),
        )
        return [CashMovementRow(row["direction"], Money(int(row["amount"]))) for row in rows]

    def under_review_receipts(self, session_id: str, *, as_of: datetime) -> list[str]:
        """Receipt numbers of the sales that were still under review at `as_of`.

        A review resolved after `as_of` does not take a sale off tonight's
        list, for the same reason the stored figures are not recomputed.
        """
        rows = self._rows(
            """
            SELECT s.receipt_no
              FROM sales s
             WHERE s.session_id = ? AND s.status = 'requires_review'
               AND NOT EXISTS (
                   SELECT 1 FROM sale_reviews r
                    WHERE r.sale_id = s.id AND r.resolved_at <= ?)
             ORDER BY s.receipt_no
            """,
            (session_id, as_of.isoformat()),
        )
        return [row["receipt_no"] or "(no receipt number)" for row in rows]

    def undelivered_sales(self, session_id: str) -> tuple[int, int]:
        """This shift's sales that have not reached the cloud: (waiting, quarantined).

        Waiting is still in the outbox; quarantined was set aside into the
        failures queue (marked synced in the same breath, which is why the
        failures table is what tells them apart). The day-close check uses
        these to say *why* the cloud's count is short.
        """
        row = self._row(
            """
            SELECT
              SUM(CASE WHEN o.synced_at IS NULL THEN 1 ELSE 0 END) AS waiting,
              SUM(CASE WHEN EXISTS (SELECT 1 FROM sync_failures f
                                     WHERE f.outbox_id = o.id) THEN 1 ELSE 0 END)
                AS quarantined
              FROM outbox o JOIN sales s ON s.id = o.entity_id
             WHERE o.entity = 'sale' AND s.session_id = ?
            """,
            (session_id,),
        )
        if row is None:
            return 0, 0
        return int(row["waiting"] or 0), int(row["quarantined"] or 0)

    def name_of(self, user_id: str) -> str:
        """A person's name for a report, or their id if the cache lacks them."""
        row = self._row("SELECT full_name FROM cached_users WHERE user_id = ?", (user_id,))
        return row["full_name"] if row is not None else user_id

    # ── The close ───────────────────────────────────────────────────────────

    def close_for(self, session_id: str) -> ClosedShift | None:
        """The stored close of a session, with the session's opening beside it."""
        row = self._row(
            """
            SELECT c.*, rs.terminal_id, rs.user_id AS opened_by, rs.opened_at,
                   rs.opening_float
              FROM shift_closes c JOIN register_sessions rs ON rs.id = c.session_id
             WHERE c.session_id = ?
            """,
            (session_id,),
        )
        if row is None:
            return None

        def m(column: str) -> Money:
            return Money(int(row[column]))

        return ClosedShift(
            close_id=row["id"],
            session_id=session_id,
            terminal_id=row["terminal_id"],
            opened_by=row["opened_by"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            closed_by=row["closed_by"],
            closed_at=datetime.fromisoformat(row["closed_at"]),
            counted_cash=m("counted_cash"),
            note=row["note"],
            figures=ShiftFigures(
                opening_float=m("opening_float"),
                cash_sales=m("cash_sales"),
                upi_attested=m("upi_attested"),
                upi_verified=m("upi_verified"),
                cash_in=m("cash_in"),
                cash_out=m("cash_out"),
                rounding=m("rounding"),
                under_review_count=int(row["under_review_count"]),
                under_review_total=m("under_review_total"),
                sales_count=int(row["sales_count"]),
            ),
        )


    def close(
        self,
        *,
        session_id: str,
        closed_by: str,
        counted_cash: Money,
        figures: ShiftFigures,
        note: str | None,
        closed_at: datetime,
    ) -> str:
        """Write the close. One transaction: the row, the audit, the outbox.

        The figures are stored, not left to be recomputed: the numbers a
        supervisor signed off are the numbers. The one column touched on the
        session row is `status`, and it is local bookkeeping so `current()`
        stops returning the session — the cloud is never told about it and
        never asked to accept it. The cloud learns the shift is closed from
        the `shift_closes` row, which is the event (008).
        """
        close_id = new_id()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO shift_closes (
                    id, session_id, closed_at, closed_by, counted_cash,
                    expected_cash, variance, cash_sales, upi_attested,
                    upi_verified, cash_in, cash_out, rounding,
                    under_review_count, under_review_total, sales_count, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    close_id, session_id, closed_at.isoformat(), closed_by,
                    counted_cash.paise, figures.expected_cash.paise,
                    figures.variance(counted_cash).paise, figures.cash_sales.paise,
                    figures.upi_attested.paise, figures.upi_verified.paise,
                    figures.cash_in.paise, figures.cash_out.paise, figures.rounding.paise,
                    figures.under_review_count, figures.under_review_total.paise,
                    figures.sales_count, note,
                ),
            )
            conn.execute(
                "UPDATE register_sessions SET status = 'closed' WHERE id = ?",
                (session_id,),
            )
            conn.execute(
                """
                INSERT INTO audit_log (
                    id, actor_id, approver_id, action, entity, entity_id,
                    before_json, after_json, occurred_at
                ) VALUES (?, ?, NULL, 'shift.closed', 'register_session', ?, NULL, ?, ?)
                """,
                (
                    new_id(), closed_by, session_id,
                    json.dumps({
                        "counted_paise": counted_cash.paise,
                        "expected_paise": figures.expected_cash.paise,
                        "variance_paise": figures.variance(counted_cash).paise,
                    }),
                    closed_at.isoformat(),
                ),
            )
            self._queue(conn, "shift_close", close_id, {}, closed_at)
        return close_id

    # ── Outbox ──────────────────────────────────────────────────────────────

    def _queue(
        self,
        conn: sqlite3.Connection,
        entity: str,
        entity_id: str,
        payload: dict[str, object],
        at: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO outbox (entity, entity_id, op, payload_json, client_seq, created_at)
            VALUES (?, ?, 'insert', ?, ?, ?)
            """,
            (entity, entity_id, json.dumps(payload), self._next_client_seq(conn),
             at.isoformat()),
        )

    def _next_client_seq(self, conn: sqlite3.Connection) -> int:
        """Per-terminal ordering (§9.2), from the counter everything shares."""
        row = conn.execute(
            "SELECT value FROM terminal_state WHERE key = 'client_seq'"
        ).fetchone()
        nxt = int(row[0]) + 1
        conn.execute(
            "UPDATE terminal_state SET value = ? WHERE key = 'client_seq'", (str(nxt),)
        )
        return nxt


def _to_open(row: sqlite3.Row) -> OpenShift:
    return OpenShift(
        id=row["id"],
        terminal_id=row["terminal_id"],
        user_id=row["user_id"],
        opened_at=datetime.fromisoformat(row["opened_at"]),
        opening_float=Money(int(row["opening_float"])),
    )

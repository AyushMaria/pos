"""Posting a sale — architecture §8.

`post_sale` is **one transaction**. Sale, lines, payment attempts, payments,
stock ledger deltas, the audit row and the outbox rows all commit together or
none of them do.

The reason is stated plainly in architecture §8: *a sale that reaches the
printer but not the outbox is a lost sale; a sale that reaches the database
but not the printer is only a reprint.* Everything that makes the sale real
goes inside this boundary; rendering the receipt happens after it, and is
retryable.

Phase 3's exit criteria include killing the process mid-sale and confirming no
partial sale exists. That is this function's contract.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.data.repositories.base import Repository
from app.domain.cart import Cart
from app.domain.ids import new_id
from app.domain.money import Money
from app.domain.payments import AttemptState, PaymentAttempt
from app.domain.tender import CashRounding


@dataclass(frozen=True, slots=True)
class PostedSale:
    """What a committed sale looks like to everything downstream."""

    sale_id: str
    receipt_no: str
    status: str
    grand_total: Money
    rounding_adjustment: Money
    change_due: Money
    posted_at: datetime


class SalesRepository(Repository):
    def post_sale(
        self,
        *,
        cart: Cart,
        cart_id: str,
        store_id: str,
        terminal_id: str,
        cashier_id: str,
        receipt_no: str,
        attempts: list[PaymentAttempt],
        rounding: CashRounding | None,
        change_due: Money,
        client_created_at: datetime,
        posted_at: datetime,
        session_id: str | None = None,
        requires_review: bool = False,
    ) -> PostedSale:
        """Commit a whole sale, atomically."""
        adjustment = rounding.adjustment if rounding else Money.zero()
        grand_total = cart.total_before_rounding + adjustment
        status = "requires_review" if requires_review else "completed"

        # Client-generated UUID v7 so the server can accept it with
        # ON CONFLICT DO NOTHING and a dropped ACK never double-posts (§9.2).
        sale_id = cart_id

        with self.transaction() as conn:
            client_seq = self._next_client_seq(conn)

            conn.execute(
                """
                INSERT INTO sales (
                    id, store_id, terminal_id, session_id, receipt_no, cashier_id,
                    type, status, subtotal, discount_total, tax_total,
                    rounding_adjustment, grand_total, client_created_at, posted
                ) VALUES (?, ?, ?, ?, ?, ?, 'sale', ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    sale_id,
                    store_id,
                    terminal_id,
                    session_id,
                    receipt_no,
                    cashier_id,
                    status,
                    cart.subtotal.paise,
                    cart.discount_total.paise,
                    cart.tax_total.paise,
                    adjustment.paise,
                    grand_total.paise,
                    client_created_at.isoformat(),
                ),
            )

            for line_no, line in cart.numbered():
                conn.execute(
                    """
                    INSERT INTO sale_lines (
                        id, sale_id, line_no, product_id, barcode_scanned,
                        description, qty_milli, unit_price, discount_amount,
                        tax_amount, line_total, tax_code, tax_rate_bp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id(),
                        sale_id,
                        line_no,
                        line.line.product_id,
                        line.line.barcode_scanned,
                        line.description,
                        line.line.qty_milli,
                        line.line.unit_price.paise,
                        line.discount_total.paise,
                        line.tax_amount.paise,
                        line.line_total.paise,
                        line.tax_code.code,
                        line.tax_code.rate_bp,
                    ),
                )

                # Stock is a ledger of deltas, never a counter (§1.5).
                conn.execute(
                    """
                    INSERT INTO stock_ledger (
                        id, store_id, product_id, delta_milli, reason,
                        ref_type, ref_id, occurred_at, terminal_id, user_id
                    ) VALUES (?, ?, ?, ?, 'sale', 'sale', ?, ?, ?, ?)
                    """,
                    (
                        new_id(),
                        store_id,
                        line.line.product_id,
                        -line.line.qty_milli,
                        sale_id,
                        posted_at.isoformat(),
                        terminal_id,
                        cashier_id,
                    ),
                )

            for attempt in attempts:
                conn.execute(
                    """
                    INSERT INTO payment_attempts (
                        id, sale_id, method, amount, state, txn_ref, qr_payload,
                        started_at, expires_at, resolved_at, failure_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.id,
                        sale_id,
                        attempt.method,
                        attempt.amount.paise,
                        attempt.state.value,
                        attempt.txn_ref,
                        attempt.qr_payload,
                        _iso(attempt.started_at),
                        _iso(attempt.expires_at),
                        _iso(attempt.resolved_at),
                        attempt.failure_reason,
                    ),
                )

                if attempt.state is not AttemptState.APPROVED:
                    continue

                conn.execute(
                    """
                    INSERT INTO payments (
                        id, sale_id, attempt_id, method, amount, status,
                        tender_ref, confirmation_method, confirmed_by, verified
                    ) VALUES (?, ?, ?, ?, ?, 'approved', ?, ?, ?, ?)
                    """,
                    (
                        new_id(),
                        sale_id,
                        attempt.id,
                        attempt.method,
                        attempt.amount.paise,
                        attempt.txn_ref,
                        # Cash is confirmed by the drawer taking it. UPI in
                        # phase 4 is manual_attestation until settlement.
                        "immediate" if attempt.method == "cash" else "manual_attestation",
                        cashier_id,
                        1 if attempt.method == "cash" else 0,
                    ),
                )

            conn.execute(
                """
                INSERT INTO audit_log (
                    id, actor_id, action, entity, entity_id, after_json, occurred_at
                ) VALUES (?, ?, 'sale.post', 'sale', ?, ?, ?)
                """,
                (
                    new_id(),
                    cashier_id,
                    sale_id,
                    json.dumps(
                        {
                            "receipt_no": receipt_no,
                            "grand_total": grand_total.paise,
                            "rounding_adjustment": adjustment.paise,
                            "lines": cart.item_count,
                            "status": status,
                        }
                    ),
                    posted_at.isoformat(),
                ),
            )

            # The outbox row rides the same transaction. A sale that is durable
            # but unqueued would never reach the cloud (§9.2).
            conn.execute(
                """
                INSERT INTO outbox (entity, entity_id, op, payload_json,
                                    client_seq, created_at)
                VALUES ('sale', ?, 'insert', ?, ?, ?)
                """,
                (
                    sale_id,
                    json.dumps({"sale_id": sale_id, "receipt_no": receipt_no}),
                    client_seq,
                    posted_at.isoformat(),
                ),
            )

        return PostedSale(
            sale_id=sale_id,
            receipt_no=receipt_no,
            status=status,
            grand_total=grand_total,
            rounding_adjustment=adjustment,
            change_due=change_due,
            posted_at=posted_at,
        )

    # ── Reads ───────────────────────────────────────────────────────────────

    def get(self, sale_id: str) -> sqlite3.Row | None:
        return self._row("SELECT * FROM sales WHERE id = ?", (sale_id,))

    def by_receipt_no(self, receipt_no: str) -> sqlite3.Row | None:
        return self._row("SELECT * FROM sales WHERE receipt_no = ?", (receipt_no,))

    def lines_for(self, sale_id: str) -> list[sqlite3.Row]:
        return self._rows(
            "SELECT * FROM sale_lines WHERE sale_id = ? ORDER BY line_no", (sale_id,)
        )

    def payments_for(self, sale_id: str) -> list[sqlite3.Row]:
        return self._rows("SELECT * FROM payments WHERE sale_id = ?", (sale_id,))

    def count(self) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM sales") or 0)

    # ── The review queue — architecture §13.5 ───────────────────────────────

    def open_reviews(self) -> list[sqlite3.Row]:
        """Sales posted as `requires_review` that nobody has settled yet.

        A left join rather than a status flag, because the sale's status is
        never rewritten: `requires_review` is what happened, and it stays true
        after the fact (see migration 003).
        """
        return self._rows(
            """
            SELECT s.id, s.receipt_no, s.grand_total, s.client_created_at,
                   s.cashier_id,
                   (SELECT COALESCE(SUM(amount), 0) FROM payment_attempts
                     WHERE sale_id = s.id AND state = 'unknown') AS disputed_amount
              FROM sales s
              LEFT JOIN sale_reviews r ON r.sale_id = s.id
             WHERE s.status = 'requires_review' AND r.id IS NULL
             ORDER BY s.client_created_at
            """
        )

    def review_for(self, sale_id: str) -> sqlite3.Row | None:
        return self._row("SELECT * FROM sale_reviews WHERE sale_id = ?", (sale_id,))

    def resolve_review(
        self,
        *,
        sale_id: str,
        outcome: str,
        resolved_by: str,
        resolved_at: datetime,
        note: str | None = None,
    ) -> str:
        """Record what a supervisor decided about a disputed payment.

        The review row and its audit row commit together: a decision about
        money that leaves no trace of who made it is worse than no decision.
        """
        review_id = new_id()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sale_reviews (
                    id, sale_id, outcome, note, resolved_by, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (review_id, sale_id, outcome, note, resolved_by,
                 resolved_at.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO audit_log (
                    id, actor_id, action, entity, entity_id, after_json, occurred_at
                ) VALUES (?, ?, 'sale.review.resolve', 'sale', ?, ?, ?)
                """,
                (
                    new_id(),
                    resolved_by,
                    sale_id,
                    json.dumps({"outcome": outcome, "note": note}),
                    resolved_at.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO outbox (entity, entity_id, op, payload_json,
                                    client_seq, created_at)
                VALUES ('sale_review', ?, 'insert', ?, ?, ?)
                """,
                (
                    review_id,
                    json.dumps({"review_id": review_id, "sale_id": sale_id}),
                    self._next_client_seq(conn),
                    resolved_at.isoformat(),
                ),
            )
        return review_id

    def totals_balance(self, sale_id: str) -> bool:
        """Does this stored sale add up?

        Read back from SQLite rather than from the in-memory cart, so it
        answers the question the exit criteria actually ask: is what *landed*
        internally consistent.
        """
        sale = self.get(sale_id)
        if sale is None:
            return False

        line_total = (
            self._scalar(
                "SELECT COALESCE(SUM(line_total), 0) FROM sale_lines WHERE sale_id = ?",
                (sale_id,),
            )
            or 0
        )
        paid = (
            self._scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM payments "
                " WHERE sale_id = ? AND status = 'approved'",
                (sale_id,),
            )
            or 0
        )

        expected_grand = int(line_total) + int(sale["rounding_adjustment"])
        if expected_grand != int(sale["grand_total"]):
            return False
        # Cash may overpay, so payments cover the total rather than equal it.
        return int(paid) >= int(sale["grand_total"])

    def _next_client_seq(self, conn: sqlite3.Connection) -> int:
        """Per-terminal ordering, so a sale pushes before its payment (§9.2)."""
        row = conn.execute(
            "SELECT value FROM terminal_state WHERE key = 'client_seq'"
        ).fetchone()
        nxt = int(row[0]) + 1
        conn.execute(
            "UPDATE terminal_state SET value = ? WHERE key = 'client_seq'", (str(nxt),)
        )
        return nxt


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()

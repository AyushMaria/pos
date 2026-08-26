"""Stock movements that no sale produced — architecture §9.4, §1.5.

A receipt, a count and an adjustment all end in the same place: rows in
`stock_ledger`, and an outbox row per movement so each reaches the cloud.
`stock_levels` is never written here, and on the terminal it is not derived
here either — there is no local trigger. The cloud's `stock_ledger_apply`
maintains the authoritative level from every delta, and the terminal receives
that number by pulling it. A second path into a level is how phase 5 briefly
moved every sale's stock twice, and adding one locally would put the puller
and the ledger in a fight over the same column.

**One transaction per document.** A receipt of forty lines is forty ledger
rows and forty outbox rows, committed together or not at all. Half a delivery
is worse than none: the shelf and the screen disagree and nothing says which
half arrived.

**The count is the one with a trap in it.** `expected` has to be read inside
the same transaction that writes the correction. Read it outside and a sale
between the two is silently reversed — the count puts back stock that was
just sold, and the ledger says the shopkeeper miscounted.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.data.repositories.base import Repository
from app.domain.ids import new_id
from app.domain.receiving import Movement


class InventoryRepository(Repository):
    """Writes to the ledger, and queues what it wrote."""

    def record(
        self,
        movements: list[Movement],
        *,
        store_id: str,
        terminal_id: str,
        user_id: str,
        ref_type: str,
        occurred_at: datetime,
    ) -> list[str]:
        """Write movements and queue them. Returns the ledger ids written.

        `ref_type` names the document — `receipt`, `count`, `adjustment` —
        and is deliberately never `sale`. That word is what `payloads.py`
        selects on to gather a sale's own deltas into its envelope, so a
        movement tagged `sale` here would be pushed twice: once on its own and
        once nested. Stock would move by double.
        """
        if ref_type == "sale":
            raise ValueError(
                "ref_type 'sale' is reserved: a sale's deltas travel inside "
                "its own envelope, and one tagged this way would push twice"
            )
        if not movements:
            return []

        written: list[str] = []
        with self.transaction() as conn:
            for movement in movements:
                movement_id = new_id()
                conn.execute(
                    """
                    INSERT INTO stock_ledger (
                        id, store_id, product_id, delta_milli, reason,
                        ref_type, ref_id, occurred_at, terminal_id, user_id
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        movement_id,
                        store_id,
                        movement.product_id,
                        movement.delta_milli,
                        movement.reason,
                        ref_type,
                        occurred_at.isoformat(),
                        terminal_id,
                        user_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO outbox (entity, entity_id, op, payload_json,
                                        client_seq, created_at)
                    VALUES ('stock_movement', ?, 'insert', ?, ?, ?)
                    """,
                    (
                        movement_id,
                        json.dumps(
                            {
                                "movement_id": movement_id,
                                "product_id": movement.product_id,
                                "reason": movement.reason,
                            }
                        ),
                        self._next_client_seq(conn),
                        occurred_at.isoformat(),
                    ),
                )
                if movement.note:
                    conn.execute(
                        """
                        INSERT INTO audit_log (
                            id, actor_id, approver_id, action, entity,
                            entity_id, before_json, after_json, occurred_at
                        ) VALUES (?, ?, NULL, ?, 'stock_ledger', ?, NULL, ?, ?)
                        """,
                        (
                            new_id(),
                            user_id,
                            f"stock.{movement.reason}",
                            movement_id,
                            json.dumps({"note": movement.note}),
                            occurred_at.isoformat(),
                        ),
                    )
                written.append(movement_id)
        return written

    def on_hand(self, product_id: str) -> int:
        """What this terminal believes it has, in thousandths.

        **Indicative, not authoritative** (§9.4), and the arithmetic says why.
        `stock_levels` on the terminal is not derived from the terminal's own
        ledger — there is no trigger here. It is a *pulled cache* of the
        cloud's number, written only by the puller and overwritten on every
        refresh. The cloud is where the trigger lives.

        So the cached level already accounts for every movement this terminal
        has successfully pushed, and for nothing it has not. A receipt taken
        two minutes ago with the line down is in the local ledger and in no
        level anywhere. Reading the cache alone would report a shelf that had
        not been restocked, and a count against that number would then
        "correct" the delivery back out of existence.

        The terminal's belief is therefore the cache plus whatever it is still
        holding: every local delta whose outbox row has not been acknowledged.
        Once the queue drains and the next pull lands, the pending set empties
        and the two agree again.
        """
        cached = self._row(
            "SELECT on_hand FROM stock_levels WHERE product_id = ?", (product_id,)
        )
        return (int(cached["on_hand"]) if cached else 0) + self.pending(product_id)

    def pending(self, product_id: str) -> int:
        """Local deltas the cloud has not acknowledged yet, in thousandths.

        A movement is pending while its outbox row is unsynced. Two shapes
        reach the ledger and they are queued differently: a stock movement is
        its own outbox row keyed on the ledger id, and a sale's deltas are
        queued as part of the sale, keyed on the sale id.
        """
        row = self._row(
            """
            SELECT COALESCE(SUM(l.delta_milli), 0) AS pending
            FROM stock_ledger l
            WHERE l.product_id = ?
              AND (
                    EXISTS (SELECT 1 FROM outbox o
                             WHERE o.synced_at IS NULL
                               AND o.entity = 'stock_movement'
                               AND o.entity_id = l.id)
                 OR (l.ref_type = 'sale' AND EXISTS (
                        SELECT 1 FROM outbox o
                         WHERE o.synced_at IS NULL
                           AND o.entity = 'sale'
                           AND o.entity_id = l.ref_id))
              )
            """,
            (product_id,),
        )
        return int(row["pending"]) if row else 0

    def unpushed(self) -> list[sqlite3.Row]:
        """Every product this terminal is holding movements for.

        There is deliberately no local reconciliation query. Reconciling the
        ledger against the levels is a *cloud-side* check — that is where the
        trigger derives one from the other, so that is the only place the two
        are claims about the same thing. On the terminal the ledger holds what
        this till did and the level holds what the cloud last said, and the
        difference between them is not an error, it is the queue.

        `scripts/reconcile_stock.sql` is the real check. This is the question
        a shopkeeper can answer without leaving the counter: what have I done
        that head office has not heard about?
        """
        return self._rows(
            """
            SELECT l.product_id                  AS product_id,
                   SUM(l.delta_milli)            AS pending
            FROM stock_ledger l
            WHERE EXISTS (SELECT 1 FROM outbox o
                           WHERE o.synced_at IS NULL
                             AND o.entity = 'stock_movement'
                             AND o.entity_id = l.id)
               OR (l.ref_type = 'sale' AND EXISTS (
                      SELECT 1 FROM outbox o
                       WHERE o.synced_at IS NULL
                         AND o.entity = 'sale'
                         AND o.entity_id = l.ref_id))
            GROUP BY l.product_id
            HAVING SUM(l.delta_milli) <> 0
            """
        )

    def _next_client_seq(self, conn: sqlite3.Connection) -> int:
        """Per-terminal ordering (§9.2). Same counter the sales path uses, so
        a movement and a sale keep their order relative to each other."""
        row = conn.execute(
            "SELECT value FROM terminal_state WHERE key = 'client_seq'"
        ).fetchone()
        nxt = int(row[0]) + 1
        conn.execute(
            "UPDATE terminal_state SET value = ? WHERE key = 'client_seq'", (str(nxt),)
        )
        return nxt

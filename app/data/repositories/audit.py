"""Audit rows the terminal writes on its own account — architecture §11.3.

Most audit rows in this system are written beside the thing they describe:
`sales.py` writes `sale.post` inside the same transaction as the sale, and
`inventory.py` does the same for a movement. They ride to the cloud attached
to their parent entity, which is why nothing has needed a repository of its
own until now.

A supervisor override has no parent. It is minted when the supervisor
authorises and may never be spent, so it cannot wait for a sale to carry it —
and a grant nobody used is still a fact about the shop. Hence this.
"""

from __future__ import annotations

import json
import sqlite3

from app.data.db import Database
from app.domain.identity import OverrideGrant
from app.domain.ids import new_id

#: The action written for a minted grant. Read by the slice 5 viewer, and by
#: anyone asking the only question an audit log is really for: who did that,
#: and who let them.
OVERRIDE_GRANTED = "override.granted"


class AuditRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record_override(self, grant: OverrideGrant) -> str:
        """Write the row that names both people, and queue it in the same breath.

        `approver_id` has existed in both schemas since 0001 and nothing has
        ever written it. This is the first thing that does, which is also why
        it is worth saying what the columns mean here: `actor_id` is the
        cashier who now holds the permission, `approver_id` is the supervisor
        who lent it. Getting those the wrong way round would produce a log
        that reads plausibly and blames the wrong person.

        **The outbox row rides the same transaction**, the rule every other
        writer here follows and the one the first version of this method
        broke. `sales.py` states it plainly: a sale that is durable but
        unqueued would never reach the cloud (§9.2). An override is worse than
        a sale in that respect, because there is no later moment that would
        notice. A quarantined sale at least sits in the failures queue where
        somebody can see it; a grant that was written and never queued leaves
        nothing pointing at it anywhere, and only a hand-written backfill
        would ever find it again. The overrides taken during whatever incident
        made someone go looking would be exactly the ones missing.

        `entity_id` is left null on purpose. The cloud column is a `uuid` and a
        permission key is not one, so the key lives in `after_json` and
        `entity` says what kind of thing the row is about. Writing it here as
        text would have worked locally and failed on the first push — which is
        how 0010 happened to this same column.
        """
        audit_id = new_id()
        with self.db.write() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    id, store_id, actor_id, approver_id, action, entity,
                    entity_id, after_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, 'permission', NULL, ?, ?)
                """,
                (
                    audit_id,
                    grant.store_id,
                    grant.actor_id,
                    grant.approver_id,
                    OVERRIDE_GRANTED,
                    json.dumps(
                        {
                            "permission": grant.permission,
                            "expires_at": grant.expires_at.isoformat(),
                            "actor_code": grant.actor_code,
                            "approver_code": grant.approver_code,
                        }
                    ),
                    grant.granted_at.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO outbox (entity, entity_id, op, payload_json,
                                    client_seq, created_at)
                VALUES ('override', ?, 'insert', ?, ?, ?)
                """,
                (
                    audit_id,
                    json.dumps(
                        {
                            "audit_id": audit_id,
                            "permission": grant.permission,
                        }
                    ),
                    self._next_client_seq(conn),
                    grant.granted_at.isoformat(),
                ),
            )
        return audit_id

    def _next_client_seq(self, conn: sqlite3.Connection) -> int:
        """Per-terminal ordering (§9.2), from the counter everything shares.

        The same counter the sales and inventory paths use, so a grant and the
        void it authorised keep their order relative to each other — which is
        the pair somebody reading the audit log actually wants.
        """
        row = conn.execute(
            "SELECT value FROM terminal_state WHERE key = 'client_seq'"
        ).fetchone()
        nxt = int(row[0]) + 1
        conn.execute(
            "UPDATE terminal_state SET value = ? WHERE key = 'client_seq'", (str(nxt),)
        )
        return nxt

    def overrides(self, limit: int = 100) -> list[dict[str, object]]:
        """Grants minted on this terminal, newest first."""
        rows = self.db.query(
            "SELECT * FROM audit_log WHERE action = ? "
            "ORDER BY occurred_at DESC LIMIT ?",
            (OVERRIDE_GRANTED, limit),
        )
        return [dict(row) for row in rows]

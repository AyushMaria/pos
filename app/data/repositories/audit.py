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
        """Write the row that names both people.

        `approver_id` has existed in both schemas since 0001 and nothing has
        ever written it. This is the first thing that does, which is also why
        it is worth saying what the columns mean here: `actor_id` is the
        cashier who now holds the permission, `approver_id` is the supervisor
        who lent it. Getting those the wrong way round would produce a log
        that reads plausibly and blames the wrong person.
        """
        audit_id = new_id()
        with self.db.write() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    id, actor_id, approver_id, action, entity, entity_id,
                    after_json, occurred_at
                ) VALUES (?, ?, ?, ?, 'permission', ?, ?, ?)
                """,
                (
                    audit_id,
                    grant.actor_id,
                    grant.approver_id,
                    OVERRIDE_GRANTED,
                    grant.permission,
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
        return audit_id

    def overrides(self, limit: int = 100) -> list[dict[str, object]]:
        """Grants minted on this terminal, newest first.

        Local-only for now, and that is a gap rather than a design: nothing
        pushes a standalone audit row, so slice 5's viewer — which reads the
        cloud — cannot see these. Held open by
        `test_an_override_row_can_be_pushed`.
        """
        rows = self.db.query(
            "SELECT * FROM audit_log WHERE action = ? "
            "ORDER BY occurred_at DESC LIMIT ?",
            (OVERRIDE_GRANTED, limit),
        )
        return [dict(row) for row in rows]

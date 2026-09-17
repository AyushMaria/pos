"""The offline identity cache — architecture §11.4.

Holds the argon2id PIN hash, the permission snapshot and the signed expiry
that together allow a till to open with the network down, bounded by a hard
TTL so that a dismissed employee cannot trade indefinitely.
"""

from __future__ import annotations

import json
from datetime import datetime

from app.data.repositories.base import Repository
from app.domain import lockout
from app.domain.identity import CachedIdentity


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class CachedUserRepository(Repository):
    def get_by_employee_code(self, employee_code: str) -> CachedIdentity | None:
        row = self._row(
            """
            SELECT user_id, employee_code, full_name, store_id, pin_hash, status,
                   roles_json, snapshot_signed_at, snapshot_expires_at,
                   consecutive_pin_failures, pin_locked_until
              FROM cached_users
             WHERE employee_code = ?
            """,
            (employee_code,),
        )
        if row is None:
            return None

        permissions = frozenset(
            r[0]
            for r in self._rows(
                "SELECT permission_key FROM cached_permissions "
                " WHERE user_id = ? AND store_id = ?",
                (row["user_id"], row["store_id"]),
            )
        )
        return CachedIdentity(
            user_id=row["user_id"],
            employee_code=row["employee_code"],
            full_name=row["full_name"],
            store_id=row["store_id"],
            pin_hash=row["pin_hash"],
            status=row["status"],
            roles=frozenset(self._json_list(row["roles_json"])),
            permissions=permissions,
            snapshot_signed_at=_parse(row["snapshot_signed_at"]),
            snapshot_expires_at=_parse(row["snapshot_expires_at"]),
            consecutive_pin_failures=int(row["consecutive_pin_failures"] or 0),
            pin_locked_until=(
                _parse(row["pin_locked_until"]) if row["pin_locked_until"] else None
            ),
        )

    def upsert(self, identity: CachedIdentity) -> None:
        """Replace a cached identity and its permission snapshot atomically.

        Called after every successful online login, which is what keeps the
        snapshot's TTL rolling forward for employees who still work here.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO cached_users (
                    user_id, employee_code, full_name, store_id, pin_hash, status,
                    roles_json, snapshot_signed_at, snapshot_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    employee_code       = excluded.employee_code,
                    full_name           = excluded.full_name,
                    store_id            = excluded.store_id,
                    pin_hash            = excluded.pin_hash,
                    status              = excluded.status,
                    roles_json          = excluded.roles_json,
                    snapshot_signed_at  = excluded.snapshot_signed_at,
                    snapshot_expires_at = excluded.snapshot_expires_at
                """,
                (
                    identity.user_id,
                    identity.employee_code,
                    identity.full_name,
                    identity.store_id,
                    identity.pin_hash,
                    identity.status,
                    json.dumps(sorted(identity.roles)),
                    _iso(identity.snapshot_signed_at),
                    _iso(identity.snapshot_expires_at),
                ),
            )
            conn.execute(
                "DELETE FROM cached_permissions WHERE user_id = ?", (identity.user_id,)
            )
            conn.executemany(
                "INSERT INTO cached_permissions (user_id, store_id, permission_key) "
                "VALUES (?, ?, ?)",
                [
                    (identity.user_id, identity.store_id, key)
                    for key in sorted(identity.permissions)
                ],
            )

    def record_pin_failure(self, user_id: str, *, now: datetime) -> datetime | None:
        """Count one wrong PIN, and lock the account if that was enough.

        Per approver, never per terminal. A supervisor who mistypes must not
        stop the cashier selling — selling is the one thing an outage may
        never interrupt (§1.1) — so nothing here touches the session at the
        till.

        Returns when the lock lifts, or None if this failure did not reach the
        threshold. Counter and lock are written together: a count that
        incremented without the lock being applied would let the next attempt
        through, which is the whole of the protection.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT consecutive_pin_failures FROM cached_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None

            failures = int(row[0] or 0) + 1
            until = lockout.locked_until(failures, now=now)
            conn.execute(
                "UPDATE cached_users SET consecutive_pin_failures = ?, "
                "pin_locked_until = ? WHERE user_id = ?",
                (failures, _iso(until) if until else None, user_id),
            )
        return until

    def clear_pin_failures(self, user_id: str) -> None:
        """A correct PIN forgets the wrong ones.

        Consecutive, not cumulative: somebody who mistypes twice a week for a
        year is not an attacker, and a counter that never resets would
        eventually lock the whole shop out for being human.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE cached_users SET consecutive_pin_failures = 0, "
                "pin_locked_until = NULL WHERE user_id = ?",
                (user_id,),
            )

    def revoke(self, user_id: str) -> None:
        """Purge a snapshot after the server reports the user deactivated.

        Emits nothing itself; the caller broadcasts ``auth.revoked`` over the
        WebSocket so an open till reacts immediately.
        """
        with self.transaction() as conn:
            conn.execute("DELETE FROM cached_permissions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM cached_users WHERE user_id = ?", (user_id,))

    def count(self) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM cached_users") or 0)

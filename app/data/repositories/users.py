"""The offline identity cache — architecture §11.4.

Holds the argon2id PIN hash, the permission snapshot and the signed expiry
that together allow a till to open with the network down, bounded by a hard
TTL so that a dismissed employee cannot trade indefinitely.

**Every row is sealed with a MAC keyed from the OS credential store.** Until
that existed, the TTL was a number in an unencrypted file on a shop counter:
editing one column extended a dismissed employee's access for another
fortnight, and every check downstream read it and believed it.
`app/security/snapshot_mac.py` says exactly what the seal does and does not
defend against — it removes the free path, not the expensive one.

A row that fails verification is treated as a row that is not there. Tampering
is the interesting cause; a lost keychain entry is the likely one, and the
remedy for both is the same: one online sign-in.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Protocol

from app.data.repositories.base import Repository
from app.domain import lockout
from app.domain.identity import CachedIdentity

log = logging.getLogger(__name__)


class Sealer(Protocol):
    """What this repository needs of a MAC, and nothing more.

    Declared here rather than imported, because `app/data` may not import
    `app/security` — the import contract keeps the layering a rule instead of
    a habit. The implementation lives in `app.security.snapshot_mac` and is
    handed in by whoever is allowed to know about both.
    """

    def seal(self, fields: dict) -> str: ...

    def verify(self, fields: dict, mac: str | None) -> bool: ...


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class CachedUserRepository(Repository):
    """The cache, and the key that makes reading it meaningful.

    `sealer` is required rather than optional on purpose. An optional one is a
    bypass that ships: every test would take the unsealed path, the production
    path would be the one nobody exercised, and "sealing is off" would be one
    missing argument away.
    """

    def __init__(self, db, sealer: Sealer) -> None:
        super().__init__(db)
        self.sealer = sealer

    # ── Sealing ─────────────────────────────────────────────────────────────

    def _fields(self, row, permissions) -> dict:
        """The sealed view of a row, built the same way on both sides.

        One function for sealing and verifying, because two would eventually
        disagree about a field and the symptom would be every identity on the
        terminal failing at once.
        """
        return {
            "user_id": row["user_id"],
            "employee_code": row["employee_code"],
            "store_id": row["store_id"],
            "pin_hash": row["pin_hash"],
            "status": row["status"],
            "roles_json": row["roles_json"],
            "snapshot_signed_at": row["snapshot_signed_at"],
            "snapshot_expires_at": row["snapshot_expires_at"],
            "consecutive_pin_failures": int(row["consecutive_pin_failures"] or 0),
            "pin_locked_until": row["pin_locked_until"],
            "permissions": sorted(permissions),
        }

    def _reseal(self, conn, user_id: str) -> None:
        """Re-stamp a row after this terminal changed it.

        The lockout counters are inside the seal, which costs an HMAC on every
        wrong PIN — microseconds, in a path that has just spent 22.8ms on
        argon2. Leaving them outside would have been cheaper and would have
        left `pin_locked_until = NULL` as a free edit, which is the one thing
        somebody guessing a supervisor's PIN at this till would most like to
        make.
        """
        row = conn.execute(
            "SELECT * FROM cached_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - the caller just wrote it
            return
        permissions = [
            r[0]
            for r in conn.execute(
                "SELECT permission_key FROM cached_permissions "
                " WHERE user_id = ? AND store_id = ?",
                (user_id, row["store_id"]),
            ).fetchall()
        ]
        conn.execute(
            "UPDATE cached_users SET row_mac = ? WHERE user_id = ?",
            (self.sealer.seal(self._fields(row, permissions)), user_id),
        )

    def get_by_employee_code(self, employee_code: str) -> CachedIdentity | None:
        row = self._row(
            """
            SELECT user_id, employee_code, full_name, store_id, pin_hash, status,
                   roles_json, snapshot_signed_at, snapshot_expires_at,
                   consecutive_pin_failures, pin_locked_until, row_mac
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
        if not self.sealer.verify(self._fields(row, permissions), row["row_mac"]):
            # Not an exception. The caller's next move is the same one it
            # makes for an employee this terminal has never seen — ask for an
            # online sign-in — and raising here would turn a moved Windows
            # profile into a till that will not open.
            #
            # Logged loudly because the two causes are very different and only
            # one of them is housekeeping.
            log.error(
                "cached identity for %s failed its seal and will not be used; "
                "this is either tampering or a lost keychain entry, and both "
                "need one online sign-in to clear",
                employee_code,
            )
            return None

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
            # Last, inside the same transaction: the seal has to cover the
            # permissions that were just written, and a row that committed
            # without one would be unreadable rather than merely stale.
            self._reseal(conn, identity.user_id)

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
            self._reseal(conn, user_id)
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
            self._reseal(conn, user_id)

    def cached_user_ids(self) -> list[str]:
        """Every identity this terminal remembers.

        The input to a revocation check. Unsealed on purpose: this asks *which
        rows exist*, not what they say, and a tampered row that is about to be
        purged anyway should still be named rather than hidden by its own
        broken seal.
        """
        return [r[0] for r in self._rows("SELECT user_id FROM cached_users")]

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

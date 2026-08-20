"""Terminal-local counters.

Receipt numbers are ``{STORE}-{TERMINAL}-{SEQ}`` from a monotonic counter, so
two terminals never collide and no coordination with the server is needed
(architecture §7). ``client_seq`` orders outbox rows per terminal, which is
what preserves causality on push — a sale before its payment (§9.2).
"""

from __future__ import annotations

from app.data.repositories.base import Repository


class TerminalRepository(Repository):
    def _bump(self, key: str) -> int:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM terminal_state WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                raise KeyError(f"terminal_state has no counter {key!r}")
            nxt = int(row[0]) + 1
            conn.execute(
                "UPDATE terminal_state SET value = ? WHERE key = ?", (str(nxt), key)
            )
        return nxt

    def next_receipt_no(self, store_code: str, terminal_code: str) -> str:
        return f"{store_code}-{terminal_code}-{self._bump('receipt_seq'):06d}"

    def next_client_seq(self) -> int:
        return self._bump("client_seq")

    def get(self, key: str) -> str | None:
        value = self._scalar("SELECT value FROM terminal_state WHERE key = ?", (key,))
        return None if value is None else str(value)

    def set(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO terminal_state (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

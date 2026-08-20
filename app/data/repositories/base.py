"""Repository base.

Every repository takes a :class:`Database` and does its writing through
``self.db.write()``, which is the single write connection behind a lock. No
repository opens a connection of its own, and no code outside this package
writes SQL against the terminal database.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from app.data.db import Database


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Reads ───────────────────────────────────────────────────────────────

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.db.query(sql, tuple(params))

    def _row(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.db.query_one(sql, tuple(params))

    def _scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self._row(sql, params)
        return None if row is None else row[0]

    # ── Writes ──────────────────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several writes into one atomic unit."""
        with self.db.write() as conn:
            yield conn

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.db.write() as conn:
            conn.execute(sql, tuple(params))

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _json_list(raw: str | None) -> list[str]:
        if not raw:
            return []
        parsed = json.loads(raw)
        return [str(item) for item in parsed]

"""SQLite connection management — architecture §8.

WAL allows many readers and exactly one writer. That constraint is made
explicit here rather than being discovered as ``database is locked`` at a busy
till: there is one write connection guarded by a ``threading.Lock``, and a
thread-local pool of read-only connections.

Nothing outside this module and ``app.data.repositories`` may open a
connection. CI enforces the layering; code review enforces the rest.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# `synchronous=FULL` on the writer: a sale that survives the API call must
# survive the power cut too. Readers stay on NORMAL.
_WRITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)

_READ_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA query_only=ON",
)


def _configure(conn: sqlite3.Connection, pragmas: tuple[str, ...]) -> None:
    conn.row_factory = sqlite3.Row
    for pragma in pragmas:
        conn.execute(pragma)


class Database:
    """Owns every connection to one SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._write_lock = threading.Lock()
        self._write_conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        _configure(self._write_conn, _WRITE_PRAGMAS)

        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._closed = False

    # ── Reading ─────────────────────────────────────────────────────────────

    def reader(self) -> sqlite3.Connection:
        """A read-only connection belonging to the calling thread."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            _configure(conn, _READ_PRAGMAS)
            self._local.conn = conn
            with self._readers_lock:
                self._readers.append(conn)
        return conn

    def query(self, sql: str, params: object = ()) -> list[sqlite3.Row]:
        return self.reader().execute(sql, params).fetchall()  # type: ignore[arg-type]

    def query_one(self, sql: str, params: object = ()) -> sqlite3.Row | None:
        return self.reader().execute(sql, params).fetchone()  # type: ignore[arg-type]

    # ── Writing ─────────────────────────────────────────────────────────────

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction.

        Everything inside the block commits together or not at all — this is
        the boundary ``sale_service.post_sale`` relies on in phase 3.
        """
        with self._write_lock:
            conn = self._write_conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def execute_script(self, sql: str) -> None:
        """Run a multi-statement script as one transaction.

        ``sqlite3.executescript`` issues its own COMMIT before running, so it
        cannot be nested inside :meth:`write`. The BEGIN/COMMIT is therefore
        part of the script itself. Only the migration runner should need this;
        everything else belongs in a repository.
        """
        with self._write_lock:
            self._write_conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @property
    def user_version(self) -> int:
        row = self._write_conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._readers_lock:
            readers, self._readers = self._readers, []
        for conn in readers:
            conn.close()
        with self._write_lock:
            self._write_conn.close()

"""SQLite discipline — architecture §8.

The failure this file guards against is ``database is locked`` appearing at a
busy till, which is what happens when writes are allowed to come from anywhere.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.data.db import Database
from app.data.migrations import migrate
from app.data.repositories.terminal import TerminalRepository


def test_wal_and_foreign_keys_are_on(db: Database) -> None:
    assert db.query_one("PRAGMA journal_mode")[0].lower() == "wal"
    assert db.query_one("PRAGMA foreign_keys")[0] == 1


def test_reader_connections_are_read_only(db: Database) -> None:
    with pytest.raises(sqlite3.OperationalError):
        db.reader().execute(
            "INSERT INTO terminal_state (key, value) VALUES ('x', 'y')"
        )


def test_write_transaction_rolls_back_on_failure(db: Database) -> None:
    with pytest.raises(RuntimeError), db.write() as conn:
        conn.execute(
            "INSERT INTO terminal_state (key, value) VALUES ('partial', '1')"
        )
        raise RuntimeError("interrupted mid-sale")

    assert db.query_one("SELECT value FROM terminal_state WHERE key='partial'") is None


def test_write_transaction_commits_together(db: Database) -> None:
    with db.write() as conn:
        conn.execute("INSERT INTO terminal_state (key, value) VALUES ('a', '1')")
        conn.execute("INSERT INTO terminal_state (key, value) VALUES ('b', '2')")

    assert db.query_one("SELECT value FROM terminal_state WHERE key='a'")[0] == "1"
    assert db.query_one("SELECT value FROM terminal_state WHERE key='b'")[0] == "2"


def test_concurrent_writers_do_not_collide(db: Database) -> None:
    """Ten threads bumping the same counter must produce ten distinct values.

    A lost update here would mean two sales sharing a receipt number.
    """
    repo = TerminalRepository(db)
    results: list[str] = []
    lock = threading.Lock()

    def bump() -> None:
        value = repo.next_receipt_no("ST01", "T1")
        with lock:
            results.append(value)

    threads = [threading.Thread(target=bump) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(results)) == 10
    assert sorted(results)[0].endswith("000001")
    assert sorted(results)[-1].endswith("000010")


def test_receipt_number_format(db: Database) -> None:
    assert TerminalRepository(db).next_receipt_no("ST01", "T2") == "ST01-T2-000001"


def test_readers_see_committed_writes(tmp_path: Path) -> None:
    db = Database(tmp_path / "pos.sqlite3")
    migrate(db)
    repo = TerminalRepository(db)

    repo.set("greeting", "hello")
    assert repo.get("greeting") == "hello"

    repo.set("greeting", "goodbye")
    assert repo.get("greeting") == "goodbye"
    db.close()

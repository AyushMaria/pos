"""The migration runner.

A terminal that has been offline for a fortnight comes back and upgrades its
schema unattended, so the interesting cases are the failures: a half-applied
migration, a numbering gap, and a database written by a newer build.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.data.db import Database
from app.data.migrations import MigrationError, discover, latest_version, migrate


def test_ships_at_least_one_migration() -> None:
    assert latest_version() >= 1


def test_migrations_are_numbered_without_gaps() -> None:
    versions = [m.version for m in discover()]
    assert versions == list(range(1, len(versions) + 1))


def test_migrate_applies_and_records_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "pos.sqlite3")
    assert db.user_version == 0

    applied = migrate(db)

    assert applied == latest_version()
    assert db.user_version == applied
    db.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "pos.sqlite3")
    first = migrate(db)
    second = migrate(db)
    assert first == second
    db.close()


def test_expected_tables_exist(tmp_path: Path) -> None:
    db = Database(tmp_path / "pos.sqlite3")
    migrate(db)

    names = {
        row[0]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table in (
        "products",
        "product_barcodes",
        "cached_users",
        "cached_permissions",
        "sales",
        "sale_lines",
        "payments",
        "payment_attempts",
        "stock_ledger",
        "audit_log",
        "outbox",
        "sync_state",
        "sync_failures",
        "terminal_state",
    ):
        assert table in names, f"missing table {table}"
    db.close()


def test_money_columns_are_integers(tmp_path: Path) -> None:
    """Architecture §17: float money must never reach the database.

    SQLite's flexible typing means a REAL column would accept integers happily
    in development and start producing fractional paise in production.
    """
    db = Database(tmp_path / "pos.sqlite3")
    migrate(db)

    money_columns = {
        "sales": ["subtotal", "discount_total", "tax_total", "rounding_adjustment",
                  "grand_total"],
        "sale_lines": ["unit_price", "discount_amount", "tax_amount", "line_total",
                       "qty_milli"],
        "payments": ["amount"],
        "payment_attempts": ["amount"],
        "product_prices": ["price", "cost"],
        "stock_ledger": ["delta_milli"],
        "cash_movements": ["amount"],
    }

    for table, columns in money_columns.items():
        types = {
            row["name"]: row["type"] for row in db.query(f"PRAGMA table_info({table})")
        }
        for column in columns:
            assert types[column] == "INTEGER", (
                f"{table}.{column} is {types[column]}, must be INTEGER minor units"
            )
    db.close()


def test_rejects_a_numbering_gap(tmp_path: Path) -> None:
    (tmp_path / "001_init.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "003_later.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationError, match="gap or duplicate"):
        discover(tmp_path)


def test_rejects_a_badly_named_file(tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationError, match="NNN_lower_snake_case"):
        discover(tmp_path)


def test_refuses_to_downgrade(tmp_path: Path) -> None:
    """A database written by a newer build must not be silently opened."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_init.sql").write_text(
        "CREATE TABLE t (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )

    db = Database(tmp_path / "pos.sqlite3")
    migrate(db, migrations)
    db.execute_script("PRAGMA user_version = 9;")

    with pytest.raises(MigrationError, match="Update the application"):
        migrate(db, migrations)
    db.close()


def test_a_failing_migration_leaves_the_version_behind(tmp_path: Path) -> None:
    """Half a schema is worse than no upgrade — the whole file must roll back."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_init.sql").write_text(
        "CREATE TABLE good (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (migrations / "002_broken.sql").write_text(
        "CREATE TABLE partial (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE partial (id INTEGER PRIMARY KEY);",  # duplicate: fails
        encoding="utf-8",
    )

    db = Database(tmp_path / "pos.sqlite3")
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        migrate(db, migrations)

    assert db.user_version == 1
    tables = {row[0] for row in db.query("SELECT name FROM sqlite_master")}
    assert "good" in tables
    assert "partial" not in tables
    db.close()

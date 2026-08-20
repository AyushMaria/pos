"""Refresh-token storage — architecture §11.2.

The refresh token goes in the OS credential store, never in SQLite and never
in a config file: the terminal database is backed up on every shift close and
those backups get copied around.
"""

from __future__ import annotations

import pytest

from app.security import keychain

# This module is entirely about the credential store, so every test needs the
# in-memory stub standing in for the real one.
pytestmark = pytest.mark.usefixtures("in_memory_keychain")


def test_round_trip(in_memory_keychain: dict) -> None:
    keychain.save_refresh_token("ST01", "T1", "refresh-abc")
    assert keychain.load_refresh_token("ST01", "T1") == "refresh-abc"


def test_terminals_do_not_share_credentials(in_memory_keychain: dict) -> None:
    keychain.save_refresh_token("ST01", "T1", "token-one")
    keychain.save_refresh_token("ST01", "T2", "token-two")

    assert keychain.load_refresh_token("ST01", "T1") == "token-one"
    assert keychain.load_refresh_token("ST01", "T2") == "token-two"


def test_clearing_removes_it(in_memory_keychain: dict) -> None:
    keychain.save_refresh_token("ST01", "T1", "refresh-abc")
    keychain.clear_refresh_token("ST01", "T1")
    assert keychain.load_refresh_token("ST01", "T1") is None


def test_missing_credential_reads_as_none(in_memory_keychain: dict) -> None:
    assert keychain.load_refresh_token("ST99", "T9") is None


def test_an_unavailable_store_does_not_stop_the_till(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential store just means one more online login next launch."""
    from keyring.errors import KeyringError

    def explode(*args: object, **kwargs: object) -> None:
        raise KeyringError("no backend available")

    monkeypatch.setattr("app.security.keychain.keyring.get_password", explode)
    assert keychain.load_refresh_token("ST01", "T1") is None


def test_the_token_never_reaches_sqlite(db: object) -> None:
    """No schema column anywhere is allowed to hold a refresh token."""
    from app.data.db import Database

    assert isinstance(db, Database)
    columns = [
        row[1].lower()
        for table in db.query("SELECT name FROM sqlite_master WHERE type='table'")
        for row in db.query(f"PRAGMA table_info({table[0]})")
    ]
    assert not any("refresh" in column or "access_token" in column for column in columns)

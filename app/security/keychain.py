"""Refresh-token storage in the OS keychain — architecture §11.2.

Windows Credential Manager, macOS Keychain, Secret Service on Linux. Never
SQLite and never a config file: the terminal database is backed up on every
shift close and those backups get copied around.

Only the refresh token is stored. The access token is short-lived (1 hour, so
revocations stay timely) and lives in memory for the life of the session.
"""

from __future__ import annotations

import logging
from contextlib import suppress

import keyring
from keyring.errors import KeyringError

log = logging.getLogger(__name__)

SERVICE = "RetailPOS"


class KeychainUnavailable(RuntimeError):
    """No usable OS credential store on this machine."""


def _account(store_code: str, terminal_code: str) -> str:
    return f"{store_code}:{terminal_code}:refresh_token"


def save_refresh_token(store_code: str, terminal_code: str, token: str) -> None:
    try:
        keyring.set_password(SERVICE, _account(store_code, terminal_code), token)
    except KeyringError as exc:
        raise KeychainUnavailable(str(exc)) from exc


def load_refresh_token(store_code: str, terminal_code: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, _account(store_code, terminal_code))
    except KeyringError as exc:
        # A missing credential store must not stop the till opening — it only
        # means the cashier authenticates online again rather than resuming.
        log.warning("keychain unavailable, refresh token not restored: %s", exc)
        return None


def clear_refresh_token(store_code: str, terminal_code: str) -> None:
    # Nothing to do if it was never stored, and a missing backend is not a
    # reason to fail a sign-out.
    with suppress(KeyringError):
        keyring.delete_password(SERVICE, _account(store_code, terminal_code))

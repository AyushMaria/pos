"""Authenticating the offline identity cache — architecture §11.4.

## What this defends against, and what it does not

`cached_users` is an unencrypted SQLite file on a shop counter. It holds the
argon2id PIN hash, the permission snapshot and the expiry that bounds how long
a dismissed employee can keep trading. Until this existed, extending that
expiry was a text edit: open the file, change one column, and a revoked
supervisor authorises overrides for another fortnight. Nothing anywhere would
report an error, because every check downstream read the number and believed
it.

The MAC does not make the file secret, and it is worth being exact about that,
because a defence described as more than it is gets relied on for more than it
does. Anyone who can edit the database can also *read* `pin_hash`, carry it
away and grind it offline at whatever speed their hardware allows — with no
lockout, because the lockout lives in the file they just copied.

What the MAC removes is the **free** path. Cracking argon2id at the shipped
parameters (m=65536, t=3, p=4) costs real time per guess. Editing a column
costs none. Those are not the same threat, and closing the cheap one is worth
doing even though the expensive one remains.

## The key

Random, 32 bytes, minted on first use and kept in the OS credential store
beside the refresh token — never in SQLite, because the terminal database is
copied around on every shift-close backup and a key stored next to the thing
it authenticates is decoration.

## When verification fails

Two causes, and the safe answer is the same for both: the row is not trusted
and the terminal behaves as though it had never seen that employee, which
means one online sign-in. Tampering is the interesting cause; a lost keychain
entry — a reinstall, a moved Windows profile — is the likely one, and a till
that refused to open in that case would turn a housekeeping accident into a
closed shop.

A row written before this module existed has no MAC at all. That is also not
trusted, so upgrading a terminal costs one online login. Trusting unsealed
rows "just this once" would leave the door open permanently: an attacker would
simply delete the column's contents.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from hashlib import sha256
from typing import Any

import keyring
from keyring.errors import KeyringError

from app.security.keychain import SERVICE, KeychainUnavailable

log = logging.getLogger(__name__)

#: The fields a MAC covers. Every one of them decides access.
#:
#: Ordered, because the serialisation below is positional and a reordering
#: would invalidate every row on the terminal at once — which is safe, but
#: would look like mass tampering to whoever read the logs.
SEALED_FIELDS: tuple[str, ...] = (
    "user_id",
    "employee_code",
    "store_id",
    "pin_hash",
    "status",
    "roles_json",
    "snapshot_signed_at",
    "snapshot_expires_at",
    "consecutive_pin_failures",
    "pin_locked_until",
    "permissions",
)


def _account(store_code: str, terminal_code: str) -> str:
    return f"{store_code}:{terminal_code}:snapshot_mac_key"


def mac_key(store_code: str, terminal_code: str) -> bytes:
    """The terminal's sealing key, minted on first use.

    Per terminal rather than per shop: the key never leaves this machine, so a
    database copied from one till to another fails verification everywhere —
    which is the correct answer to somebody moving a file around.
    """
    account = _account(store_code, terminal_code)
    try:
        existing = keyring.get_password(SERVICE, account)
        if existing:
            return bytes.fromhex(existing)
        minted = secrets.token_bytes(32)
        keyring.set_password(SERVICE, account, minted.hex())
        return minted
    except KeyringError as exc:
        raise KeychainUnavailable(str(exc)) from exc


def _canonical(fields: dict[str, Any]) -> bytes:
    """One byte string for a row, with no way to shuffle its meaning.

    JSON with sorted keys and explicit separators, over a dict built from
    `SEALED_FIELDS` alone. Concatenating values with a delimiter would let
    `("AB", "C")` and `("A", "BC")` seal identically, which is the classic way
    a MAC over several fields authenticates the wrong thing.
    """
    sealed = {name: fields.get(name) for name in SEALED_FIELDS}
    # Permissions are a set on the way in and a list on the way out; sorting
    # makes the seal independent of iteration order.
    permissions = sealed.get("permissions")
    if permissions is not None:
        sealed["permissions"] = sorted(permissions)
    return json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal(fields: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, _canonical(fields), sha256).hexdigest()


def verify(fields: dict[str, Any], mac: str | None, key: bytes) -> bool:
    """Constant-time comparison, and `None` is never valid.

    `compare_digest` rather than `==`: a timing oracle on a MAC is how a
    forgery gets built one byte at a time, and it costs nothing to avoid.
    """
    if not mac:
        return False
    return hmac.compare_digest(seal(fields, key), mac)


class SnapshotSealer:
    """A key, bound to the two operations that use it.

    An object rather than two loose functions and a key passed around, because
    `app/data` may not import `app/security` — the import contract in
    `pyproject.toml` says api -> services -> {domain, data}, and it is right:
    a repository that reached into the security package would make the layering
    a matter of habit rather than a rule.

    So the repository takes something with `seal` and `verify` on it and never
    learns where the key came from. `server.py`, which is allowed to know both,
    builds this and hands it over. Inverting the dependency is cheaper than
    arguing with the contract, and the contract was right.
    """

    def __init__(self, key: bytes) -> None:
        self._key = key

    def seal(self, fields: dict[str, Any]) -> str:
        return seal(fields, self._key)

    def verify(self, fields: dict[str, Any], mac: str | None) -> bool:
        return verify(fields, mac, self._key)

"""argon2id PIN hashing — architecture §11.4.

Tuned to roughly 100 ms on target hardware rather than left at the library
default. A 500 ms PIN check is felt at a queue, and a supervisor override
happens with a customer waiting; run ``scripts/tune_argon2.py`` on the actual
till and put the resulting parameters in the environment.

A PIN is short and low-entropy, so the cost parameters are doing all the work.
The same parameters must be used by the ``authenticate-pin`` Edge Function,
otherwise a hash minted in the cloud cannot be verified on the terminal.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from app.config import Settings, get_settings

MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 12


class WeakPin(ValueError):
    """Raised when a PIN fails the minimum policy."""


def _hasher(settings: Settings) -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost_kib,
        parallelism=settings.argon2_parallelism,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


def validate_pin(pin: str) -> None:
    if not pin.isdigit():
        raise WeakPin("PIN must be digits only")
    if not MIN_PIN_LENGTH <= len(pin) <= MAX_PIN_LENGTH:
        raise WeakPin(f"PIN must be {MIN_PIN_LENGTH}-{MAX_PIN_LENGTH} digits")
    if len(set(pin)) == 1:
        raise WeakPin("PIN must not be a single repeated digit")


def hash_pin(pin: str, settings: Settings | None = None) -> str:
    """Hash a PIN after checking it against the minimum policy."""
    validate_pin(pin)
    return _hasher(settings or get_settings()).hash(pin)


def verify_pin(pin_hash: str, pin: str, settings: Settings | None = None) -> bool:
    """Constant-time-ish verification. Never raises on a wrong PIN."""
    try:
        return _hasher(settings or get_settings()).verify(pin_hash, pin)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(pin_hash: str, settings: Settings | None = None) -> bool:
    """True when the stored hash predates a change in cost parameters."""
    try:
        return _hasher(settings or get_settings()).check_needs_rehash(pin_hash)
    except InvalidHashError:
        return True

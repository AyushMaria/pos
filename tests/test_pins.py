"""PIN hashing — architecture §11.4."""

from __future__ import annotations

import time

import pytest

from app.config import Settings, get_settings
from app.security.pins import WeakPin, hash_pin, needs_rehash, validate_pin, verify_pin


@pytest.fixture
def fast() -> Settings:
    return Settings(argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_parallelism=1)


def test_hash_and_verify(fast: Settings) -> None:
    stored = hash_pin("4913", fast)
    assert verify_pin(stored, "4913", fast) is True


def test_wrong_pin_is_rejected(fast: Settings) -> None:
    stored = hash_pin("4913", fast)
    assert verify_pin(stored, "4914", fast) is False


def test_same_pin_hashes_differently(fast: Settings) -> None:
    """Distinct salts: identical PINs must not produce identical hashes."""
    assert hash_pin("4913", fast) != hash_pin("4913", fast)


def test_uses_argon2id(fast: Settings) -> None:
    assert hash_pin("4913", fast).startswith("$argon2id$")


def test_verify_never_raises_on_garbage(fast: Settings) -> None:
    assert verify_pin("not-a-hash", "4913", fast) is False
    assert verify_pin("", "4913", fast) is False


def test_rehash_detected_when_cost_changes(fast: Settings) -> None:
    stored = hash_pin("4913", fast)
    stronger = Settings(
        argon2_time_cost=3, argon2_memory_cost_kib=8192, argon2_parallelism=1
    )
    assert needs_rehash(stored, fast) is False
    assert needs_rehash(stored, stronger) is True


@pytest.mark.parametrize(
    "pin,reason",
    [
        ("123", "too short"),
        ("1234567890123", "too long"),
        ("12a4", "not digits"),
        ("1111", "single repeated digit"),
        ("", "empty"),
    ],
)
def test_weak_pins_are_refused(pin: str, reason: str, fast: Settings) -> None:
    with pytest.raises(WeakPin):
        hash_pin(pin, fast)


def test_reasonable_pins_are_accepted() -> None:
    for pin in ("4913", "1000", "849163", "12345678"):
        validate_pin(pin)


def test_shipped_parameters_are_in_the_right_range() -> None:
    """The committed defaults must be a considered choice, not the library's.

    Too fast and a four-digit PIN falls to an offline attack on a stolen
    terminal database; too slow and a supervisor override stalls a queue.
    """
    settings = get_settings()
    assert settings.argon2_memory_cost_kib >= 19 * 1024  # OWASP floor
    assert settings.argon2_time_cost >= 2
    assert settings.argon2_parallelism >= 1


@pytest.mark.slow
def test_shipped_parameters_cost_roughly_100ms() -> None:
    """Guards the tuning, not the security. Re-tune on the till before pilot."""
    settings = get_settings()
    start = time.perf_counter()
    hash_pin("849163", settings)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert 20 < elapsed_ms < 600, f"argon2id took {elapsed_ms:.0f} ms"

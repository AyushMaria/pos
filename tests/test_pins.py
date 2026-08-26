"""PIN hashing — architecture §11.4."""

from __future__ import annotations

import re
import time
from pathlib import Path

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
    """Guards the tuning, not the security. Re-tune on the till before pilot.

    The floor is low because the binding constraint is not this machine: the
    same parameters run as WebAssembly inside `authenticate-pin`, roughly 50×
    slower, and t=12 measured 3.7–5.3 s there against ~87 ms here. Native cost
    is now a few milliseconds and that is expected — 5 ms still fails on
    parameters set to nothing, which is all this bound is for.
    """
    settings = get_settings()
    start = time.perf_counter()
    hash_pin("849163", settings)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert 5 < elapsed_ms < 600, f"argon2id took {elapsed_ms:.0f} ms"


# ── The Edge Function has to agree, and nothing else would notice ───────────

EDGE_FUNCTION = (
    Path(__file__).resolve().parent.parent
    / "supabase" / "functions" / "authenticate-pin" / "index.ts"
)


def _shipped_params() -> str:
    settings = get_settings()
    return (
        f"m={settings.argon2_memory_cost_kib},"
        f"t={settings.argon2_time_cost},"
        f"p={settings.argon2_parallelism}"
    )


def test_the_edge_function_mints_at_the_shipped_parameters() -> None:
    """It re-mints a hash made at any other cost, so its constants must be the
    ones this repository ships, or every account migrates to the wrong thing."""
    source = EDGE_FUNCTION.read_text(encoding="utf-8")
    settings = get_settings()

    assert f"memoryKiB: {settings.argon2_memory_cost_kib}" in source
    assert f"timeCost: {settings.argon2_time_cost}" in source
    assert f"parallelism: {settings.argon2_parallelism}" in source


def _decoy_hash() -> str:
    """The decoy as the Edge Function will actually use it, joined if split."""
    source = EDGE_FUNCTION.read_text(encoding="utf-8")
    match = re.search(
        r'const DECOY_HASH =\s*((?:"[^"]*"\s*\+?\s*)+);', source, re.MULTILINE
    )
    assert match, "no DECOY_HASH in the Edge Function"
    return "".join(re.findall(r'"([^"]*)"', match.group(1)))


def test_the_decoy_hash_costs_what_a_real_one_costs() -> None:
    """The timing oracle this closes was real.

    An unknown employee code is verified against a decoy so that it costs the
    same as a wrong PIN. The decoy shipped at `t=2` while real hashes were
    `t=12`, so an unknown code answered roughly six times faster and codes
    could be enumerated with a stopwatch — the exact attack the decoy exists to
    prevent, defeated by the decoy itself.

    `needs_rehash` is doing two jobs here. It is False only when the string is
    a *well-formed* argon2 hash **and** carries the shipped parameters, so this
    catches a decoy left as a placeholder as well as one left at an old cost.
    A malformed decoy is the worse failure: `argon2Verify` throws on it
    immediately, so an unknown code returns in no time at all and the oracle is
    wider than before.
    """
    assert needs_rehash(_decoy_hash()) is False, (
        "the decoy is not a valid hash at the shipped parameters — "
        "run scripts/remint_pin_hashes.py"
    )


def test_the_seed_hashes_are_minted_at_the_shipped_parameters() -> None:
    """Otherwise a freshly seeded project pays the old cost on every sign-in
    until each account happens to migrate itself."""
    seed = (
        Path(__file__).resolve().parent.parent
        / "supabase" / "seed" / "seed.sql"
    ).read_text(encoding="utf-8")

    minted = re.findall(r"\$argon2id\$v=19\$m=[^']*", seed)

    assert len(minted) == 3, f"expected three seeded PINs, found {len(minted)}"
    for stored in minted:
        assert needs_rehash(stored) is False, (
            f"a seeded PIN was minted at other parameters than {_shipped_params()} — "
            "run scripts/remint_pin_hashes.py"
        )

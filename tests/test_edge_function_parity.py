"""Constants duplicated into Deno must agree with the Python that ships.

`supabase/functions/*/index.ts` runs in a Deno isolate with no access to
`app/`, so anything it needs from the domain is retyped. There is no import to
break and no type to fail: a number that drifts here is simply a different
number, and the only symptom is behaviour nobody is watching. A snapshot TTL
that says 14 on the terminal and 30 in the cloud produces a till that trusts a
dismissed employee for a fortnight longer than the design says, and nothing
anywhere reports an error.

`tests/test_pins.py` already does this for the argon2 parameters, and the
Edge Function's own comments say why it matters there. The phase 7 plan
recorded that `SNAPSHOT_TTL` had no such test — "two independent constants,
**no parity test** (contrast the argon2 pair, which has one)".

This file scans **every** function directory rather than a named file, so the
next one to be written is covered the day it lands rather than the day
somebody remembers. `authorize-override` is about to be the second, and a
third copy of a constant is where this stops being hypothetical.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import get_settings
from app.domain.identity import OVERRIDE_GRANT_TTL, SNAPSHOT_TTL

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "supabase" / "functions"


def edge_sources() -> dict[str, str]:
    """Every Edge Function's source, keyed by function name."""
    return {
        path.parent.name: path.read_text(encoding="utf-8")
        for path in sorted(FUNCTIONS_DIR.glob("*/index.ts"))
    }


def test_there_are_edge_functions_to_check() -> None:
    """The positive control.

    Every assertion below is "if this function declares the constant, it must
    match". A directory that has moved, or a glob that has stopped matching,
    makes all of them vacuous — the suite would report parity across an empty
    set and go green.
    """
    sources = edge_sources()
    assert sources, f"no index.ts under {FUNCTIONS_DIR} — the scan found nothing"
    assert "authenticate-pin" in sources, sorted(sources)


#: Each entry: the constant as it is written in TypeScript, and the Python
#: value it has to equal. Declared as a table so adding a third duplicated
#: constant is one line rather than one test.
DUPLICATED = {
    "SNAPSHOT_TTL_DAYS": lambda: SNAPSHOT_TTL.days,
    "OVERRIDE_GRANT_TTL_SECONDS": lambda: int(OVERRIDE_GRANT_TTL.total_seconds()),
}


@pytest.mark.parametrize("constant", sorted(DUPLICATED))
def test_a_duplicated_constant_matches_the_python(constant: str) -> None:
    """Wherever a function declares it, the value must be Python's.

    Absence is fine — not every function needs every constant. Disagreement is
    not, and it is invisible from either side.
    """
    expected = DUPLICATED[constant]()
    pattern = re.compile(rf"const\s+{constant}\s*=\s*(\d+)")

    for name, source in edge_sources().items():
        match = pattern.search(source)
        if match is None:
            continue
        assert int(match.group(1)) == expected, (
            f"{name}/index.ts has {constant} = {match.group(1)}, "
            f"but this repository ships {expected}. One of them is wrong and "
            f"neither side would ever notice."
        )


def test_the_snapshot_ttl_is_actually_duplicated_somewhere() -> None:
    """The other half of the control, and the reason this file exists.

    `test_a_duplicated_constant_matches_the_python` passes when nothing
    declares the constant, which is also what a renamed constant looks like.
    `authenticate-pin` is known to carry `SNAPSHOT_TTL_DAYS`; if it stops, that
    is either a real removal or a rename that has just silenced the check, and
    both deserve a look.
    """
    source = edge_sources()["authenticate-pin"]
    assert re.search(r"const\s+SNAPSHOT_TTL_DAYS\s*=\s*\d+", source), (
        "authenticate-pin no longer declares SNAPSHOT_TTL_DAYS. If it was "
        "renamed, rename it in DUPLICATED too — otherwise the parity check "
        "above is now checking nothing."
    )


def test_the_argon2_parameters_still_agree() -> None:
    """Kept here as well as in test_pins.py, deliberately.

    `test_pins.py` asserts this as part of what minting a hash means; this file
    asserts it as one instance of the general rule. The duplication is two
    tests over one fact, which is cheaper than the fact going unchecked
    because each file assumed the other had it.
    """
    settings = get_settings()
    source = edge_sources()["authenticate-pin"]

    assert f"memoryKiB: {settings.argon2_memory_cost_kib}" in source
    assert f"timeCost: {settings.argon2_time_cost}" in source
    assert f"parallelism: {settings.argon2_parallelism}" in source


def test_no_function_hardcodes_a_service_role_key() -> None:
    """§1.7, at the one place a service_role key legitimately exists.

    An Edge Function is the only thing in this repository entitled to use it,
    and it must come from the environment rather than the source, because the
    source is in git.
    """
    from_env = 'Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")'
    for name, source in edge_sources().items():
        if "SUPABASE_SERVICE_ROLE_KEY" in source:
            assert from_env in source, (
                f"{name}/index.ts names the service_role key without reading "
                "it from the environment"
            )
        assert not re.search(r'["\']eyJ[A-Za-z0-9_-]{20,}', source), (
            f"{name}/index.ts appears to contain a literal JWT"
        )

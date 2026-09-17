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
from app.domain import lockout
from app.domain.identity import OVERRIDE_GRANT_TTL, SNAPSHOT_TTL

FUNCTIONS_DIR = Path(__file__).resolve().parent.parent / "supabase" / "functions"


def _code_only(source: str) -> str:
    """The source with its whole-line `//` comments removed.

    Written because the first run of `test_only_the_login_function_mints_a_session`
    flagged `authorize-override` — for a comment saying, in as many words, that
    it must never call `generateLink` or `verifyOtp`. A text search cannot tell
    a warning from the thing it warns about, and a check that punishes the
    explanation teaches people to delete the explanation.

    Only comments that start a line are stripped, which is every comment in
    this directory. Nothing touches the inside of a string, so `"https://..."`
    survives intact and a marker cannot be hidden behind one.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//")
    )


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


# ── The throttles are not the same number, and that is the point ────────────


def test_the_cloud_still_rate_limits_at_all() -> None:
    """The local lockout was built because this existed and had no counterpart.

    If `authenticate-pin` ever stops rate-limiting, the reasoning in
    `app/domain/lockout.py` is out of date and the asymmetry has flipped: the
    terminal would be the only throttled path. Worth failing over rather than
    discovering later.
    """
    source = edge_sources()["authenticate-pin"]

    attempts = re.search(r"const\s+MAX_ATTEMPTS\s*=\s*(\d+)", source)
    window = re.search(r"const\s+WINDOW_MS\s*=\s*([\d\s*]+);", source)

    assert attempts, "authenticate-pin no longer declares MAX_ATTEMPTS"
    assert window, "authenticate-pin no longer declares WINDOW_MS"
    assert int(attempts.group(1)) > 0
    assert eval(window.group(1).strip(), {"__builtins__": {}}) > 0


def test_the_local_throttle_is_at_least_as_strict_as_the_cloud() -> None:
    """The one comparison worth asserting between the two.

    They are deliberately different shapes — the cloud's is a rolling window
    in a `Map`, which is right for a function that restarts and wrong for a
    terminal an attacker can restart at will. So this is not a parity test of
    equal constants; it is the invariant that makes the pair defensible.

    The cloud allows MAX_ATTEMPTS per WINDOW_MS, sustained, for ever. The
    terminal must never be looser than that, or the offline path becomes the
    cheaper way in — which is exactly the state this slice found.

    One caveat, and it runs the opposite way to the usual assumption. The
    cloud's figure is **per isolate**: `attempts` is a Map in one instance's
    memory, and the platform cold-starts, recycles and scales these
    horizontally, so the real cloud rate is some multiple of this number that
    nobody measures. The terminal's is persisted and escalating, so it is the
    one that actually binds. This assertion therefore compares a real counter
    against a hopeful one — which is still worth asserting, because it holds
    in the direction that protects the shop, but it is a floor on the cloud's
    behaviour rather than a description of it.
    """
    source = edge_sources()["authenticate-pin"]
    attempts = int(re.search(r"const\s+MAX_ATTEMPTS\s*=\s*(\d+)", source).group(1))
    window_ms = eval(
        re.search(r"const\s+WINDOW_MS\s*=\s*([\d\s*]+);", source).group(1).strip(),
        {"__builtins__": {}},
    )

    cloud_per_hour = attempts * (3_600_000 / window_ms)
    local_per_hour = lockout.guesses_per_hour()

    assert local_per_hour <= cloud_per_hour, (
        f"the terminal allows {local_per_hour:.0f} guesses/hour and the cloud "
        f"allows {cloud_per_hour:.0f}. Offline is now the cheaper way to guess "
        "a supervisor's PIN, which is the asymmetry this lockout exists to end."
    )


def test_an_in_memory_throttle_says_that_it_is_per_instance() -> None:
    """A Map of attempts is a nominal limit, and has to admit it.

    The pattern is cheap to copy and reads like protection: ten attempts,
    a window, a 429. What it does not survive is a cold start, a recycle or a
    second isolate, none of which are visible from the source. The comment is
    the only thing standing between the next function and a limit somebody
    trusts.

    Its own positive control: `authenticate-pin` declares one, so the loop
    cannot pass by matching nothing.
    """
    throttles = {
        name: source
        for name, source in edge_sources().items()
        if re.search(r"new Map<[^>]*count[^>]*>", source)
    }
    assert "authenticate-pin" in throttles, (
        "authenticate-pin no longer keeps an in-memory attempt counter — "
        "either the throttle moved somewhere durable, which is good news and "
        "makes this check stale, or the check has stopped matching it"
    )

    for name, source in throttles.items():
        assert re.search(r"per instance", source, re.IGNORECASE), (
            f"{name}/index.ts rate-limits in a Map without saying that the "
            "limit is per isolate. It resets on every cold start and does not "
            "exist across instances; the terminal's lockout is the binding one."
        )


# ── One function mints sessions, and it is not the new one ──────────────────

#: Markers that mean "this function hands back a login".
#:
#: Any one of them is enough. `generateLink` and `verifyOtp` are the supported
#: way to sign a user in without their password; `access_token` and
#: `refresh_token` are what that produces and what a caller would store.
_SESSION_MARKERS = ("generateLink", "verifyOtp", "access_token", "refresh_token")

#: The only function entitled to return a session.
#:
#: A table rather than an `assert name == "authenticate-pin"`, because the
#: interesting failure is a *second* entry appearing, and a set says that
#: plainly. Adding one means writing down that another Edge Function now logs
#: somebody in, which should be hard to do by accident.
SESSION_MINTING = frozenset({"authenticate-pin"})


def test_only_the_login_function_mints_a_session() -> None:
    """`authorize-override` verifies a second person; it must not become them.

    The two functions share a directory, a decoy hash, a rate limiter and
    two-thirds of their body. The half that differs is the half that matters:
    `authenticate-pin` ends in `generateLink` + `verifyOtp` and returns a real
    Supabase session. Copy that file to build the next one, delete the wrong
    half, and a cashier voiding a line hands the till a supervisor's session —
    which would look like it worked, because the grant would be minted too.

    `9a59314` named the property when the offline path was written: *verify a
    second person without becoming them.* This is that property, asserted
    against the source rather than remembered.

    Its own positive control is the inclusion of `authenticate-pin`: the
    markers must still match the function that really does mint a session, so
    a typo in the pattern fails here rather than passing everything.

    Matched against code with the comments stripped. The first version of this
    test failed on `authorize-override` for a comment explaining why it must
    never mint a session — see `_code_only`.
    """
    minting = {
        name
        for name, source in edge_sources().items()
        if any(marker in _code_only(source) for marker in _SESSION_MARKERS)
    }

    assert "authenticate-pin" in minting, (
        "the login function no longer looks like it mints a session — the "
        "markers have stopped matching, and this check is now vacuous"
    )
    assert minting == SESSION_MINTING, (
        f"{sorted(minting - SESSION_MINTING)} returns a session. An override "
        "authorises an act; it does not replace the person at the till."
    )


def test_the_override_function_checks_the_permission_for_itself() -> None:
    """The online path must not be looser than the offline one.

    Offline, `authorize_override` refuses an approver who does not hold the
    key, reading the cached snapshot. If the Edge Function took the client's
    word for it — or handed back a permission list for the terminal to check
    — then having a network would lower the bar, which is the same inversion
    the lockout exists to prevent one layer down.
    """
    source = edge_sources()["authorize-override"]
    assert re.search(r'from\("role_permissions"\)', source), (
        "authorize-override does not read role_permissions. Whether the "
        "approver holds the key has to be decided server-side, from the "
        "database, not from the body of the request."
    )
    assert re.search(r'\.eq\("permission_key", permission\)', source), (
        "authorize-override reads role_permissions but does not filter by the "
        "permission it was asked about"
    )

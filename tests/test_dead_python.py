"""The Python dead-code guard, checked the way it checks everything else.

A scanner whose passing state is "found nothing" cannot be told apart from a
scanner that has stopped reading. These tests give it something to find.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

_spec = importlib.util.spec_from_file_location(
    "check_dead_python", REPO_ROOT / "scripts" / "check_dead_python.py"
)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
sys.modules["check_dead_python"] = guard
_spec.loader.exec_module(guard)


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_an_uncalled_function_is_reported_and_a_called_one_is_not(tmp_path: Path) -> None:
    scoped = _write(
        tmp_path,
        "app/services/thing.py",
        "class Client:\n"
        "    def used(self): ...\n"
        "    def unused(self): ...\n"
        "def helper(): ...\n",
    )
    caller = _write(
        tmp_path,
        "app/api/route.py",
        "from app.services.thing import Client, helper\n"
        "def route():\n"
        "    helper()\n"
        "    return Client().used()\n",
    )

    _, dead = guard.scan([scoped], [caller], root=tmp_path)

    assert dead == ["app.services.thing.Client.unused"]


def test_a_mention_in_a_docstring_is_not_a_caller(tmp_path: Path) -> None:
    """The trap the TypeScript guard's regex does not have and this one had to
    avoid: `load_refresh_token` was named in three docstrings and a comment
    while nothing called it. A text search passes; an AST walk does not."""
    scoped = _write(tmp_path, "app/security/kc.py", "def load_token(): ...\n")
    caller = _write(
        tmp_path,
        "app/services/auth.py",
        '"""Restores the session with load_token() on start."""\n'
        "# TODO: call load_token here\n"
        "def login():\n"
        "    return 'load_token'\n",
    )

    _, dead = guard.scan([scoped], [caller], root=tmp_path)

    assert dead == ["app.security.kc.load_token"]


def test_a_framework_hook_is_not_counted(tmp_path: Path) -> None:
    scoped = _write(
        tmp_path,
        "app/security/mw.py",
        "class Guard:\n    async def dispatch(self, request, call_next): ...\n",
    )

    defs, dead = guard.scan([scoped], [], root=tmp_path)

    assert defs == {} and dead == []


def test_the_debt_list_names_only_things_that_exist() -> None:
    """The rename check. Every entry in KNOWN_UNCALLED was a real definition
    when written; if one is renamed and the list is not, the guard must say
    so rather than quietly pass with one fewer thing to look at."""
    scope = sorted(p for d in guard.SCOPE for p in d.rglob("*.py"))
    defs = guard.definitions(scope)

    vanished = sorted(guard.KNOWN_UNCALLED - set(defs))
    assert vanished == [], f"KNOWN_UNCALLED names things that no longer exist: {vanished}"


def test_the_scope_still_finds_the_codebase() -> None:
    """MIN_DEFINITIONS is the guard's own tripwire; this is the tripwire's."""
    scope = sorted(p for d in guard.SCOPE for p in d.rglob("*.py"))
    assert len(guard.definitions(scope)) >= guard.MIN_DEFINITIONS


THE_TWO = [
    "app.services.supabase_auth.SupabaseAuthClient.refresh",
    "app.security.keychain.load_refresh_token",
]


@pytest.mark.parametrize("entry", THE_TWO)
def test_the_two_that_prompted_this_are_called_now(entry: str) -> None:
    """Regression for the bug the guard exists for. If either loses its
    caller again, this fails before the guard does, with the name."""
    scope = sorted(p for d in guard.SCOPE for p in d.rglob("*.py"))
    callers = [
        p
        for p in sorted(guard.APP.rglob("*.py")) + sorted(guard.SCRIPTS.glob("*.py"))
        if p.name != "check_dead_python.py"
    ]
    defs, dead = guard.scan(scope, callers)

    assert entry in defs, "the function itself has gone"
    assert entry not in dead


@pytest.mark.parametrize("entry", THE_TWO)
def test_the_two_that_prompted_this_are_not_shadowed(entry: str) -> None:
    """The guard matches by name, so a second `refresh` anywhere in scope
    would let `SupabaseAuthClient.refresh` lose its caller unnoticed. It
    happened: `TokenRefresher.refresh` was renamed `renew` for exactly this
    reason. This is what stops the name coming back."""
    scope = sorted(p for d in guard.SCOPE for p in d.rglob("*.py"))
    defs = guard.definitions(scope)

    name = entry.rsplit(".", 1)[1]
    assert name not in guard.shadowed(defs), (
        f"{name!r} is defined more than once in scope; the guard can no longer "
        f"tell whether {entry} has a caller"
    )


def test_shadowed_reports_a_shared_name(tmp_path: Path) -> None:
    a = _write(tmp_path, "app/services/a.py", "class A:\n    def go(self): ...\n")
    b = _write(
        tmp_path, "app/sync/b.py", "class B:\n    def go(self): ...\n    def only(self): ...\n"
    )

    shared = guard.shadowed(guard.definitions([a, b], root=tmp_path))

    assert shared == {"go": ["app.services.a.A.go", "app.sync.b.B.go"]}

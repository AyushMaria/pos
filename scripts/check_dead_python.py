"""Every function in the service, security, sync and repository layers must
have a caller.

## Why this exists

`scripts/check_dead_client.py` was written after phase 6 shipped
`createProduct` — a route, a model, a client function, and no button. It walks
the TypeScript client and asks whether anything calls each function.

Phase 7 then shipped the same bug on the other side of the language boundary.
`SupabaseAuthClient.refresh` and `keychain.load_refresh_token` both existed,
both looked right, and neither had a caller: the refresh token was saved at
login and never read, so every till stopped syncing an hour after sign-in.
The guard built for exactly this failure watched only TypeScript, and the bug
walked across to Python.

This is the Python half. Run against the tree as it stood before that fix
(`git checkout 9e1fbfb`), it reports both functions — which is the positive
control that matters: the scanner finds the real bug it was written for, not
only a synthetic one.

## What counts as "called"

An attribute or name reference anywhere in `app/` or `scripts/`, found by
walking the AST. Not a text match: `load_refresh_token` appeared in three
docstrings and a comment before anything called it, and a grep would have
passed. Tests are excluded on purpose — a test can call a function while no
screen, route or loop ever does, which is precisely the shape of the bug.
`scripts/` count because they are operational entry points (seeding,
reconciliation), not tests.

A reference by *name* anywhere is enough. Two classes with a `refresh` method
shadow each other, so a method can hide behind a namesake. That is a known
blind spot, accepted for the same reason the TypeScript guard accepts its
regex: a scanner that resolves types is a type checker, and this is not one.

It bit on the first mutation check. `TokenRefresher.refresh` and
`Pusher.refresh` were named after the thing they wrapped, so once
`SupabaseAuthClient.refresh` lost its caller the guard still saw the name
and stayed green — the exact regression it was written for, invisible. Both
were renamed to `renew`, `shadowed()` lists every name defined more than
once in scope so the blind spot is visible rather than remembered, and
`test_dead_python` pins the two names that started this to one definition
each.

## What it does not claim

That the caller is reachable, or that the function is correct. Only that
something names it. The acceptance pass is still the thing that knows.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
SCRIPTS = ROOT / "scripts"

#: Where an uncalled function is a feature that does not exist. Scoped here
#: rather than to all of `app/`: route handlers are called by their decorator,
#: `app.main` is called by the OS, and `app.domain` is a library whose unused
#: corners are a different, smaller problem.
SCOPE = (
    APP / "services",
    APP / "security",
    APP / "sync",
    APP / "data" / "repositories",
)

#: Method names the framework calls by contract, never by name in our code.
#: `dispatch` is Starlette's `BaseHTTPMiddleware` hook. Kept as a set so a
#: second entry has to be written down beside the first.
FRAMEWORK_HOOKS = frozenset({"dispatch"})

#: Functions that already had no caller when this check was written.
#:
#: **This list is debt, not permission.** It may shrink and nothing else.
#: Adding to it means writing down that a new function cannot be reached,
#: which should be harder than wiring it up or deleting it.
#:
#: Each entry is `module.Class.method` or `module.function`, exactly as the
#: scanner prints it. The two that prompted this check — `refresh` and
#: `load_refresh_token` — are deliberately *not* here: they were wired up in
#: `3dd3d19`, and the scanner reports them if that commit is reverted.
#:
#: Worth knowing what is in here:
#:
#: * `PaymentProvider.poll` and both implementations — the protocol slot for
#:   a PSP or a terminal that reports back. Nothing polls today because cash
#:   and attested UPI resolve the moment they begin (§13.6).
#: * `InventoryRepository.unpushed` — the terminal's half of
#:   `scripts/reconcile_stock.sql`, which names it in a comment and nothing
#:   calls. A docstring mention is not a caller; that is the whole point.
#: * `AuditRepository.lockouts` / `.refusals` — local readers for rows the
#:   audit viewer reads from the cloud instead (slice 5). Either wire a
#:   local view or delete them.
#: * `seed_local_user` is here because `scripts/seed_local.py` calls it and
#:   nothing in `app/` does; scripts count as callers, so this entry proves
#:   the scanner sees them. It should never leave this list by gaining an
#:   `app/` caller.
KNOWN_UNCALLED = frozenset(
    {
        "app.data.repositories.audit.AuditRepository.lockouts",
        "app.data.repositories.audit.AuditRepository.refusals",
        "app.data.repositories.inventory.InventoryRepository.unpushed",
        "app.data.repositories.sales.SalesRepository.by_receipt_no",
        "app.data.repositories.sales.SalesRepository.totals_balance",
        "app.data.repositories.terminal.TerminalRepository.next_client_seq",
        "app.data.repositories.unknown_scans.UnknownScanRepository.count_pending",
        "app.security.keychain.clear_refresh_token",
        "app.security.local_auth.require_session_token",
        "app.security.pins.needs_rehash",
        "app.services.payment_providers.CashProvider.poll",
        "app.services.payment_providers.PaymentProvider.poll",
        "app.services.payment_providers.ProviderRegistry.methods",
        "app.services.payment_providers.UpiProvider.poll",
        "app.services.receipt_render.totals_line",
        "app.services.supabase_auth.permissions_from_access_token",
        "app.services.supabase_auth.store_ids_from_access_token",
        "app.sync.payloads.as_json",
    }
)

#: Fewer definitions than this and the scope has stopped matching. The four
#: packages held 280 when this was written; a number an order of magnitude
#: lower is a moved directory, not a smaller codebase.
MIN_DEFINITIONS = 100


def _module_name(path: Path, root: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


def definitions(paths: list[Path], root: Path = ROOT) -> dict[str, tuple[Path, int]]:
    """Every function and method in scope, by qualified name.

    Dunder methods are the language's, not ours. Nested functions are
    skipped: a closure is called by the function that made it, which the
    scanner would otherwise count as a caller of nothing.
    """
    found: dict[str, tuple[Path, int]] = {}
    for path in paths:
        module = _module_name(path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("__"):
                    found[f"{module}.{node.name}"] = (path, node.lineno)
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name.startswith("__") or item.name in FRAMEWORK_HOOKS:
                            continue
                        found[f"{module}.{node.name}.{item.name}"] = (path, item.lineno)
    return found


def referenced_names(paths: list[Path]) -> set[str]:
    """Every attribute and bare name that appears in the callers' AST."""
    names: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
    return names


def shadowed(defs: dict[str, tuple[Path, int]]) -> dict[str, list[str]]:
    """Bare names defined more than once in scope.

    The guard matches by name, so each of these can lose its caller without
    being noticed as long as a namesake keeps one. Reported, not failed:
    `record`, `begin` and `cancel` are legitimately shared across
    repositories and providers. What must not happen is a *new* collision on
    a name the guard is specifically watching, which the tests pin.
    """
    by_name: dict[str, list[str]] = {}
    for qualified in defs:
        by_name.setdefault(qualified.rsplit(".", 1)[1], []).append(qualified)
    return {name: sorted(qs) for name, qs in by_name.items() if len(qs) > 1}


def scan(
    scope: list[Path], callers: list[Path], root: Path = ROOT
) -> tuple[dict[str, tuple[Path, int]], list[str]]:
    """The definitions in scope, and those nothing in `callers` names.

    `root` is what qualified names are relative to; tests pass a temp tree.
    """
    defs = definitions(scope, root)
    used = referenced_names(callers)
    dead = sorted(q for q in defs if q.rsplit(".", 1)[1] not in used)
    return defs, dead


def main() -> int:
    for directory in SCOPE:
        if not directory.is_dir():
            print(f"no {directory.relative_to(ROOT)} — refusing to pass a check that read nothing")
            return 2

    scope = sorted(p for d in SCOPE for p in d.rglob("*.py"))
    callers = sorted(APP.rglob("*.py")) + sorted(SCRIPTS.glob("*.py"))
    # This script names every entry in KNOWN_UNCALLED, which would make each
    # of them look called.
    callers = [p for p in callers if p.resolve() != Path(__file__).resolve()]

    defs, dead = scan(scope, callers)

    if len(defs) < MIN_DEFINITIONS:
        print(
            f"found only {len(defs)} definitions in scope (expected at least "
            f"{MIN_DEFINITIONS}) — SCOPE has stopped matching where the code lives"
        )
        return 2

    # The debt list as positive control: every entry was a real definition
    # when written down. One the scanner cannot find is either deleted (shrink
    # the list) or a parser that has stopped reading — and a rename is the
    # cheapest way to find out which.
    vanished = sorted(KNOWN_UNCALLED - set(defs))
    if vanished:
        print("These are in KNOWN_UNCALLED but no longer exist:\n")
        for entry in vanished:
            print(f"  {entry}")
        print(
            "\nIf the function was deleted or renamed, update KNOWN_UNCALLED."
            "\nIf it is still there, the definition walker has stopped matching"
            "\nand this check is no longer reading the code."
        )
        return 1

    new = sorted(set(dead) - KNOWN_UNCALLED)
    fixed = sorted(KNOWN_UNCALLED - set(dead))

    if new:
        print(f"{len(new)} function(s) nothing in app/ or scripts/ calls:\n")
        for entry in new:
            path, line = defs[entry]
            print(f"  {entry}  ({path.relative_to(ROOT)}:{line})")
        print(
            "\nEither wire it up or delete it. A function with no caller is a"
            "\nfeature that does not exist yet — the refresh token was one."
        )
        return 1

    if fixed:
        print("These now have callers — remove them from KNOWN_UNCALLED:\n")
        for entry in fixed:
            print(f"  {entry}")
        return 1

    print(
        f"every function in scope is called "
        f"({len(defs)} checked, {len(KNOWN_UNCALLED)} known unreachable, "
        f"{len(shadowed(defs))} names shared between definitions)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

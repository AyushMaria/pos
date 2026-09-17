"""Every function in the TypeScript API client must be called by the UI.

## Why this exists

Slice 6 shipped `createProduct` in `core/api/admin.ts`, a matching route, a
matching Pydantic model, and no way to reach any of it: nothing in the UI ever
called the function. The screen looked finished, the contract check passed —
`ProductCreateRequest` really did match its model — and the acceptance step
"create a product" was impossible.

Nothing in the toolchain could see it. `noUnusedLocals` does not apply to a
property on an exported object, TypeScript has no unused-export check, and a
type that is correct but unused looks exactly like a type that is correct and
used.

## What counts as "called"

A reference from a **non-test** file. A mock in a test proves nothing: a test
can happily mock `createProduct` while no screen calls it, which is precisely
the shape of the original bug. Tests are excluded on purpose.

## What it does not claim

That the call is reachable, or correct, or that a human can find the button.
Only that one exists. The acceptance pass is still the thing that knows.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "ui-src" / "src"
API = SRC / "core" / "api"

#: Client functions that already had no caller when this check was written.
#:
#: **This list is debt, not permission.** It exists so that turning the check
#: on did not require fixing seven unrelated things in one commit, and it is
#: allowed to shrink and nothing else. Adding to it means writing down that a
#: new endpoint cannot be reached, which is worth having to type out.
#:
#: `admin.createProduct` is deliberately *not* here: it is what prompted the
#: check, and it was wired up in the same commit.
#:
#: Two of these are worth a look on their own — `register.setQuantity` and
#: `register.abandon` mean the till can void a line and re-scan but cannot
#: change a quantity or clear a basket.
KNOWN_UNCALLED = frozenset(
    {
        "admin.product",
        "api.health",
        "api.logout",
        "register.readCart",
        "register.setQuantity",
        "register.abandon",
        "sync.failures",
    }
)

#: `export const admin = { ... }` — the client objects the UI calls through.
CLIENT = re.compile(r"^export const (\w+)\s*=\s*\{", re.MULTILINE)

#: A top-level key in one of those objects. Two spaces of indent is the object
#: body; anything deeper belongs to a nested literal and is not an entry point.
MEMBER = re.compile(r"^  (\w+):", re.MULTILINE)


def members(source: str) -> dict[str, list[str]]:
    """Map each exported client object to its top-level function names."""
    found: dict[str, list[str]] = {}
    for match in CLIENT.finditer(source):
        name = match.group(1)
        body = source[match.end() :]
        end = body.find("\n};")
        found[name] = MEMBER.findall(body if end == -1 else body[:end])
    return found


def callers() -> str:
    """Every non-test source file, concatenated."""
    parts = []
    for path in sorted(SRC.rglob("*.ts")) + sorted(SRC.rglob("*.tsx")):
        if ".test." in path.name or path.parent == API:
            continue
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def main() -> int:
    # Three ways this check can report success having looked at nothing, and
    # it exists because a whole feature once shipped uncalled — the same shape
    # of silence it is meant to detect. `ui-src/src` missing used to print
    # "nothing to check" and exit 0, which is the friendliest possible way to
    # stop working.
    if not SRC.is_dir():
        print(f"no {SRC.relative_to(ROOT)} — refusing to pass a check that read nothing")
        return 2
    if not API.is_dir():
        print(f"no {API.relative_to(ROOT)} — the client moved and this check did not")
        return 2

    client_files = sorted(API.glob("*.ts"))
    if not client_files:
        print(f"no .ts files in {API.relative_to(ROOT)} — nothing was parsed")
        return 2

    used = callers()
    if not used.strip():
        print("no non-test sources found — every function would look uncalled")
        return 2

    dead: list[str] = []
    seen: set[str] = set()

    for path in client_files:
        for obj, names in members(path.read_text(encoding="utf-8")).items():
            for name in names:
                seen.add(f"{obj}.{name}")
                if not re.search(rf"\b{obj}\.{name}\b", used):
                    dead.append(f"{obj}.{name}")

    checked = len(seen)
    if not checked:
        print(
            f"parsed {len(client_files)} client file(s) and found no functions — "
            "CLIENT or MEMBER has stopped matching how the client is written",
        )
        return 2

    # The debt list as positive control, which is the trick
    # `tests/test_route_coverage.py` gets for free: its two halves check each
    # other, so neither can pass vacuously alone. Every name in
    # KNOWN_UNCALLED was a real member when it was written down, so if the
    # parser stops finding one, either the function is gone — in which case
    # the list must shrink — or the parser is broken.
    vanished = sorted(KNOWN_UNCALLED - seen)
    if vanished:
        print("These are in KNOWN_UNCALLED but no longer exist in the client:\n")
        for entry in vanished:
            print(f"  {entry}")
        print(
            "\nIf the function was deleted, remove it from KNOWN_UNCALLED."
            "\nIf it is still there, the member parser has stopped matching and"
            "\nthis check is no longer reading the client."
        )
        return 1

    new = sorted(set(dead) - KNOWN_UNCALLED)
    fixed = sorted(KNOWN_UNCALLED - set(dead))

    if new:
        print(f"{len(new)} API client function(s) nothing in the UI calls:\n")
        for entry in new:
            print(f"  {entry}")
        print(
            "\nEither wire it to a screen or delete it. An endpoint with a client"
            "\nfunction and no caller is a feature that does not exist yet."
        )
        return 1

    if fixed:
        print("These now have callers — remove them from KNOWN_UNCALLED:\n")
        for entry in fixed:
            print(f"  {entry}")
        return 1

    print(
        f"every API client function is called "
        f"({checked} checked, {len(KNOWN_UNCALLED)} known unreachable)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

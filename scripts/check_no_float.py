"""Reject binary floating point anywhere in `app/domain/`.

Phase 2 exit criterion, and architecture §17's first risk: "float money
reaching the database". The defence has three layers — INTEGER columns in both
schemas, a `Money` value object in the domain, and this check, which stops the
mistake at the point it is written rather than at the point it is noticed.

Why a bespoke check: ruff has no rule for "this package may not use a builtin
type". `flake8-tidy-imports` bans imported names, and `float` is never
imported. So the AST is walked directly.

What is rejected in `app/domain/`:

  * float literals            0.1, 1e-3
  * the `float` builtin       float(x), `x: float`, isinstance(x, float)
  * `complex`                 same family, same problem
  * `round()`                 returns a float for float input and silently
                              uses banker's rounding; money quantisation must
                              be explicit ROUND_HALF_UP at the tax boundary
  * `statistics.mean` etc.    float-returning stdlib maths

Not rejected: `/`. Dividing a `Decimal` yields a `Decimal`, and pricing
genuinely needs it (qty in thousandths, tax bases). Integer-vs-Decimal
correctness there is the property tests' job, not this script's.

    python scripts/check_no_float.py
    python scripts/check_no_float.py --path app/domain app/services
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BANNED_NAMES = {
    "float": "use Money (integer minor units) or Decimal",
    "complex": "not a money type",
    "round": "use Decimal.quantize with ROUND_HALF_UP at the tax boundary",
}

BANNED_ATTRS = {
    "statistics.mean": "returns a float; sum integers and divide as Decimal",
    "statistics.fmean": "returns a float",
    "math.floor": "use Decimal.quantize or // on integers",
    "math.ceil": "use Decimal.quantize or // on integers",
}

# One escape hatch, used with a reason. `app/domain/ids.py` has none; if a
# future module legitimately needs one, it has to say why on the line.
ESCAPE = "allow-float:"


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    col: int
    code: str
    message: str

    def render(self, root: Path) -> str:
        rel = self.path.relative_to(root)
        return f"{rel}:{self.line}:{self.col}: {self.code} {self.message}"


class FloatHunter(ast.NodeVisitor):
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.lines = source.splitlines()
        self.violations: list[Violation] = []

    def _excused(self, node: ast.AST) -> bool:
        line_no = getattr(node, "lineno", 0)
        if not 1 <= line_no <= len(self.lines):
            return False
        return ESCAPE in self.lines[line_no - 1]

    def _flag(self, node: ast.AST, code: str, message: str) -> None:
        if self._excused(node):
            return
        self.violations.append(
            Violation(
                path=self.path,
                line=getattr(node, "lineno", 0),
                col=getattr(node, "col_offset", 0) + 1,
                code=code,
                message=message,
            )
        )

    def visit_Constant(self, node: ast.Constant) -> None:
        # `isinstance(True, int)` is True, so check the exact type: a bool
        # constant must not be mistaken for a number here.
        if type(node.value) is float:
            self._flag(
                node,
                "MONEY001",
                f"float literal {node.value!r} — use integer minor units or Decimal",
            )
        elif type(node.value) is complex:
            self._flag(node, "MONEY001", "complex literal")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in BANNED_NAMES:
            self._flag(
                node, "MONEY002", f"`{node.id}` is banned in domain/ — {BANNED_NAMES[node.id]}"
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        dotted = _dotted_name(node)
        if dotted in BANNED_ATTRS:
            self._flag(node, "MONEY003", f"`{dotted}` — {BANNED_ATTRS[dotted]}")
        self.generic_visit(node)


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def check_file(path: Path) -> list[Violation]:
    source = path.read_text(encoding="utf-8")
    hunter = FloatHunter(path, source)
    hunter.visit(ast.parse(source, filename=str(path)))
    return hunter.violations


def check_paths(paths: list[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for target in paths:
        for path in sorted(target.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            violations.extend(check_file(path))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        nargs="+",
        default=["app/domain"],
        help="packages to check (default: app/domain)",
    )
    args = parser.parse_args()

    targets = [REPO_ROOT / p for p in args.path]
    missing = [t for t in targets if not t.exists()]
    if missing:
        print(f"no such path: {missing[0]}", file=sys.stderr)
        return 2

    violations = check_paths(targets)
    for violation in violations:
        print(violation.render(REPO_ROOT), file=sys.stderr)

    if violations:
        print(
            f"\n{len(violations)} floating-point violation(s). "
            "Money is integer minor units; quantities are thousandths "
            "(architecture §6).",
            file=sys.stderr,
        )
        return 1

    checked = sum(1 for t in targets for _ in t.rglob("*.py"))
    print(f"no floating point in {', '.join(args.path)} ({checked} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The float ban — phase 2 exit criterion, architecture §17.

`float` money reaching the database is the classic failure of a Python POS,
and it is invisible until a customer disputes a paise. The checker is asserted
here rather than only wired into CI, so that a change which quietly makes it
stop catching things fails the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_no_float import check_file, check_paths  # noqa: E402


def write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return path


# ── The domain really is clean ──────────────────────────────────────────────


def test_domain_has_no_floating_point() -> None:
    violations = check_paths([REPO_ROOT / "app" / "domain"])
    assert violations == [], "\n".join(v.render(REPO_ROOT) for v in violations)


# ── ...and the checker would notice if it weren't ───────────────────────────


@pytest.mark.parametrize(
    "source,code",
    [
        ("price = 19.99\n", "MONEY001"),
        ("rate = 1e-3\n", "MONEY001"),
        ("x = complex(1, 2)\n", "MONEY002"),
        ("def f(x: float) -> None: ...\n", "MONEY002"),
        ("total = float(subtotal)\n", "MONEY002"),
        ("if isinstance(v, float): ...\n", "MONEY002"),
        ("paise = round(total)\n", "MONEY002"),
        ("import statistics\nm = statistics.mean(xs)\n", "MONEY003"),
        ("import math\nn = math.floor(x)\n", "MONEY003"),
    ],
)
def test_violations_are_caught(tmp_path: Path, source: str, code: str) -> None:
    violations = check_file(write(tmp_path, source))
    assert violations, f"not caught: {source!r}"
    assert violations[0].code == code


@pytest.mark.parametrize(
    "source",
    [
        "qty = 1250\n",
        "from decimal import Decimal\nrate = Decimal('0.18')\n",
        "paise = total_minor // 100\n",
        "flag = True\n",
        "share = Decimal(base) / Decimal(3)\n",
        "name = 'float'\n",  # a string, not the builtin
        "class Money:\n    def as_float(self) -> None: ...\n",  # attribute name
    ],
)
def test_legitimate_money_code_passes(tmp_path: Path, source: str) -> None:
    assert check_file(write(tmp_path, source)) == []


def test_the_escape_hatch_needs_a_reason_on_the_line(tmp_path: Path) -> None:
    """Deliberate use is possible, but it has to be said out loud."""
    excused = "elapsed = 0.5  # allow-float: a timing constant, never money\n"
    assert check_file(write(tmp_path, excused)) == []

    assert check_file(write(tmp_path, "elapsed = 0.5\n")) != []


def test_a_violation_reports_a_useful_location(tmp_path: Path) -> None:
    path = write(tmp_path, "a = 1\nb = 2\nc = 3.5\n")
    violation = check_file(path)[0]

    assert violation.line == 3
    assert "3.5" in violation.message

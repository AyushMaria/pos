"""Generated files must not drift from their source.

The permission matrix exists in Python, in Postgres and (via OpenAPI) in
TypeScript. Three copies of one table is three chances to disagree, and a
disagreement here means the UI hides a control the database allows, or the
reverse. So two of the three are generated, and this test fails the build if
the committed output stops matching.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_permission_seed_matches_the_domain_matrix() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_permission_seed.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_every_permission_reaches_postgres() -> None:
    """A belt-and-braces read of the committed SQL, independent of the script."""
    from app.domain import permissions as perms

    sql = (REPO_ROOT / "supabase" / "migrations" / "0002_permissions_seed.sql").read_text(
        encoding="utf-8"
    )
    for key in perms.ALL_PERMISSIONS:
        assert f"'{key}'" in sql, f"{key} is missing from the Postgres seed"
    for role in perms.ROLES:
        assert f"'{role}'" in sql, f"{role} is missing from the Postgres seed"


def test_local_and_cloud_schemas_agree_on_money_columns() -> None:
    """The two schemas mirror each other; a REAL on either side is a defect."""
    cloud = (REPO_ROOT / "supabase" / "migrations" / "0001_core.sql").read_text(
        encoding="utf-8"
    )
    local = (REPO_ROOT / "app" / "data" / "migrations" / "001_init.sql").read_text(
        encoding="utf-8"
    )

    def strip_comments(sql: str) -> str:
        # The comments in these files talk *about* floats at length; only the
        # DDL itself is being inspected.
        return "\n".join(line.split("--")[0] for line in sql.lower().splitlines())

    for name, sql in (("cloud", cloud), ("local", local)):
        ddl = strip_comments(sql)
        for banned in (" real", " float", " double", " numeric("):
            assert banned not in ddl, (
                f"{name} schema declares a{banned.upper()} column; money and "
                "quantities are INTEGER minor units (architecture §6)"
            )


def test_typescript_contract_matches_the_pydantic_models() -> None:
    """Execution plan §4: contract-first, or the two tracks drift.

    A stale contract.ts is not a compile error in Python and not a compile
    error in TypeScript — it is a runtime `undefined` in front of a customer.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_ts_types.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr or result.stdout

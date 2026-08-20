"""Run the RLS suite against a throwaway Postgres, no Docker required.

RLS is the only real security boundary in this system, so its tests must be
easy enough to run that nobody skips them. Docker Desktop is not installed on
every Windows machine in a small shop's dev setup; `pgserver` bundles a real
Postgres and starts it in a temp directory instead.

    pip install -e ".[pg]"
    python scripts/run_rls_tests.py
    python scripts/run_rls_tests.py -k margin -v

CI uses a Postgres service container and sets POS_TEST_PG_DSN directly; this
script exists for the machine in front of you.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str]) -> int:
    try:
        import pgserver
    except ImportError:
        print(
            "pgserver is not installed.\n"
            '  pip install -e ".[pg]"\n'
            "Or point POS_TEST_PG_DSN at any Postgres and run pytest directly.",
            file=sys.stderr,
        )
        return 1

    import pytest

    data_dir = Path(tempfile.gettempdir()) / "pos_rls_pgdata"
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"starting postgres in {data_dir} ...")
    server = pgserver.get_server(str(data_dir))
    dsn = server.get_uri()
    print(f"ready: {dsn}\n")

    os.environ["POS_TEST_PG_DSN"] = dsn
    return int(pytest.main([str(REPO_ROOT / "tests" / "test_rls.py"), *argv]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

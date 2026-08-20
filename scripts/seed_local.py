"""Seed this terminal's local database with staff and a demo catalog.

Phase 1 has to demonstrate a login before phase 5's sync engine exists, so the
identity cache is written directly. This is also how a terminal is recovered
when a store's connectivity fails on go-live day.

    python scripts/seed_local.py
    python scripts/seed_local.py --reset      # drop the database first

Real deployments get their users from the cloud on first online login; this
script exists for development, tests and emergencies.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings  # noqa: E402
from app.data.db import Database  # noqa: E402
from app.data.migrations import migrate  # noqa: E402
from app.data.repositories.users import CachedUserRepository  # noqa: E402
from app.domain import permissions as perms  # noqa: E402
from app.services.auth_service import AuthService, SessionStore  # noqa: E402

# Matches the store id in supabase/seed/seed.sql, so a terminal seeded locally
# and one seeded from the cloud agree on which store they belong to.
DEMO_STORE_ID = "018f0000-0000-7000-8000-000000000100"

# Fixed ids so that re-seeding updates rather than duplicates.
STAFF = [
    {
        "user_id": "018f0000-0000-7000-8000-000000000001",
        "employee_code": "C001",
        "full_name": "Anita Rao",
        "pin": "4913",
        "roles": frozenset({perms.CASHIER}),
    },
    {
        "user_id": "018f0000-0000-7000-8000-000000000002",
        "employee_code": "S001",
        "full_name": "Vikram Shah",
        "pin": "7261",
        "roles": frozenset({perms.SUPERVISOR}),
    },
    {
        "user_id": "018f0000-0000-7000-8000-000000000003",
        "employee_code": "M001",
        "full_name": "Priya Nair",
        "pin": "5820",
        "roles": frozenset({perms.MANAGER}),
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_directories()

    if args.reset and settings.db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(settings.db_path) + suffix)
            candidate.unlink(missing_ok=True)
        print(f"removed {settings.db_path}")

    db = Database(settings.db_path)
    version = migrate(db)
    print(f"database at {settings.db_path} (schema v{version})")

    auth = AuthService(
        users=CachedUserRepository(db),
        sessions=SessionStore(),
        cloud=None,
        store_code=settings.store_code,
        terminal_code=settings.terminal_code,
        settings=settings,
    )

    print(f"\nhashing PINs with argon2id t={settings.argon2_time_cost} "
          f"m={settings.argon2_memory_cost_kib}KiB p={settings.argon2_parallelism}")

    for member in STAFF:
        roles = member["roles"]
        assert isinstance(roles, frozenset)
        auth.seed_local_user(
            user_id=str(member["user_id"]),
            employee_code=str(member["employee_code"]),
            full_name=str(member["full_name"]),
            store_id=DEMO_STORE_ID,
            pin=str(member["pin"]),
            roles=roles,
            permissions=perms.permissions_for(roles),
        )
        granted = len(perms.permissions_for(roles))
        print(
            f"  {member['employee_code']:<6} {member['full_name']:<14} "
            f"{', '.join(sorted(roles)):<12} PIN {member['pin']}  "
            f"({granted} permissions)"
        )

    db.close()
    print("\nStart the till with:  python -m app.main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

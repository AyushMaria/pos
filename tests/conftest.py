"""Shared fixtures.

Two worlds here. Most tests run against a temp SQLite file with no window and
no network — that is the payoff of keeping the domain pure and the service
layer injectable. The RLS tests need a real Postgres, because RLS is the one
thing that cannot be faked; they skip unless ``POS_TEST_PG_DSN`` points at one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings

from app.api.server import build_app
from app.config import Settings
from app.data.db import Database
from app.data.migrations import migrate
from app.data.repositories.users import CachedUserRepository
from app.domain import permissions as perms
from app.services.auth_service import AuthService, SessionStore

REPO_ROOT = Path(__file__).resolve().parent.parent

# The domain suite has to stay under two seconds (execution plan, phase 2 exit
# criteria) because a slow suite stops being run. "fast" keeps that promise at
# the desk; CI sets HYPOTHESIS_PROFILE=ci to search far harder on the same
# properties, which is where a rare counterexample is actually worth finding.
settings.register_profile(
    "fast", max_examples=40, deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("ci", max_examples=400, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "fast"))

TEST_STORE_ID = "018f0000-0000-7000-8000-000000000100"
TEST_TOKEN = "test-session-token"

# Cheap argon2 parameters. The production values are tuned to ~100 ms, which
# would add a minute to a suite that hashes on every login test. The cost
# parameters themselves are asserted separately in test_pins.py.
FAST_ARGON2 = {
    "argon2_time_cost": 1,
    "argon2_memory_cost_kib": 8192,
    "argon2_parallelism": 1,
}


@pytest.fixture
def in_memory_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Keep the suite out of the real OS credential store.

    Without this, running the tests leaves refresh tokens in Windows
    Credential Manager and the results depend on what a previous run left
    behind.

    Opt-in rather than autouse: `tests/domain/` must run with no fixtures at
    all, and an autouse fixture at this level would apply there too.
    """
    vault: dict[str, str] = {}

    monkeypatch.setattr(
        "app.security.keychain.keyring.set_password",
        lambda service, account, password: vault.__setitem__(
            f"{service}:{account}", password
        ),
    )
    monkeypatch.setattr(
        "app.security.keychain.keyring.get_password",
        lambda service, account: vault.get(f"{service}:{account}"),
    )
    monkeypatch.setattr(
        "app.security.keychain.keyring.delete_password",
        lambda service, account: vault.pop(f"{service}:{account}", None),
    )
    return vault


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        store_code="ST01",
        terminal_code="T1",
        supabase_url="",
        supabase_anon_key="",
        **FAST_ARGON2,
    )


@pytest.fixture
def db(settings: Settings) -> Iterator[Database]:
    database = Database(settings.db_path)
    migrate(database)
    yield database
    database.close()


@pytest.fixture
def users(db: Database) -> CachedUserRepository:
    return CachedUserRepository(db)


@pytest.fixture
def sessions() -> SessionStore:
    return SessionStore()


@pytest.fixture
def auth_service(
    users: CachedUserRepository, sessions: SessionStore, settings: Settings
) -> AuthService:
    return AuthService(
        users=users,
        sessions=sessions,
        cloud=None,
        store_code=settings.store_code,
        terminal_code=settings.terminal_code,
        settings=settings,
    )


@pytest.fixture
def seeded_cashier(auth_service: AuthService) -> dict[str, str]:
    auth_service.seed_local_user(
        user_id="018f0000-0000-7000-8000-000000000001",
        employee_code="C001",
        full_name="Anita Rao",
        store_id=TEST_STORE_ID,
        pin="4913",
        roles=frozenset({perms.CASHIER}),
        permissions=perms.permissions_for(frozenset({perms.CASHIER})),
    )
    return {"employee_code": "C001", "pin": "4913"}


@pytest.fixture
def seeded_manager(auth_service: AuthService) -> dict[str, str]:
    auth_service.seed_local_user(
        user_id="018f0000-0000-7000-8000-000000000003",
        employee_code="M001",
        full_name="Priya Nair",
        store_id=TEST_STORE_ID,
        pin="5820",
        roles=frozenset({perms.MANAGER}),
        permissions=perms.permissions_for(frozenset({perms.MANAGER})),
    )
    return {"employee_code": "M001", "pin": "5820"}


@pytest.fixture
def client(settings: Settings, db: Database) -> Iterator[TestClient]:
    """A client that already carries the session token and a loopback Host."""
    app = build_app(token=TEST_TOKEN, settings=settings, db=db, run_migrations=False)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        yield test_client


@pytest.fixture
def raw_client(settings: Settings, db: Database) -> Iterator[TestClient]:
    """No credentials attached — for testing the guards themselves."""
    app = build_app(token=TEST_TOKEN, settings=settings, db=db, run_migrations=False)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


# ── Postgres / RLS ──────────────────────────────────────────────────────────


def pg_dsn() -> str | None:
    return os.environ.get("POS_TEST_PG_DSN")


requires_postgres = pytest.mark.skipif(
    pg_dsn() is None,
    reason="set POS_TEST_PG_DSN to a Postgres database to run the RLS suite",
)

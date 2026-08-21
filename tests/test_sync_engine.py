"""The sync loop and what the till says about it — architecture §9, §4.

The engine's job is to be invisible when it works and legible when it does
not. So these test two things: that a cycle does push-then-pull-then-report,
and that what a cashier sees is a number they can act on — how many sales are
still sitting on this machine.

The loop itself is deliberately hard to break: every failure inside it is
caught, because a till that stops selling when the internet stops is not
offline-first, it is just online with extra steps.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.server import build_app
from app.config import Settings
from app.data.db import Database
from app.data.repositories.outbox import OutboxRepository
from app.sync.backoff import Backoff
from app.sync.engine import SyncEngine
from app.sync.payloads import PayloadBuilder
from app.sync.puller import Puller
from app.sync.pusher import Pusher
from tests.conftest import FAST_ARGON2, TEST_TOKEN, add_barcode, open_cart
from tests.test_sync_push import TERMINAL_ID, FakeCloud, sell


@pytest.fixture
def outbox(db: Database) -> OutboxRepository:
    return OutboxRepository(db)


def an_engine(
    db: Database, outbox: OutboxRepository, cloud: FakeCloud, token: str | None = "jwt"
) -> SyncEngine:
    return SyncEngine(
        outbox=outbox,
        pusher=Pusher(
            outbox,
            PayloadBuilder(db, terminal_id=TERMINAL_ID),
            base_url="https://example.supabase.co",
            anon_key="anon",
            token_provider=lambda: token,
            backoff=Backoff(first_seconds=0.0, cap_seconds=0.0),
            client=cloud.client,
        ),
        puller=Puller(
            db,
            base_url="https://example.supabase.co",
            anon_key="anon",
            token_provider=lambda: token,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=[])
                )
            ),
        ),
    )


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud()


# ── One cycle ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_cycle_pushes_and_reports(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    sell(till)
    engine = an_engine(db, outbox, cloud)

    await engine.cycle()

    assert engine.status.online is True
    assert engine.status.backlog == 0
    assert engine.status.last_push_at is not None
    assert engine.status.last_pull_at is not None


@pytest.mark.asyncio
async def test_a_failed_push_does_not_pull(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """No point asking for prices down a line that just refused a sale."""
    sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.ConnectError("down")
    engine = an_engine(db, outbox, cloud)

    await engine.cycle()

    assert engine.status.online is False
    assert engine.status.last_pull_at is None
    assert engine.status.backlog == 1


@pytest.mark.asyncio
async def test_the_loop_survives_anything(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """A sync failure must never become a selling failure (§1.1)."""
    sell(till)
    engine = an_engine(db, outbox, cloud)

    async def explode() -> None:
        raise RuntimeError("the cloud caught fire")

    engine.pusher.drain = explode  # type: ignore[method-assign]

    await engine.cycle()

    assert engine.status.online is False
    assert "caught fire" in (engine.status.last_error or "")
    # And the sale is still here, still queued, still sellable-from.
    assert outbox.backlog() == 1


@pytest.mark.asyncio
async def test_an_outdated_terminal_is_flagged_for_a_human(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """The one sync state waiting cannot fix (§17)."""
    sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(
        400, json={"message": "outdated_terminal: schema_version 2 is not supported"}
    )
    engine = an_engine(db, outbox, cloud)

    await engine.cycle()

    assert engine.status.failures == 1


@pytest.mark.asyncio
async def test_the_status_counts_what_is_still_here(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    for _ in range(3):
        sell(till)
    engine = an_engine(db, outbox, cloud, token=None)  # signed out: nothing goes

    await engine.cycle()

    assert engine.snapshot().backlog == 3
    assert engine.snapshot().oldest_pending_at is not None


@pytest.mark.asyncio
async def test_the_status_is_published_for_the_screen(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """The register hears about a backlog rather than polling for one."""
    published: list[tuple[str, dict[str, Any]]] = []

    async def record(event: str, payload: dict[str, Any]) -> None:
        published.append((event, payload))

    engine = an_engine(db, outbox, cloud)
    # Async, because in the app this is the WebSocket hub's broadcast.
    engine.publish = record

    await engine.cycle()

    assert published[0][0] == "sync.status"
    assert "backlog" in published[0][1]


# ── The endpoint ────────────────────────────────────────────────────────────


@pytest.fixture
def cloud_settings(tmp_path: Any) -> Settings:
    """A terminal that believes it has a cloud, so an engine gets built."""
    return Settings(
        data_dir=tmp_path,
        store_code="ST01",
        terminal_code="T1",
        terminal_id=TERMINAL_ID,
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon-key",
        **FAST_ARGON2,
    )


@pytest.fixture
def cloud_till(
    cloud_settings: Settings, db: Database, seeded_cashier: dict
) -> Iterator[TestClient]:
    app = build_app(
        token=TEST_TOKEN, settings=cloud_settings, db=db, run_migrations=False
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        client.post("/auth/login", json=seeded_cashier)
        yield client


def test_status_reports_the_backlog(cloud_till: TestClient, catalog: dict) -> None:
    cart_id = open_cart(cloud_till)
    add_barcode(cloud_till, cart_id, "8901030608278")
    cloud_till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    cloud_till.post(f"/register/carts/{cart_id}/post")

    status = cloud_till.get("/sync/status").json()

    assert status["backlog"] == 1
    assert status["failures"] == 0
    assert status["needs_update"] is False


def test_a_terminal_with_no_cloud_says_so_plainly(till: TestClient) -> None:
    """Rather than reporting a healthy sync that does not exist."""
    response = till.get("/sync/status")

    assert response.status_code == 503
    assert "no Supabase project" in response.json()["detail"]


def test_a_cashier_cannot_read_the_failure_list(cloud_till: TestClient) -> None:
    """It is a manager's problem, and it contains whole sale payloads."""
    assert cloud_till.get("/sync/failures").status_code == 403


def test_a_manager_can_read_the_failure_list(
    cloud_till: TestClient, seeded_manager: dict
) -> None:
    cloud_till.post("/auth/login", json=seeded_manager)

    response = cloud_till.get("/sync/failures")

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_status_needs_a_signed_in_session(cloud_settings: Settings, db: Database) -> None:
    app = build_app(
        token=TEST_TOKEN, settings=cloud_settings, db=db, run_migrations=False
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        assert client.get("/sync/status").status_code == 401

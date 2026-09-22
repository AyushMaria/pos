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

import json
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
from tests.conftest import (
    FAST_ARGON2,
    TEST_MAC_KEY,
    TEST_TOKEN,
    add_barcode,
    open_cart,
    settle_opening,
)
from tests.test_sync_push import TERMINAL_ID, FakeCloud, sell


@pytest.fixture
def till(till: TestClient, db: Database) -> TestClient:
    """See `settle_opening`: the queue starts empty of the fixture shift."""
    settle_opening(db)
    return till


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


# ── Retrying what was set aside ─────────────────────────────────────────────


async def _quarantine_one(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> SyncEngine:
    """Sell, then have the cloud refuse it in a way that is permanent."""
    sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(
        400, json={"message": 'column "entity_id" is of type uuid'}
    )
    engine = an_engine(db, outbox, cloud)
    await engine.cycle()
    assert outbox.unacknowledged_failures() == 1
    assert outbox.backlog() == 0
    return engine


@pytest.mark.asyncio
async def test_a_quarantined_sale_can_be_sent_again_once_the_cause_is_fixed(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """The gap this closes.

    `quarantine()` marks the outbox row synced so the drain loop steps over
    it, and before this nothing ever cleared that. A sale refused by a bug
    that was later fixed — a missing cast in `sync_push`, an RLS policy since
    corrected — stayed on the terminal for good, with the failures list able
    to say why and nobody able to do anything about it.

    Found the hard way: a real sale needed hand-written SQL against the
    terminal's SQLite to get it moving again.
    """
    engine = await _quarantine_one(till, db, outbox, cloud)

    requeued = outbox.retry_failures()

    assert requeued == 1
    assert outbox.backlog() == 1
    assert outbox.unacknowledged_failures() == 0

    # The cloud is behaving now, so the next cycle gets it through.
    await engine.cycle()

    assert outbox.backlog() == 0
    assert len(cloud.sales) == 1


@pytest.mark.asyncio
async def test_retrying_a_still_broken_sale_quarantines_it_again(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Retry is not forgiveness. If the cause was not fixed, the sale comes
    straight back to the failures list rather than cycling quietly forever."""
    engine = await _quarantine_one(till, db, outbox, cloud)

    outbox.retry_failures()
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(400, json={"message": "still broken"})
    await engine.cycle()

    assert outbox.unacknowledged_failures() == 1
    assert outbox.backlog() == 0


@pytest.mark.asyncio
async def test_retrying_one_failure_leaves_the_others_alone(
    till: TestClient, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    engine = await _quarantine_one(till, db, outbox, cloud)
    sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(400, json={"message": "nope"})
    await engine.cycle()
    assert outbox.unacknowledged_failures() == 2

    first = outbox.failures()[-1]["id"]
    requeued = outbox.retry_failures([int(first)])

    assert requeued == 1
    assert outbox.unacknowledged_failures() == 1


def test_retrying_nothing_is_not_retrying_everything(
    db: Database, outbox: OutboxRepository
) -> None:
    """An empty list means "these none", not "all of them". The difference
    matters because `None` is the all-of-them case."""
    assert outbox.retry_failures([]) == 0


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
        token=TEST_TOKEN,
        settings=cloud_settings,
        db=db,
        run_migrations=False,
        mac_key=TEST_MAC_KEY,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        client.post("/auth/login", json=seeded_cashier)
        client.post("/shifts/open", json={"opening_float_paise": 50_000})
        settle_opening(db)
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


def test_a_cashier_cannot_retry_a_failure(cloud_till: TestClient) -> None:
    """Deciding that a refusal no longer applies is the same judgement as
    reading why it happened, and gated on the same permission."""
    response = cloud_till.post("/sync/failures/retry", json={})

    assert response.status_code == 403


def test_a_manager_can_retry_and_is_told_how_many_moved(
    cloud_till: TestClient, seeded_manager: dict
) -> None:
    cloud_till.post("/auth/login", json=seeded_manager)

    response = cloud_till.post("/sync/failures/retry", json={})

    assert response.status_code == 200
    body = response.json()
    # Nothing was quarantined, so nothing moved — and the endpoint says so
    # rather than reporting a success that did nothing.
    assert body["requeued"] == 0
    assert body["status"]["failures"] == 0


def test_status_needs_a_signed_in_session(cloud_settings: Settings, db: Database) -> None:
    app = build_app(
        token=TEST_TOKEN,
        settings=cloud_settings,
        db=db,
        run_migrations=False,
        mac_key=TEST_MAC_KEY,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        assert client.get("/sync/status").status_code == 401


def test_a_refused_row_is_named_the_way_a_person_would_name_it() -> None:
    """`reference` on `/sync/failures`, added when the failures screen was
    finally built (phase 7 acceptance). A manager recognises a sale by its
    receipt number, an unknown scan by its barcode, an override by who lent
    what to whom — never by an outbox id. And a payload with nothing to
    offer says so with a null rather than an empty string that the screen
    would render as a blank."""
    from app.api.sync import _reference_of

    sale = json.dumps({"entity": "sale", "data": {"receipt_no": "ST01-T1-000008"}})
    scan = json.dumps({"entity": "unknown_scan", "data": {"barcode": "8901999000014"}})
    grant = json.dumps(
        {
            "entity": "override",
            "data": {"after_json": json.dumps({"actor_code": "C001", "approver_code": "S001"})},
        }
    )
    movement = json.dumps({"entity": "stock_movement", "data": {"reason": "adjustment"}})

    assert _reference_of(sale) == "ST01-T1-000008"
    assert _reference_of(scan) == "8901999000014"
    assert _reference_of(grant) == "C001 ← S001"
    assert _reference_of(movement) == "adjustment"
    assert _reference_of("{}") is None
    assert _reference_of("not json") is None

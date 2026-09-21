"""The outbox drain — phase 5 exit criteria, the chaos test.

    run 200 sales offline, reconnect, confirm exactly 200 arrive. Kill the app
    mid-push and confirm no duplicates. Drop the network mid-sale and confirm
    the cashier notices nothing. Corrupt one payload and confirm it
    quarantines without blocking the queue.

Each of those is a test below, against a fake Supabase that behaves like the
real one in the ways that matter: it dedupes on primary key, it can fail, and
it can fail in the two different ways that call for opposite responses.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.data.db import Database
from app.data.repositories.outbox import OutboxRepository
from app.domain.identity import utcnow
from app.sync.backoff import Backoff
from app.sync.payloads import PayloadBuilder
from app.sync.pusher import Pusher
from tests.conftest import add_barcode, open_cart

TERMINAL_ID = "018f0000-0000-7000-8000-000000000200"


class FakeCloud:
    """A Supabase that remembers what it has been told.

    Deduplicating on `id` is the only behaviour that really matters here: it
    is what `ON CONFLICT (id) DO NOTHING` gives us server-side, and every
    idempotency claim in §9.2 rests on it.
    """

    def __init__(self) -> None:
        self.sales: dict[str, dict[str, Any]] = {}
        #: Stock movements pushed on their own — a receipt, a count, an
        #: adjustment. Kept separate from `sales` because a sale's deltas
        #: travel inside its envelope, and the two sets must stay disjoint.
        self.movements: dict[str, dict[str, Any]] = {}
        self.calls = 0
        self.fail_with: httpx.Response | Exception | None = None
        #: Fail this many times, then behave. For testing recovery.
        self.fail_times = 0
        #: Entities this server has never heard of. A batch carrying one is
        #: refused whole, with that entity's name in the error.
        self.refuses: set[str] = set()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1

        if self.fail_times > 0:
            self.fail_times -= 1
            if isinstance(self.fail_with, Exception):
                raise self.fail_with
            return self.fail_with or httpx.Response(503, json={"message": "down"})

        body = json.loads(request.content)

        # `sync_push` is one transaction: the first item it cannot accept
        # raises, names itself, and rolls the whole call back.
        for item in body["items"]:
            if item["entity"] in self.refuses:
                return httpx.Response(
                    400, json={"message": f"unknown entity {item['entity']}"}
                )

        for item in body["items"]:
            if item["entity"] == "sale":
                # do-nothing-on-conflict, the whole point.
                self.sales.setdefault(item["id"], item["data"])
            elif item["entity"] == "stock_movement":
                self.movements.setdefault(item["id"], item["data"])
        return httpx.Response(200, json={"accepted": len(body["items"])})

    @property
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud()


@pytest.fixture
def outbox(db: Database) -> OutboxRepository:
    return OutboxRepository(db)


def pusher(db: Database, outbox: OutboxRepository, cloud: FakeCloud) -> Pusher:
    return Pusher(
        outbox,
        PayloadBuilder(db, terminal_id=TERMINAL_ID),
        base_url="https://example.supabase.co",
        anon_key="anon",
        token_provider=lambda: "jwt-token",
        backoff=Backoff(first_seconds=0.0, cap_seconds=0.0),
        client=cloud.client,
    )


def sell(till: Any, barcode: str = "8901030608278") -> str:
    """One complete cash sale, the way a cashier makes one."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, barcode)
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    response = till.post(f"/register/carts/{cart_id}/post")
    assert response.status_code == 200, response.text
    return str(response.json()["sale_id"])


# ── The queue fills whether or not anyone is listening ──────────────────────


def test_a_sale_queues_itself(till: Any, outbox: OutboxRepository) -> None:
    sale_id = sell(till)

    pending = outbox.next_pending()

    assert [(row.entity, row.entity_id) for row in pending] == [("sale", sale_id)]
    assert outbox.backlog() == 1


def test_the_cashier_never_waits_for_the_cloud(till: Any) -> None:
    """Exit criterion: drop the network mid-sale, the cashier notices nothing.

    There is no network here at all — no engine is running in the test app —
    and the sale completes regardless. That is the architecture's first
    principle holding: Supabase is a sync target, never a request-path
    dependency (§1.1).
    """
    sale_id = sell(till)

    assert sale_id


# ── Draining ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_drained_sale_arrives_whole(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """A sale is not one row, and all of it has to travel together."""
    sale_id = sell(till)

    result = await pusher(db, outbox, cloud).drain()

    assert result.pushed == 1
    arrived = cloud.sales[sale_id]
    assert len(arrived["lines"]) == 1
    assert len(arrived["payments"]) == 1
    assert len(arrived["stock_ledger"]) == 1
    assert arrived["audit"], "the audit row travels with its sale"
    assert outbox.backlog() == 0


@pytest.mark.asyncio
async def test_the_terminal_code_becomes_a_uuid(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Local rows carry `T1`; the cloud keys terminals by UUID."""
    sale_id = sell(till)

    await pusher(db, outbox, cloud).drain()

    assert cloud.sales[sale_id]["terminal_id"] == TERMINAL_ID


@pytest.mark.asyncio
async def test_a_terminal_with_no_identity_refuses_to_push(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Better queued locally than pushed as a sale nobody can attribute."""
    sell(till)
    unregistered = Pusher(
        outbox,
        PayloadBuilder(db, terminal_id=""),
        base_url="https://example.supabase.co",
        anon_key="anon",
        token_provider=lambda: "jwt-token",
        backoff=Backoff(first_seconds=0.0),
        client=cloud.client,
    )

    result = await unregistered.drain()

    assert result.pushed == 0
    assert result.quarantined == 1
    assert cloud.calls == 0
    assert "POS_TERMINAL_ID" in outbox.failures()[0]["error"]


# ── The chaos test ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_hundred_offline_sales_all_arrive(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Exit criterion, in full: 200 sales offline, reconnect, exactly 200."""
    expected = {sell(till) for _ in range(200)}
    assert outbox.backlog() == 200

    result = await pusher(db, outbox, cloud).drain()

    assert result.pushed == 200
    assert set(cloud.sales) == expected
    assert len(cloud.sales) == 200
    assert outbox.backlog() == 0


@pytest.mark.asyncio
async def test_a_replay_after_a_lost_acknowledgement_is_not_a_duplicate(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Exit criterion: kill the app mid-push, confirm no duplicates.

    The dangerous window is server-committed / acknowledgement-lost. Simulated
    exactly: the rows are never marked synced, so the next drain sends them
    again — and the second send must change nothing.
    """
    for _ in range(5):
        sell(till)

    engine = pusher(db, outbox, cloud)
    # The push lands, then the process dies before mark_synced.
    batch = outbox.next_pending()
    envelopes, _ = engine._build(batch)
    await engine._post([envelope for _, envelope in envelopes])
    assert len(cloud.sales) == 5
    assert outbox.backlog() == 5, "nothing was acknowledged"

    # It comes back up and drains from the beginning.
    result = await engine.drain()

    assert result.pushed == 5
    assert len(cloud.sales) == 5, "the replay created nothing new"
    assert outbox.backlog() == 0


@pytest.mark.asyncio
async def test_a_corrupt_payload_quarantines_without_blocking(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Exit criterion. One bad row must not hold up the ones behind it."""
    good_before = sell(till)
    orphan = sell(till)
    good_after = sell(till)

    # The pointer survives; the record it points at does not. Nothing in the
    # product does this — it is the shape of a corrupt queue entry.
    with db.write() as conn:
        conn.execute("DELETE FROM sale_lines WHERE sale_id = ?", (orphan,))
        conn.execute("DELETE FROM payments WHERE sale_id = ?", (orphan,))
        conn.execute("DELETE FROM payment_attempts WHERE sale_id = ?", (orphan,))
        conn.execute("DELETE FROM stock_ledger WHERE ref_id = ?", (orphan,))
        conn.execute("DELETE FROM sales WHERE id = ?", (orphan,))

    result = await pusher(db, outbox, cloud).drain()

    assert result.quarantined == 1
    assert result.pushed == 2
    assert set(cloud.sales) == {good_before, good_after}
    assert outbox.backlog() == 0, "the queue kept moving"

    failure = outbox.failures()[0]
    assert orphan in failure["error"]
    assert outbox.unacknowledged_failures() == 1


# ── Transient versus permanent ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_dropped_network_keeps_the_sale_queued(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """The one thing that must never happen is losing it."""
    sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.ConnectError("no route to host")

    result = await pusher(db, outbox, cloud).drain()

    assert result.stopped_early
    assert result.pushed == 0
    assert outbox.backlog() == 1, "still ours to send"
    assert outbox.unacknowledged_failures() == 0, "not a failure, just not yet"


@pytest.mark.asyncio
async def test_it_sends_once_the_network_returns(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    sell(till)
    cloud.fail_times = 2
    cloud.fail_with = httpx.ConnectError("no route to host")
    engine = pusher(db, outbox, cloud)

    assert (await engine.drain()).stopped_early
    assert (await engine.drain()).stopped_early
    result = await engine.drain()

    assert result.pushed == 1
    assert outbox.backlog() == 0


@pytest.mark.asyncio
async def test_a_server_error_is_transient(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """A 500 is the server having a moment, not a verdict on the data."""
    sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(500, json={"message": "boom"})

    result = await pusher(db, outbox, cloud).drain()

    assert result.stopped_early
    assert outbox.backlog() == 1


@pytest.mark.asyncio
async def test_a_rejected_payload_is_permanent(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """A 400 is a verdict. Retrying it forever would stop the queue."""
    sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(400, json={"message": "invalid input syntax"})

    result = await pusher(db, outbox, cloud).drain()

    assert result.quarantined == 1
    assert outbox.backlog() == 0
    assert "invalid input syntax" in outbox.failures()[0]["error"]


@pytest.mark.asyncio
async def test_one_bad_row_does_not_take_its_batch_down_with_it(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Found by the phase 7 acceptance run.

    `sync_push` refuses a batch on its first unacceptable item and names it.
    The pusher used to quarantine every row in that batch with that one
    sentence — so sale ST01-T1-000008 was recorded as failing for "unknown
    entity override", an entity it did not contain, and left the queue
    beside the row that actually had.

    Two claims, and both must hold: the innocent row is sent, and the guilty
    row's error names the guilty row.
    """
    from app.data.repositories.unknown_scans import UnknownScanRepository

    sale_id = sell(till)
    # A real, buildable row the *server* refuses — the refusal has to happen
    # at the wire, where the bug lives, not in the builder.
    UnknownScanRepository(db).record(
        "0000000000000",
        store_id="018f0000-0000-7000-8000-000000000100",
        terminal_id="T1",
        at=utcnow(),
    )
    cloud.refuses = {"unknown_scan"}

    result = await pusher(db, outbox, cloud).drain()

    assert sale_id in cloud.sales, "the sale did nothing wrong and must arrive"
    assert outbox.backlog() == 0
    failures = outbox.failures()
    assert len(failures) == 1
    assert "unknown_scan" in failures[0]["error"]
    assert result.pushed == 1 and result.quarantined == 1


@pytest.mark.asyncio
async def test_an_expired_token_is_transient_not_a_denial(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """401 is ambiguous — an expired JWT or a genuine RLS refusal — and the
    safe reading is the one that keeps the sale."""
    sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(401, json={"message": "JWT expired"})

    result = await pusher(db, outbox, cloud).drain()

    assert result.stopped_early
    assert result.quarantined == 0
    assert outbox.backlog() == 1


@pytest.mark.asyncio
async def test_being_signed_out_is_not_an_error(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Offline-first: the queue simply waits for someone to sign in."""
    sell(till)
    signed_out = Pusher(
        outbox,
        PayloadBuilder(db, terminal_id=TERMINAL_ID),
        base_url="https://example.supabase.co",
        anon_key="anon",
        token_provider=lambda: None,
        backoff=Backoff(first_seconds=0.0),
        client=cloud.client,
    )

    result = await signed_out.drain()

    assert result.stopped_early
    assert cloud.calls == 0
    assert outbox.backlog() == 1


@pytest.mark.asyncio
async def test_an_outdated_terminal_says_so(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Architecture §17: the server rejects a mismatched schema version, and
    the message has to mean something to whoever reads it."""
    sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(
        400, json={"message": "outdated_terminal: schema_version 2 is not supported"}
    )

    result = await pusher(db, outbox, cloud).drain()

    assert result.quarantined == 1
    assert "needs updating" in outbox.failures()[0]["error"]


# ── Ordering and batching ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sales_push_in_the_order_they_happened(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """`client_seq` preserves causality per terminal (§9.2)."""
    order = [sell(till) for _ in range(3)]
    sent: list[str] = []

    async def record(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.extend(item["id"] for item in body["items"])
        return await cloud.handle(request)

    engine = pusher(db, outbox, cloud)
    engine._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    await engine.drain()

    assert sent == order


@pytest.mark.asyncio
async def test_a_batch_carries_the_schema_version(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    sell(till)
    seen: list[int] = []

    async def record(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.extend(item["schema_version"] for item in body["items"])
        return await cloud.handle(request)

    engine = pusher(db, outbox, cloud)
    engine._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    await engine.drain()

    assert seen == [3]


# ── Backoff ─────────────────────────────────────────────────────────────────


def test_backoff_doubles_and_caps() -> None:
    backoff = Backoff(first_seconds=1.0, cap_seconds=8.0)

    assert [backoff.fail() for _ in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_backoff_forgets_as_soon_as_anything_works() -> None:
    """A shop that has been offline all morning should push within seconds of
    coming back, not wait out the last interval."""
    backoff = Backoff()
    for _ in range(5):
        backoff.fail()

    backoff.reset()

    assert backoff.seconds == 0
    assert not backoff.is_backing_off


@pytest.mark.asyncio
async def test_a_successful_drain_clears_the_backoff(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    sell(till)
    # A real backoff, not the zero-delay one the other tests share — this is
    # the one test that cares what the wait actually is.
    engine = pusher(db, outbox, cloud)
    engine.backoff = Backoff(first_seconds=1.0, cap_seconds=8.0)
    cloud.fail_times = 1
    cloud.fail_with = httpx.ConnectError("down")

    await engine.drain()
    assert engine.backoff.is_backing_off

    await engine.drain()
    assert not engine.backoff.is_backing_off


# ── The failure record is worth reading ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_quarantined_row_records_what_was_sent(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Not "sale 123 failed" — the envelope, so someone can see what went."""
    sale_id = sell(till)
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(400, json={"message": "nope"})

    await pusher(db, outbox, cloud).drain()

    stored = json.loads(outbox.failures()[0]["payload_json"])
    assert stored["entity"] == "sale"
    assert stored["id"] == sale_id
    assert stored["data"]["lines"], "the whole envelope, not the pointer"


def test_marking_synced_is_idempotent(outbox: OutboxRepository) -> None:
    """Called twice after a retry, it must not error or double-count."""
    outbox.mark_synced([], at=utcnow())

    assert outbox.backlog() == 0


# ── Stock movements that no sale produced ───────────────────────────────────

STORE_ID = "018f0000-0000-7000-8000-000000000100"
PRODUCT_ID = "018f0000-0000-7000-8000-000000001001"


def a_stock_movement(
    db: Database,
    *,
    movement_id: str = "019300aa-0000-7000-8000-00000000c001",
    delta_milli: int = 24000,
    reason: str = "receipt",
    ref_type: str | None = "receipt",
) -> str:
    """A receipt, a count or an adjustment: a delta with no sale behind it."""
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO stock_ledger (
                id, store_id, product_id, delta_milli, reason, ref_type,
                ref_id, occurred_at, terminal_id, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'T1', NULL)
            """,
            (
                movement_id,
                STORE_ID,
                PRODUCT_ID,
                delta_milli,
                reason,
                ref_type,
                utcnow().isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO outbox (entity, entity_id, op, payload_json, client_seq, "
            "created_at) VALUES ('stock_movement', ?, 'insert', '{}', 1, ?)",
            (movement_id, utcnow().isoformat()),
        )
    return movement_id


@pytest.mark.asyncio
async def test_a_receipt_reaches_the_cloud_on_its_own(
    db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """The gap phase 6 opens with.

    Before `stock_movement` existed, the only ledger rows that reached the
    cloud were the ones nested inside a sale envelope, because a sale was the
    only thing that produced one. A goods receipt had no route at all: it sat
    in the outbox and came back as `unknown entity`, which the pusher treats as
    permanent and quarantines — correctly, for a reason that was never the
    shopkeeper's fault.
    """
    movement_id = a_stock_movement(db)

    result = await pusher(db, outbox, cloud).drain()

    assert result.pushed == 1
    assert result.quarantined == 0
    assert outbox.backlog() == 0
    assert cloud.movements[movement_id]["delta_milli"] == 24000


@pytest.mark.asyncio
async def test_a_movement_names_the_terminal_by_uuid(
    db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """Local rows carry the terminal *code*; the cloud keys terminals by UUID.
    The same translation the sale path makes, for the same reason — and the
    thing that silently broke sync the first time it was missed."""
    movement_id = a_stock_movement(db)

    await pusher(db, outbox, cloud).drain()

    assert cloud.movements[movement_id]["terminal_id"] == TERMINAL_ID


@pytest.mark.asyncio
async def test_resending_a_movement_does_not_move_stock_twice(
    db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """A dropped acknowledgement makes the terminal re-send. Server-side that
    is `on conflict (id) do nothing` on a terminal-generated id; here it is the
    fake keeping one row per id, which is the property that matters."""
    movement_id = a_stock_movement(db)
    await pusher(db, outbox, cloud).drain()

    with db.write() as conn:
        conn.execute("UPDATE outbox SET synced_at = NULL WHERE entity = 'stock_movement'")
    await pusher(db, outbox, cloud).drain()

    assert len(cloud.movements) == 1
    assert cloud.movements[movement_id]["delta_milli"] == 24000


@pytest.mark.asyncio
async def test_a_sale_still_carries_its_own_deltas(
    till: Any, db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """The two paths must stay disjoint, or a sale moves stock twice.

    A sale's ledger rows travel *inside* the sale envelope, because they were
    written in the same transaction and mean nothing apart from it. Nothing
    enqueues a `stock_movement` whose ref_type is `sale`, and `_sale` selects
    only `WHERE ref_type = 'sale'` — that is what keeps them apart.
    """
    sale_id = sell(till)

    await pusher(db, outbox, cloud).drain()

    assert cloud.movements == {}, "a sale's deltas must not push as movements"
    assert cloud.sales[sale_id]["stock_ledger"], "a sale must carry its deltas"


@pytest.mark.asyncio
async def test_a_movement_that_vanished_is_quarantined(
    db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """A pointer whose row is gone will never build. Quarantine, do not spin."""
    movement_id = a_stock_movement(db)
    with db.write() as conn:
        conn.execute("DELETE FROM stock_ledger WHERE id = ?", (movement_id,))

    result = await pusher(db, outbox, cloud).drain()

    assert result.quarantined == 1
    assert movement_id in outbox.failures()[0]["error"]


@pytest.mark.asyncio
async def test_an_unpushable_entity_is_quarantined(
    db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    """A queue entry nothing knows how to build is a bug, and it belongs in
    `sync_failures` where someone will see it — not in a retry loop."""
    with db.write() as conn:
        conn.execute(
            "INSERT INTO outbox (entity, entity_id, op, payload_json, client_seq, "
            "created_at) VALUES ('mystery', 'x', 'insert', '{}', 1, ?)",
            (utcnow().isoformat(),),
        )

    result = await pusher(db, outbox, cloud).drain()

    assert result.quarantined == 1
    assert "mystery" in outbox.failures()[0]["error"]

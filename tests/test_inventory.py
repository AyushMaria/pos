"""Receiving, counting and adjusting — architecture §9.4.

Phase 6's exit criterion is one sentence: receive a delivery by entering case
codes, count a section, and have `stock_levels` reconcile against the ledger.
The last clause earns most of these tests, because it is the property every
other feature in the phase can quietly break — and did once already, when a
duplicate trigger moved every sale's stock twice.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.data.db import Database
from app.data.repositories.inventory import InventoryRepository
from app.domain import receiving
from app.domain.barcode import weighed_barcode, with_check_digit
from app.domain.ids import new_id
from app.domain.receiving import ReceivingError
from tests.conftest import add_barcode, open_cart

#: A retail unit from the seeded catalogue: `pack_size` 1.
UNIT_BARCODE = "8901030608278"  # SKU-SOAP

#: A case of 24 of the same product. Nothing in the seeded catalogue has a
#: pack size above 1, and the multiplication is the part worth testing.
CASE_BARCODE = with_check_digit("1890103060827")

#: Parses cleanly, resolves to nothing. Distinct from a mistyped code, which
#: fails its check digit and is a different error with a different answer.
UNKNOWN_BARCODE = with_check_digit("890999999999")


@pytest.fixture
def inventory(db: Database) -> InventoryRepository:
    return InventoryRepository(db)


@pytest.fixture
def cased(db: Database, catalog: dict[str, str]) -> str:
    """An ITF-14 case code for SKU-SOAP, 24 to the case (§10.2)."""
    with db.write() as conn:
        conn.execute(
            "INSERT INTO product_barcodes (id, product_id, barcode, symbology, "
            "pack_size, is_primary, updated_at) VALUES (?, ?, ?, 'ITF14', 24, 0, ?)",
            (
                new_id(),
                catalog["SKU-SOAP"],
                CASE_BARCODE,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return CASE_BARCODE


@pytest.fixture
def stockroom(till: TestClient, seeded_manager: dict) -> TestClient:
    """The same terminal, signed in as someone who may move stock.

    A cashier holds none of `stock.receive`, `stock.count` or `stock.adjust` —
    receiving a delivery is a different act of trust from selling, and the
    matrix has said so since phase 1.
    """
    assert till.post("/auth/login", json=seeded_manager).status_code == 200
    return till


def receive(client: TestClient, barcode: str, packs: int = 1) -> dict:
    response = client.post(
        "/inventory/receipts", json={"lines": [{"barcode": barcode, "packs": packs}]}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── The arithmetic, before any database ─────────────────────────────────────


def test_one_case_of_24_receives_24_units() -> None:
    """Not one, and not 24,000. Two multiplications stacked — packs to units,
    units to thousandths — and either order of getting it wrong produces a
    plausible-looking number nobody notices until a shelf disagrees."""
    assert receiving.receipt("p", packs=1, pack_size=24).delta_milli == 24_000


def test_a_retail_unit_is_a_pack_size_of_one() -> None:
    assert receiving.receipt("p", packs=3, pack_size=1).delta_milli == 3_000


@pytest.mark.parametrize("packs,pack_size", [(0, 1), (-1, 1), (1, 0)])
def test_a_receipt_of_nothing_is_refused(packs: int, pack_size: int) -> None:
    with pytest.raises(ReceivingError):
        receiving.receipt("p", packs=packs, pack_size=pack_size)


def test_a_count_writes_the_difference_not_the_count() -> None:
    """The ledger records what changed. That is what keeps it reconcilable:
    sum the deltas and you get the level, with no row overriding history."""
    assert receiving.count("p", counted_milli=5_000, expected_milli=8_000).delta_milli == -3_000


def test_a_count_that_matches_corrects_nothing() -> None:
    with pytest.raises(ReceivingError, match="nothing to correct"):
        receiving.count("p", counted_milli=5_000, expected_milli=5_000)


def test_an_adjustment_needs_a_reason() -> None:
    """The only movement with no document behind it, which is why the reason
    is required. An adjustment column full of blanks is where shrinkage goes
    to be forgotten about."""
    with pytest.raises(ReceivingError, match="needs a reason"):
        receiving.adjustment("p", delta_milli=-1_000, note="   ")


# ── Receiving ───────────────────────────────────────────────────────────────


def test_receiving_a_case_adds_the_whole_case(
    stockroom: TestClient, inventory: InventoryRepository, catalog: dict, cased: str
) -> None:
    """The exit criterion's first clause: *by entering case codes*."""
    receive(stockroom, cased, packs=1)

    assert inventory.on_hand(catalog["SKU-SOAP"]) == 24_000


def test_receiving_a_retail_unit_adds_one(
    stockroom: TestClient, inventory: InventoryRepository, catalog: dict
) -> None:
    receive(stockroom, UNIT_BARCODE, packs=6)

    assert inventory.on_hand(catalog["SKU-SOAP"]) == 6_000


def test_a_receipt_queues_itself(stockroom: TestClient, db: Database) -> None:
    """A delivery taken with the line down still reaches the cloud later.
    Before slice 1 no envelope kind could carry one."""
    receive(stockroom, UNIT_BARCODE, packs=2)

    queued = db.query(
        "SELECT entity FROM outbox WHERE entity = 'stock_movement' AND synced_at IS NULL"
    )
    assert len(queued) == 1


def test_a_receipt_is_all_lines_or_none(stockroom: TestClient, db: Database) -> None:
    """Half a delivery is worse than none: the shelf and the screen disagree
    and nothing says which half arrived."""
    before = len(db.query("SELECT id FROM stock_ledger"))

    response = stockroom.post(
        "/inventory/receipts",
        json={
            "lines": [
                {"barcode": UNIT_BARCODE, "packs": 2},
                {"barcode": UNKNOWN_BARCODE, "packs": 1},
            ]
        },
    )

    assert response.status_code == 404
    assert len(db.query("SELECT id FROM stock_ledger")) == before


def test_a_weighed_code_is_not_a_delivery_quantity(stockroom: TestClient) -> None:
    """A `22…` code carries one weighing of one item. That is not a number of
    packs anybody received."""
    response = stockroom.post(
        "/inventory/receipts/preview",
        json={"barcode": weighed_barcode(42, 1250), "packs": 1},
    )

    assert response.status_code == 422
    assert "weighing" in response.json()["detail"]


def test_preview_shows_units_and_thousandths(stockroom: TestClient, cased: str) -> None:
    """A receipt three orders of magnitude out looks plausible in either
    number alone, so the screen shows both."""
    body = stockroom.post(
        "/inventory/receipts/preview", json={"barcode": cased, "packs": 2}
    ).json()

    assert body["pack_size"] == 24
    assert body["units"] == 48
    assert body["delta_milli"] == 48_000


# ── Adjusting ───────────────────────────────────────────────────────────────


def test_an_adjustment_records_why(
    stockroom: TestClient, db: Database, catalog: dict
) -> None:
    response = stockroom.post(
        "/inventory/adjustments",
        json={
            "product_id": catalog["SKU-SOAP"],
            "delta_milli": -2_000,
            "note": "two tins damaged in transit",
        },
    )

    assert response.status_code == 200, response.text
    audit = db.query("SELECT * FROM audit_log WHERE entity = 'stock_ledger'")
    assert len(audit) == 1
    assert "damaged" in audit[0]["after_json"]


def test_an_adjustment_without_a_reason_is_refused(
    stockroom: TestClient, catalog: dict
) -> None:
    response = stockroom.post(
        "/inventory/adjustments",
        json={"product_id": catalog["SKU-SOAP"], "delta_milli": -1_000, "note": ""},
    )

    assert response.status_code == 422


# ── A product that does not exist ───────────────────────────────────────────
#
# Found by the phase 7 permission matrix, which probes these two routes with
# `"no-such-product"` and used to get a 200. The ledger row was written, rode
# the outbox, and was refused by the cloud's foreign key hours later — with a
# constraint name for an error and nobody watching. Local `stock_ledger` has
# no FK of its own, so the service is where the refusal has to live.


def test_an_adjustment_to_a_product_that_does_not_exist_is_refused(
    stockroom: TestClient, db: Database, catalog: dict
) -> None:
    response = stockroom.post(
        "/inventory/adjustments",
        json={"product_id": "no-such-product", "delta_milli": -1_000, "note": "probe"},
    )

    assert response.status_code == 404, response.text
    assert "no product with id no-such-product" in response.json()["detail"]
    # The whole point: nothing was written, so nothing will be refused later.
    assert db.query("SELECT 1 FROM stock_ledger WHERE product_id = 'no-such-product'") == []
    assert db.query("SELECT 1 FROM outbox WHERE entity = 'stock_movement'") == []


def test_a_count_with_one_bad_line_is_refused_whole(
    stockroom: TestClient, db: Database, catalog: dict
) -> None:
    """One typo must not half-commit a section count. The good line here
    would have written a row; the bad one stops it."""
    good = catalog["SKU-SOAP"]
    response = stockroom.post(
        "/inventory/counts",
        json={
            "lines": [
                {"product_id": good, "counted_milli": 5_000},
                {"product_id": "no-such-product", "counted_milli": 1_000},
            ]
        },
    )

    assert response.status_code == 404, response.text
    assert db.query("SELECT 1 FROM stock_ledger WHERE product_id = ?", (good,)) == []


def test_a_withdrawn_product_is_refused_with_its_own_sentence(
    stockroom: TestClient, db: Database, catalog: dict
) -> None:
    """The cloud's FK would accept this row — the product exists, it is only
    soft-deleted. Stock moving on something nobody can sell is exactly the
    shrinkage the reason column exists to catch, so it is refused here, and
    the sentence says why rather than calling it a typo."""
    product_id = catalog["SKU-SOAP"]
    with db.write() as conn:
        conn.execute(
            "UPDATE products SET deleted_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), product_id),
        )

    response = stockroom.post(
        "/inventory/adjustments",
        json={"product_id": product_id, "delta_milli": -1_000, "note": "late breakage"},
    )

    assert response.status_code == 404, response.text
    assert "withdrawn from the catalogue" in response.json()["detail"]
    assert db.query("SELECT 1 FROM stock_ledger WHERE product_id = ?", (product_id,)) == []


def test_a_real_product_still_adjusts(stockroom: TestClient, catalog: dict) -> None:
    """The positive control for the three refusals above. A check whose
    passing state is 'refused' is indistinguishable from a route that refuses
    everything."""
    response = stockroom.post(
        "/inventory/adjustments",
        json={"product_id": catalog["SKU-SOAP"], "delta_milli": -1_000, "note": "breakage"},
    )
    assert response.status_code == 200, response.text


# ── The exit criterion ──────────────────────────────────────────────────────


def test_the_level_is_the_cache_plus_what_is_still_queued(
    stockroom: TestClient, inventory: InventoryRepository, catalog: dict, cased: str
) -> None:
    """What "indicative while offline" actually means (§9.4).

    The terminal has no trigger on `stock_ledger`. Its `stock_levels` is a
    pulled cache of the cloud's number, written only by the puller, and it
    already accounts for everything this till has pushed and nothing it has
    not. So the terminal's belief is the cache plus the queue.

    Reading the cache alone — which is what this repository did at first —
    reports a shelf that was never restocked, and a count against that number
    then corrects the delivery back out of existence.
    """
    product_id = catalog["SKU-SOAP"]
    assert inventory.on_hand(product_id) == 0  # nothing pulled, nothing queued

    receive(stockroom, cased, packs=1)  # +24000, unpushed
    stockroom.post(
        "/inventory/adjustments",
        json={"product_id": product_id, "delta_milli": -1_500, "note": "breakage"},
    )

    assert inventory.pending(product_id) == 22_500
    assert inventory.on_hand(product_id) == 22_500

    held = {row["product_id"]: row["pending"] for row in inventory.unpushed()}
    assert held == {product_id: 22_500}


def test_nothing_is_held_once_the_queue_drains(
    stockroom: TestClient, inventory: InventoryRepository, catalog: dict, db: Database
) -> None:
    """And the cache becomes the whole answer again.

    Acknowledging the outbox is what the pusher does after the cloud confirms.
    The pulled level then arrives carrying those same movements, which is why
    counting them locally as well would double them.
    """
    receive(stockroom, UNIT_BARCODE, packs=4)
    assert inventory.pending(catalog["SKU-SOAP"]) == 4_000

    with db.write() as conn:
        conn.execute("UPDATE outbox SET synced_at = '2026-08-24T00:00:00+00:00'")

    assert inventory.pending(catalog["SKU-SOAP"]) == 0
    assert inventory.unpushed() == []


def test_a_count_does_not_reverse_a_sale(
    till: TestClient,
    seeded_cashier: dict,
    seeded_manager: dict,
    inventory: InventoryRepository,
    catalog: dict,
) -> None:
    """`expected` must be read in the transaction that writes the correction.

    Read it earlier and a sale landing between the two is silently put back —
    the count returns stock that had just gone out of the door, and the ledger
    says the shopkeeper miscounted.
    """
    product_id = catalog["SKU-SOAP"]

    till.post("/auth/login", json=seeded_manager)
    receive(till, UNIT_BARCODE, packs=10)

    # A cashier sells one while the section is being counted.
    till.post("/auth/login", json=seeded_cashier)
    cart_id = open_cart(till)
    add_barcode(till, cart_id, UNIT_BARCODE)
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    till.post(f"/register/carts/{cart_id}/post")
    assert inventory.on_hand(product_id) == 9_000

    till.post("/auth/login", json=seeded_manager)
    till.post(
        "/inventory/counts",
        json={"lines": [{"product_id": product_id, "counted_milli": 9_000}]},
    )

    # The count agreed with what was there, so it corrected nothing — the sale
    # stands and the ledger still adds up.
    assert inventory.on_hand(product_id) == 9_000


def test_a_count_that_matches_leaves_no_row(
    stockroom: TestClient, db: Database, catalog: dict
) -> None:
    receive(stockroom, UNIT_BARCODE, packs=5)
    before = len(db.query("SELECT id FROM stock_ledger"))

    response = stockroom.post(
        "/inventory/counts",
        json={"lines": [{"product_id": catalog["SKU-SOAP"], "counted_milli": 5_000}]},
    )

    assert response.status_code == 200
    assert response.json()["movement_ids"] == []
    assert len(db.query("SELECT id FROM stock_ledger")) == before


# ── Permissions ─────────────────────────────────────────────────────────────


def test_a_cashier_cannot_receive(till: TestClient) -> None:
    """UX hides the screen; this is the layer that refuses. A cashier holds
    none of the three stock permissions."""
    response = till.post(
        "/inventory/receipts", json={"lines": [{"barcode": UNIT_BARCODE, "packs": 1}]}
    )

    assert response.status_code == 403


def test_a_cashier_cannot_adjust(till: TestClient, catalog: dict) -> None:
    response = till.post(
        "/inventory/adjustments",
        json={"product_id": catalog["SKU-SOAP"], "delta_milli": -1_000, "note": "x"},
    )

    assert response.status_code == 403

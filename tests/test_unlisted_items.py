"""Selling something the catalogue has never heard of — phase 6, slice 5.

There is a customer at the counter with an item nothing recognises, and a
queue behind them. "Wait while I add this to the system properly" is not an
answer; offline it is not even an available answer, because phase 6 decided
admin is online-only.

Two things have to be true at once, and they pull against each other:

  * the sale completes now, with the right money and the right GST;
  * the shop finds out, so the catalogue gets fixed.

`0014` resolves that with a placeholder product the line points at, and an
`unknown_scans` row that outlives the terminal.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.data.db import Database
from app.domain.unlisted import UNLISTED_PRODUCT_ID, UNLISTED_SKU
from tests.conftest import open_cart

REPO_ROOT = Path(__file__).resolve().parent.parent

# A well-formed EAN-13 that no fixture product carries: readable, unstocked.
MISSING = "8901999000014"


# ── The code that matched nothing ───────────────────────────────────────────


def test_a_missed_lookup_is_recorded_not_just_refused(till: TestClient) -> None:
    """The most useful evidence in the project, and the table sat empty for it.

    A 404 tells the cashier. It tells nobody else. Which codes real customers
    present that this catalogue cannot answer is exactly the list a shop needs
    to grow its catalogue from, and it can only be collected at the moment of
    the miss.
    """
    cart_id = open_cart(till)
    response = till.post(
        f"/register/carts/{cart_id}/lines", json={"barcode": MISSING}
    )
    assert response.status_code == 404

    scans = till.app.state.unknown_scans.pending()
    assert [row["barcode"] for row in scans] == [MISSING]
    assert scans[0]["resolved"] == 0


def test_the_same_code_twice_is_two_rows(till: TestClient) -> None:
    """Deliberately not deduplicated.

    The same unknown code presented five times in a week is a much stronger
    signal than the same code once. Collapsing them is the difference between
    "somebody once had an odd item" and "we turn this away daily".
    """
    cart_id = open_cart(till)
    for _ in range(3):
        till.post(f"/register/carts/{cart_id}/lines", json={"barcode": MISSING})

    assert till.app.state.unknown_scans.count_pending() == 3


def test_a_mistyped_code_is_not_filed_as_a_missing_product(till: TestClient) -> None:
    """A code that fails its check digit never resolved to anything, so there
    is nothing to report. Filing it would fill the worklist with typos."""
    cart_id = open_cart(till)
    response = till.post(
        f"/register/carts/{cart_id}/lines", json={"barcode": "8901030608274"}
    )
    assert response.status_code == 422

    assert till.app.state.unknown_scans.count_pending() == 0


# ── Selling it anyway ───────────────────────────────────────────────────────


def _sell(till: TestClient, cart_id: str, **overrides: object) -> dict:
    body = {
        "description": "Kissan Mixed Fruit Jam 500g",
        "unit_price_paise": 19500,
        "tax_code": "GST12",
        "barcode": MISSING,
    }
    body.update(overrides)
    return till.post(
        f"/register/carts/{cart_id}/lines/unlisted", json=body
    ).json()


def test_the_line_carries_its_own_identity(till: TestClient, db: Database) -> None:
    """The point of the whole design.

    `sale_lines.product_id` is `not null references public.products(id)`, so a
    locally invented product would fail that key and quarantine the sale. The
    line points at the placeholder; what it actually is rides on the line, in
    columns that have existed since phase 1 for exactly this. Checked after the
    sale is banked, because it is the stored row that syncs — not the basket.
    """
    cart_id = open_cart(till)
    cart = _sell(till, cart_id)
    assert cart["lines"][0]["description"] == "Kissan Mixed Fruit Jam 500g"
    assert cart["lines"][0]["line_total"]["paise"] == 19500

    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    assert till.post(f"/register/carts/{cart_id}/post").status_code == 200

    stored = db.query("SELECT * FROM sale_lines")
    assert len(stored) == 1
    assert stored[0]["product_id"] == UNLISTED_PRODUCT_ID
    assert stored[0]["description"] == "Kissan Mixed Fruit Jam 500g"
    assert stored[0]["barcode_scanned"] == MISSING


def test_the_money_is_right_including_tax(till: TestClient) -> None:
    """An unlisted line is a real line. GST12 inclusive on ₹195.00 is ₹20.89,
    and the same arithmetic every other line goes through produces it."""
    cart_id = open_cart(till)
    cart = _sell(till, cart_id)

    assert cart["total_before_rounding"]["paise"] == 19500
    assert cart["tax_total"]["paise"] == 2089
    assert [c["rate_bp"] for c in cart["tax_breakdown"]] == [1200]


def test_two_unlisted_items_stay_two_lines(till: TestClient) -> None:
    """Merging is right for two scans of the same book and wrong here: these
    are two different real products that happen to share a placeholder, and
    folding them would keep one description and lose the other."""
    cart_id = open_cart(till)
    _sell(till, cart_id, description="Jam", barcode=MISSING)
    cart = _sell(till, cart_id, description="Pickle", barcode=None)

    assert [line["description"] for line in cart["lines"]] == ["Jam", "Pickle"]


def test_selling_it_anyway_also_files_the_code(till: TestClient) -> None:
    """A code sold against is more urgent than one merely scanned: there is now
    a sale line in the books that no product explains."""
    cart_id = open_cart(till)
    till.post(f"/register/carts/{cart_id}/lines", json={"barcode": MISSING})
    _sell(till, cart_id)

    # Once from the missed lookup, once from the sale.
    assert till.app.state.unknown_scans.count_pending() == 2


def test_a_line_with_no_description_is_refused(till: TestClient) -> None:
    """An anonymous amount of money on a receipt is unauditable, and the
    identity is the only thing that makes the row fixable later."""
    cart_id = open_cart(till)
    response = till.post(
        f"/register/carts/{cart_id}/lines/unlisted",
        json={"description": "   ", "unit_price_paise": 100, "tax_code": "GST12"},
    )
    assert response.status_code == 422


def test_the_rate_comes_from_the_catalogue(till: TestClient) -> None:
    """A tax rate the client chose is a tax bill the client chose. The request
    names a code; the server looks up what that code means."""
    cart_id = open_cart(till)
    response = till.post(
        f"/register/carts/{cart_id}/lines/unlisted",
        json={
            "description": "Jam",
            "unit_price_paise": 100,
            "tax_code": "GST-MADE-UP",
        },
    )
    assert response.status_code == 422
    assert "GST-MADE-UP" in response.json()["detail"]


def test_the_form_is_offered_the_shop_s_own_rates(till: TestClient) -> None:
    codes = till.get("/catalog/tax-codes").json()["tax_codes"]
    assert [rate["code"] for rate in codes] == ["GST0", "GST5", "GST12", "GST18"]
    assert codes[3]["rate_bp"] == 1800


# ── Getting it to the cloud ─────────────────────────────────────────────────


def test_a_scan_is_queued_for_the_cloud(till: TestClient) -> None:
    """The queue is only useful if it outlives the terminal. A shop that loses
    a machine should not also lose the list of things it could not identify."""
    cart_id = open_cart(till)
    till.post(f"/register/carts/{cart_id}/lines", json={"barcode": MISSING})

    queued = till.app.state.outbox.next_pending()
    assert "unknown_scan" in [row.entity for row in queued]


def test_the_payload_carries_the_terminal_uuid(till: TestClient) -> None:
    """Local rows hold the terminal *code*; the cloud keys terminals by UUID.
    Same translation every other entity makes, for the same reason."""
    from app.sync.payloads import PayloadBuilder

    cart_id = open_cart(till)
    till.post(f"/register/carts/{cart_id}/lines", json={"barcode": MISSING})
    scan = till.app.state.unknown_scans.pending()[0]

    envelope = PayloadBuilder(
        till.app.state.db, terminal_id="018f0000-0000-7000-8000-00000000t111"
    ).build("unknown_scan", scan["id"], "insert", 1)

    assert envelope.entity == "unknown_scan"
    assert envelope.data["barcode"] == MISSING
    assert envelope.data["resolved"] is False
    assert envelope.data["store_id"]
    assert envelope.data["terminal_id"] == "018f0000-0000-7000-8000-00000000t111"


# ── The two halves must agree about the placeholder ─────────────────────────


def test_the_placeholder_id_matches_the_migration() -> None:
    """A terminal that guessed a different id would write sale lines that fail
    the very foreign key this design exists to avoid — and the failure would
    appear as a quarantined sale hours later, not as a test."""
    migration = (
        REPO_ROOT / "supabase" / "migrations" / "0014_unlisted_and_unknown_scans.sql"
    ).read_text(encoding="utf-8")

    assert UNLISTED_PRODUCT_ID in migration
    assert UNLISTED_SKU in migration


def test_the_placeholder_does_not_track_stock() -> None:
    """One row standing in for many unrelated products. A stock level on it
    would be the sum of things that have nothing to do with each other."""
    migration = (
        REPO_ROOT / "supabase" / "migrations" / "0014_unlisted_and_unknown_scans.sql"
    ).read_text(encoding="utf-8")
    insert = migration[migration.index("insert into public.products") :]

    assert "false" in insert[: insert.index("on conflict")]


def test_the_sync_function_knows_the_entity() -> None:
    """The pusher sends mixed batches to `sync_push` and nothing else, so a
    separate RPC would simply never be called."""
    migration = (
        REPO_ROOT / "supabase" / "migrations" / "0014_unlisted_and_unknown_scans.sql"
    ).read_text(encoding="utf-8")

    assert "when 'unknown_scan' then" in migration
    assert "create or replace function public.sync_push(" in migration


def test_the_payload_json_is_a_pointer_not_a_copy(till: TestClient) -> None:
    """Outbox payloads are rebuilt from the tables at push time, so a row
    edited between queueing and pushing pushes its current state."""
    cart_id = open_cart(till)
    till.post(f"/register/carts/{cart_id}/lines", json={"barcode": MISSING})

    queued = next(
        row
        for row in till.app.state.outbox.next_pending()
        if row.entity == "unknown_scan"
    )
    stored = queued.payload

    assert stored["barcode"] == MISSING

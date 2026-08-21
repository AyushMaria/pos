"""The vertical slice — phase 3 exit criteria, end to end.

    enter 10 items by barcode and by search, void a line, take cash, produce a
    legible receipt with a correct GST breakdown and a visible rounding line,
    sale lands in SQLite with balanced totals. Kill the process mid-sale and
    confirm no partial sale exists.

Every one of those is a test below, over the real HTTP API against a real
SQLite file.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.data.db import Database
from app.domain.barcode import weighed_barcode, with_check_digit
from app.domain.ids import new_id
from tests.conftest import PRODUCTS, add_barcode, open_cart

# ── Entering items ──────────────────────────────────────────────────────────


def test_a_barcode_puts_a_line_on_the_screen(till: TestClient) -> None:
    cart_id = open_cart(till)
    cart = add_barcode(till, cart_id, "8901262010016")

    assert cart["item_count"] == 1
    assert cart["lines"][0]["description"].startswith("Amul")
    assert cart["lines"][0]["line_total"]["paise"] == 3300
    assert cart["total_before_rounding"]["text"] == "₹33.00"


def test_ten_items_by_barcode_and_by_search(till: TestClient, catalog: dict) -> None:
    """Exit criterion: enter 10 items, by barcode *and* by search."""
    cart_id = open_cart(till)

    for barcode in [p[5] for p in PRODUCTS]:
        add_barcode(till, cart_id, barcode)

    found = till.get("/catalog/search", params={"q": "colgate"}).json()
    assert found["results"], "search found nothing"

    for _ in range(5):
        response = till.post(
            f"/register/carts/{cart_id}/lines",
            json={"product_id": found["results"][0]["product_id"]},
        )
        assert response.status_code == 200, response.text

    cart = till.get(f"/register/carts/{cart_id}").json()
    assert cart["item_count"] == 10


def test_search_finds_products_with_no_barcode(till: TestClient, db: Database) -> None:
    """The only route in for 38.6% of the real catalogue."""
    now = datetime.now(timezone.utc).isoformat()
    product_id = new_id()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO products (id, sku, name, short_name, uom, is_weighed, "
            "track_stock, tax_code, is_active, updated_at) "
            "VALUES (?, 'SKU-LOOSE', 'Loose Tea Dust', 'Tea Dust', 'kg', 1, 1, "
            "'GST5', 1, ?)",
            (product_id, now),
        )
        conn.execute(
            "INSERT INTO product_prices (id, product_id, store_id, price, valid_from) "
            "VALUES (?, ?, 'ST01', 24000, ?)",
            (new_id(), product_id, now),
        )

    results = till.get("/catalog/search", params={"q": "tea dust"}).json()["results"]

    assert results and results[0]["sku"] == "SKU-LOOSE"


def test_a_mistyped_code_and_an_unstocked_one_differ(till: TestClient) -> None:
    """Two problems, two different next actions for the cashier."""
    cart_id = open_cart(till)

    mistyped = till.post(
        f"/register/carts/{cart_id}/lines", json={"barcode": "8901262010017"}
    )
    assert mistyped.status_code == 422
    assert "could not be read" in mistyped.json()["detail"]

    unstocked = till.post(
        f"/register/carts/{cart_id}/lines", json={"barcode": "5901234123457"}
    )
    assert unstocked.status_code == 404
    assert "searching by name" in unstocked.json()["detail"]


def test_a_weighed_code_carries_its_own_quantity(
    till: TestClient, db: Database
) -> None:
    """The POS generates these; scanning one back must recover the weight."""
    now = datetime.now(timezone.utc).isoformat()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO products (id, sku, name, short_name, uom, is_weighed, "
            "track_stock, tax_code, is_active, updated_at) "
            "VALUES (?, '00042', 'Loose Walnuts', 'Walnuts', 'kg', 1, 1, 'GST5', 1, ?)",
            (new_id(), now),
        )
        conn.execute(
            "INSERT INTO product_prices (id, product_id, store_id, price, valid_from) "
            "SELECT ?, id, 'ST01', 120000, ? FROM products WHERE sku = '00042'",
            (new_id(), now),
        )

    cart_id = open_cart(till)
    cart = add_barcode(till, cart_id, weighed_barcode(42, 1250))

    assert cart["lines"][0]["qty_milli"] == 1250
    assert cart["lines"][0]["quantity_text"] == "1.25"
    assert cart["lines"][0]["line_total"]["paise"] == 150000  # 1.25 kg at ₹1200


# ── Voiding ─────────────────────────────────────────────────────────────────


def test_voiding_a_line(till: TestClient) -> None:
    """Exit criterion: void a line."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901262010016")
    add_barcode(till, cart_id, "8901030865275")
    add_barcode(till, cart_id, "8901719101250")

    response = till.delete(f"/register/carts/{cart_id}/lines/2")
    assert response.status_code == 200

    cart = response.json()
    assert cart["item_count"] == 2
    assert [line["line_no"] for line in cart["lines"]] == [1, 3]
    assert cart["total_before_rounding"]["paise"] == 3300 + 2500


def test_changing_a_quantity_reprices(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901719101250")

    cart = till.patch(
        f"/register/carts/{cart_id}/lines/1", json={"qty_milli": 3000}
    ).json()

    assert cart["lines"][0]["line_total"]["paise"] == 7500


# ── Tender ──────────────────────────────────────────────────────────────────


def test_cash_rounds_and_upi_does_not(till: TestClient) -> None:
    """₹37.40 collects as ₹37 in cash but ₹37.40 over UPI (architecture §13.4)."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")  # ₹37.40

    cash = till.get(
        f"/register/carts/{cart_id}/tender-quote", params={"method": "cash"}
    ).json()
    upi = till.get(
        f"/register/carts/{cart_id}/tender-quote", params={"method": "upi"}
    ).json()

    assert cash["collected"]["paise"] == 3700
    assert cash["rounding_adjustment"]["paise"] == -40
    assert upi["collected"]["paise"] == 3740
    assert upi["rounding_adjustment"]["paise"] == 0


def test_taking_cash_settles_the_sale(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")

    response = till.post(
        f"/register/carts/{cart_id}/payments", json={"method": "cash"}
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["state"] == "approved"
    assert body["cart"]["settled"] is True
    assert body["cart"]["outstanding"]["paise"] == 0


def test_change_is_computed_from_what_was_handed_over(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")  # ₹37.40 -> ₹37 cash

    body = till.post(
        f"/register/carts/{cart_id}/payments",
        json={"method": "cash", "tendered_paise": 10000},
    ).json()

    assert body["change_due"]["paise"] == 6300  # ₹100 - ₹37


def test_the_basket_locks_once_money_is_taken(till: TestClient) -> None:
    """Moving a total under a payment already collected is how a till ends short."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901262010016")
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})

    blocked = till.post(
        f"/register/carts/{cart_id}/lines", json={"barcode": "8901719101250"}
    )
    assert blocked.status_code == 409


def test_an_empty_basket_cannot_take_payment(till: TestClient) -> None:
    cart_id = open_cart(till)

    response = till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})

    assert response.status_code == 400


# ── The complete sale ───────────────────────────────────────────────────────


def complete_a_sale(till: TestClient) -> dict:
    cart_id = open_cart(till)
    for barcode in [p[5] for p in PRODUCTS]:
        add_barcode(till, cart_id, barcode)
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})

    response = till.post(f"/register/carts/{cart_id}/post")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_complete_cash_sale(till: TestClient) -> None:
    """Exit criterion: the whole slice, in one go."""
    sale = complete_a_sale(till)

    assert sale["receipt_no"].startswith("ST01-T1-")
    assert sale["status"] == "completed"
    # 33.00 + 275.00 + 25.00 + 115.00 + 37.40 = 485.40, cash-rounded to 485.
    assert sale["grand_total"]["paise"] == 48500
    assert sale["rounding_adjustment"]["paise"] == -40


def test_the_receipt_is_legible_and_shows_its_working(till: TestClient) -> None:
    """Exit criterion: correct GST breakdown and a *visible* rounding line."""
    sale = complete_a_sale(till)
    text = sale["receipt_text"]

    assert "Rounding" in text, "the rounding line must be visible"
    assert "-₹0.40" in text
    assert "TOTAL" in text and "₹485.00" in text

    # One row per rate, each with its CGST/SGST split.
    assert "GST 5%" in text
    assert "GST 12%" in text
    assert "GST 18%" in text
    assert "CGST / SGST" in text

    assert "<section class=\"receipt\">" in sale["receipt_html"]


def test_an_unregistered_tender_method_is_refused(till: TestClient) -> None:
    """The registry is the whole extension point (architecture §13.1).

    Cash and UPI are registered; a card terminal is not, and asking for one
    fails loudly rather than silently posting an unpaid sale. UPI itself is
    exercised in tests/test_upi_tender.py.
    """
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")

    response = till.post(f"/register/carts/{cart_id}/payments", json={"method": "card"})

    assert response.status_code == 422
    assert "unknown tender method" in response.json()["detail"]


def test_a_sale_with_no_rounding_shows_no_rounding_line(till: TestClient) -> None:
    """A whole-rupee basket has nothing to explain, so the line is absent.

    Its absence is informative: it is how a receipt shows that no cash
    adjustment was applied (architecture §13.4).
    """
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901262010016")  # ₹33.00 exactly
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    assert sale["rounding_adjustment"]["paise"] == 0
    assert "Rounding" not in sale["receipt_text"]


def test_the_sale_lands_in_sqlite_with_balanced_totals(
    till: TestClient, db: Database
) -> None:
    """Exit criterion, checked by reading back what actually landed."""
    from app.data.repositories.sales import SalesRepository

    sale = complete_a_sale(till)
    repo = SalesRepository(db)

    assert repo.count() == 1
    assert repo.totals_balance(sale["sale_id"]), "stored sale does not reconcile"

    stored = repo.get(sale["sale_id"])
    assert stored["status"] == "completed"
    assert stored["grand_total"] == 48500
    assert stored["rounding_adjustment"] == -40
    assert len(repo.lines_for(sale["sale_id"])) == 5
    assert len(repo.payments_for(sale["sale_id"])) == 1


def test_posting_writes_the_ledger_audit_and_outbox_together(
    till: TestClient, db: Database
) -> None:
    """One transaction: everything that makes the sale real, or none of it."""
    sale = complete_a_sale(till)

    ledger = db.query(
        "SELECT delta_milli FROM stock_ledger WHERE ref_id = ?", (sale["sale_id"],)
    )
    assert len(ledger) == 5
    assert all(row[0] < 0 for row in ledger), "a sale reduces stock"

    audit = db.query_one(
        "SELECT action FROM audit_log WHERE entity_id = ?", (sale["sale_id"],)
    )
    assert audit["action"] == "sale.post"

    outbox = db.query_one(
        "SELECT entity, synced_at FROM outbox WHERE entity_id = ?", (sale["sale_id"],)
    )
    assert outbox["entity"] == "sale"
    assert outbox["synced_at"] is None, "not yet pushed"


def test_a_sale_cannot_post_before_it_is_paid(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901262010016")

    response = till.post(f"/register/carts/{cart_id}/post")

    assert response.status_code == 409
    assert "outstanding" in response.json()["detail"]


def test_receipt_numbers_are_sequential(till: TestClient) -> None:
    """Unique without coordination, and gaps are auditable (architecture §7)."""
    first = complete_a_sale(till)["receipt_no"]
    second = complete_a_sale(till)["receipt_no"]

    assert first == "ST01-T1-000001"
    assert second == "ST01-T1-000002"


# ── Crash safety ────────────────────────────────────────────────────────────


def test_a_crash_mid_post_leaves_no_partial_sale(
    till: TestClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit criterion: kill the process mid-sale, confirm no partial sale.

    The failure is injected *inside* the post transaction, after the sale and
    some of its lines have been written. If the boundary were anywhere other
    than where architecture §8 puts it, this test would find a headless sale
    with no payments against it.
    """
    from app.data.repositories.sales import SalesRepository

    cart_id = open_cart(till)
    for barcode in [p[5] for p in PRODUCTS]:
        add_barcode(till, cart_id, barcode)
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})

    real_new_id = SalesRepository.__module__
    calls = {"n": 0}

    def exploding_new_id() -> str:
        calls["n"] += 1
        if calls["n"] > 4:
            raise RuntimeError("power cut")
        return new_id()

    monkeypatch.setattr("app.data.repositories.sales.new_id", exploding_new_id)
    assert real_new_id  # keeps the import meaningful

    with pytest.raises(RuntimeError, match="power cut"):
        till.post(f"/register/carts/{cart_id}/post")

    repo = SalesRepository(db)
    assert repo.count() == 0, "a partial sale survived the crash"
    assert db.query("SELECT id FROM sale_lines") == []
    assert db.query("SELECT id FROM payments") == []
    assert db.query("SELECT id FROM stock_ledger") == []
    assert db.query("SELECT id FROM outbox") == []


def test_the_cart_survives_a_failed_post(
    till: TestClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before commit is recoverable; the cashier retries."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901262010016")
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("power cut")

    monkeypatch.setattr("app.data.repositories.sales.new_id", explode)
    with pytest.raises(RuntimeError):
        till.post(f"/register/carts/{cart_id}/post")

    monkeypatch.undo()
    retried = till.post(f"/register/carts/{cart_id}/post")

    assert retried.status_code == 200, "the cashier could not retry the sale"


# ── Reprint ─────────────────────────────────────────────────────────────────


def test_reprint_by_sale_id(till: TestClient, tmp_path) -> None:
    """Phase 3 deliverable: reprint by sale id, marked as a reprint."""
    sale = complete_a_sale(till)

    response = till.post(f"/register/sales/{sale['sale_id']}/receipt.pdf")
    assert response.status_code == 200, response.text

    from pathlib import Path

    written = Path(response.json()["path"])
    assert written.exists() and written.stat().st_size > 500
    assert written.suffix == ".pdf"


def test_a_reprint_reproduces_the_original_figures(till: TestClient, db: Database) -> None:
    from app.services.receipt_render import render_text

    sale = complete_a_sale(till)
    service = till.app.state.sale_service  # type: ignore[attr-defined]

    receipt = service.receipt_for_sale(sale["sale_id"], "Someone Else")

    assert receipt is not None
    assert receipt.is_reprint
    assert receipt.grand_total.paise == sale["grand_total"]["paise"]
    assert receipt.rounding_adjustment.paise == -40
    assert "REPRINT" in render_text(receipt)
    # The cashier who made the sale, not whoever reprinted it.
    assert receipt.cashier_name == "Anita Rao"


def test_reprinting_an_unknown_sale(till: TestClient) -> None:
    assert till.post(f"/register/sales/{new_id()}/receipt.pdf").status_code == 404


def test_a_stray_barcode_is_still_a_valid_ean13() -> None:
    """Guards the fixture itself: the seeded codes must be real GTINs."""
    for _sku, _name, _price, _code, _rate, barcode in PRODUCTS:
        assert with_check_digit(barcode[:-1]) == barcode, barcode

"""Shifts — phase 8 slices 1 and 2, at the HTTP layer.

The domain arithmetic has its own properties in `tests/domain/test_shift.py`.
What is asserted here is the shape the plan fixed: a sale needs an open
shift, the close is a row that rides the outbox, the X-report never shows
the drawer, and the numbers a real day produces through the real register
agree with the arithmetic.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.data.db import Database
from app.data.repositories.shifts import ShiftRepository
from tests.conftest import TEST_OPENING_FLOAT, add_barcode, open_cart

SOAP = "8901030608278"  # ₹37.40, GST18 — the odd price, so cash has to round


def sell_cash(till: TestClient, barcode: str = SOAP) -> dict[str, Any]:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, barcode)
    cash = till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    assert cash.status_code == 200, cash.text
    response = till.post(f"/register/carts/{cart_id}/post")
    assert response.status_code == 200, response.text
    return response.json()


def sell_split(till: TestClient, upi_paise: int, barcode: str = SOAP) -> dict[str, Any]:
    """UPI first for part of it, cash for the rest — the §13.4 case."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, barcode)
    upi = till.post(f"/register/carts/{cart_id}/payments", json={"method": "upi"})
    assert upi.status_code == 200, upi.text
    attempt_id = upi.json()["attempt_id"]
    attested = till.post(
        f"/register/payments/{attempt_id}/confirm", json={"amount_paise": upi_paise}
    )
    assert attested.status_code == 200, attested.text
    cash = till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    assert cash.status_code == 200, cash.text
    response = till.post(f"/register/carts/{cart_id}/post")
    assert response.status_code == 200, response.text
    return response.json()


# ── A sale needs a shift ────────────────────────────────────────────────────


def test_the_register_will_not_open_a_cart_without_a_shift(
    client: TestClient, seeded_cashier: dict, catalog: dict
) -> None:
    """Decision 2, at the first scan rather than at payment."""
    assert client.post("/auth/login", json=seeded_cashier).status_code == 200

    response = client.post("/register/carts")

    assert response.status_code == 409, response.text
    assert "open a shift" in response.json()["detail"]


def test_a_cart_opened_before_the_close_cannot_be_posted_after_it(
    till: TestClient, seeded_manager: dict, seeded_cashier: dict
) -> None:
    """The backstop in `SaleService.post`, for the cart that straddles a close."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, SOAP)
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})

    assert till.post("/auth/login", json=seeded_manager).status_code == 200
    closed = till.post("/shifts/close", json={"counted_cash_paise": TEST_OPENING_FLOAT})
    assert closed.status_code == 200, closed.text
    assert till.post("/auth/login", json=seeded_cashier).status_code == 200

    response = till.post(f"/register/carts/{cart_id}/post")

    assert response.status_code == 409, response.text
    assert "open a shift" in response.json()["detail"]


def test_a_posted_sale_carries_its_shift(till: TestClient, db: Database) -> None:
    posted = sell_cash(till)
    current = till.get("/shifts/current").json()

    row = db.query_one("SELECT session_id FROM sales WHERE id = ?", (posted["sale_id"],))
    assert row is not None and row["session_id"] == current["id"]


# ── Opening ─────────────────────────────────────────────────────────────────


def test_only_one_shift_opens_per_terminal(till: TestClient) -> None:
    response = till.post("/shifts/open", json={"opening_float_paise": 1_000})

    assert response.status_code == 409, response.text
    assert "already open" in response.json()["detail"]


def test_current_names_the_open_shift(till: TestClient) -> None:
    response = till.get("/shifts/current")

    assert response.status_code == 200
    assert response.json()["opening_float"]["paise"] == TEST_OPENING_FLOAT


def test_current_is_a_404_when_nothing_is_open(
    client: TestClient, seeded_cashier: dict
) -> None:
    assert client.post("/auth/login", json=seeded_cashier).status_code == 200
    assert client.get("/shifts/current").status_code == 404


def test_opening_is_queued_ahead_of_the_first_sale(till: TestClient, db: Database) -> None:
    """`sales.session_id` is a foreign key in the cloud; the parent goes first."""
    sell_cash(till)
    rows = db.query("SELECT entity FROM outbox ORDER BY client_seq")
    assert [r["entity"] for r in rows] == ["register_session", "sale"]


# ── Cash movements ──────────────────────────────────────────────────────────


def test_a_cashier_cannot_move_cash(till: TestClient) -> None:
    response = till.post(
        "/shifts/cash", json={"direction": "out", "amount_paise": 5_000, "reason": "tea"}
    )
    assert response.status_code == 403


def test_a_manager_pays_out_and_it_is_audited_and_queued(
    till: TestClient, seeded_manager: dict, db: Database
) -> None:
    assert till.post("/auth/login", json=seeded_manager).status_code == 200

    response = till.post(
        "/shifts/cash", json={"direction": "out", "amount_paise": 5_000, "reason": "tea"}
    )

    assert response.status_code == 200, response.text
    movement_id = response.json()["movement_id"]
    assert db.query_one("SELECT approved_by FROM cash_movements WHERE id = ?", (movement_id,))[
        "approved_by"
    ] is None
    audit = db.query("SELECT action FROM audit_log WHERE entity = 'cash_movement'")
    assert [a["action"] for a in audit] == ["cash.out"]
    queued = db.query("SELECT entity_id FROM outbox WHERE entity = 'cash_movement'")
    assert [q["entity_id"] for q in queued] == [movement_id]


def test_a_payout_needs_a_reason(till: TestClient, seeded_manager: dict) -> None:
    assert till.post("/auth/login", json=seeded_manager).status_code == 200
    response = till.post(
        "/shifts/cash", json={"direction": "out", "amount_paise": 5_000, "reason": "   "}
    )
    assert response.status_code == 422


# ── The X-report ────────────────────────────────────────────────────────────


def test_the_x_report_never_shows_the_drawer(till: TestClient) -> None:
    """A supervisor who sees expected before counting types expected."""
    sell_cash(till)

    body = till.get("/shifts/current/x").json()

    assert body["figures"]["expected_cash"] is None
    assert body["figures"]["variance"] is None
    assert body["figures"]["counted_cash"] is None
    assert body["figures"]["cash_sales"]["paise"] == 3_700


# ── The close ───────────────────────────────────────────────────────────────


def test_a_days_figures_through_the_real_register(
    till: TestClient, seeded_manager: dict, db: Database
) -> None:
    """Three sales the way a cashier makes them, one payout, then the count.

    Soap is ₹37.40. Cash rounds to ₹37 (-40 paise). A ₹20 UPI + cash split
    leaves ₹17.40 owing in cash, collected as ₹17 (-40 paise). The numbers
    below are worked by hand from §13.4, not read back from the code.
    """
    sell_cash(till)                      # ₹37 cash, -40 paise
    sell_cash(till)                      # ₹37 cash, -40 paise
    sell_split(till, upi_paise=2_000)    # ₹20 UPI attested, ₹17 cash, -40 paise

    assert till.post("/auth/login", json=seeded_manager).status_code == 200
    payout = till.post(
        "/shifts/cash", json={"direction": "out", "amount_paise": 5_000, "reason": "tea"}
    )
    assert payout.status_code == 200, payout.text

    counted = TEST_OPENING_FLOAT + 3_700 + 3_700 + 1_700 - 5_000 - 100  # ₹1 short
    response = till.post("/shifts/close", json={"counted_cash_paise": counted, "note": "x"})

    assert response.status_code == 200, response.text
    f = response.json()["figures"]
    assert f["cash_sales"]["paise"] == 9_100
    assert f["upi_attested"]["paise"] == 2_000
    assert f["upi_verified"]["paise"] == 0
    assert f["cash_out"]["paise"] == 5_000
    assert f["rounding"]["paise"] == -120
    assert f["sales_count"] == 3
    assert f["expected_cash"]["paise"] == TEST_OPENING_FLOAT + 9_100 - 5_000
    assert f["variance"]["paise"] == -100

    stored = db.query_one("SELECT * FROM shift_closes")
    assert stored is not None
    assert stored["variance"] == -100 and stored["rounding"] == -120
    assert db.query_one("SELECT status FROM register_sessions")["status"] == "closed"
    queued = db.query("SELECT entity FROM outbox WHERE entity = 'shift_close'")
    assert len(queued) == 1
    audit = db.query("SELECT action FROM audit_log WHERE action = 'shift.closed'")
    assert len(audit) == 1


def test_closing_needs_the_key(till: TestClient) -> None:
    assert till.post("/shifts/close", json={"counted_cash_paise": 0}).status_code == 403


def test_closing_twice_is_refused(till: TestClient, seeded_manager: dict) -> None:
    assert till.post("/auth/login", json=seeded_manager).status_code == 200
    assert till.post("/shifts/close", json={"counted_cash_paise": 0}).status_code == 200

    response = till.post("/shifts/close", json={"counted_cash_paise": 0})

    assert response.status_code == 409
    assert till.get("/shifts/current").status_code == 404


def test_a_close_is_pushed_as_it_was_signed(
    till: TestClient, seeded_manager: dict, db: Database
) -> None:
    """The payload the cloud receives is the row, figures included. A review
    resolved tomorrow changes tomorrow's numbers, not tonight's."""
    from app.sync.payloads import PayloadBuilder

    sell_cash(till)
    assert till.post("/auth/login", json=seeded_manager).status_code == 200
    close_id = till.post("/shifts/close", json={"counted_cash_paise": 0}).json()["close_id"]

    envelope = PayloadBuilder(db, terminal_id="00000000-0000-7000-8000-000000000001").build(
        "shift_close", close_id, "insert", 1
    )

    assert envelope.data["cash_sales"] == 3_700
    assert envelope.data["expected_cash"] == TEST_OPENING_FLOAT + 3_700
    assert envelope.data["variance"] == -(TEST_OPENING_FLOAT + 3_700)


def test_a_session_payload_names_the_terminal_by_uuid(till: TestClient, db: Database) -> None:
    from app.sync.payloads import PayloadBuilder

    session_id = till.get("/shifts/current").json()["id"]
    envelope = PayloadBuilder(db, terminal_id="00000000-0000-7000-8000-000000000001").build(
        "register_session", session_id, "insert", 1
    )

    assert envelope.data["terminal_id"] == "00000000-0000-7000-8000-000000000001"
    assert envelope.data["opening_float"] == TEST_OPENING_FLOAT
    assert "status" not in envelope.data
    assert envelope.data["store_id"]


def test_repository_reads_only_approved_payments(till: TestClient, db: Database) -> None:
    """A cancelled attempt leaves a `payments` row in some flows; the close
    must not count money that never arrived."""
    sell_cash(till)
    session_id = till.get("/shifts/current").json()["id"]
    with db.write() as conn:
        conn.execute("UPDATE payments SET status = 'voided'")

    assert ShiftRepository(db).payments_for(session_id) == []

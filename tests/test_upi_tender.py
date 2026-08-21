"""UPI tender — phase 4 exit criteria, end to end.

    all three tender combinations complete correctly; a short UPI payment
    leaves a balance the cashier can settle in cash without leaving the sale;
    an expired attempt releases the cart cleanly; an attested UPI sale is
    visibly distinct from a verified one in the data; a UPI-only sale carries
    no cash rounding adjustment.

The shops take UPI on a **printed counter QR** (architecture §13.3), which is
what makes this suite look different from the one the original design implied.
There is no QR to render and nothing to poll. What there *is*, and what is
tested hardest here, is a customer typing an amount into their own app — so a
payment can arrive short, or over, and neither is an error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.data.db import Database
from app.domain.ids import new_id
from tests.conftest import add_barcode, open_cart


def begin_upi(till: TestClient, cart_id: str) -> dict:
    response = till.post(f"/register/carts/{cart_id}/payments", json={"method": "upi"})
    assert response.status_code == 200, response.text
    return response.json()


def attest(till: TestClient, attempt_id: str, **body: object) -> dict:
    response = till.post(f"/register/payments/{attempt_id}/confirm", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ── The provider ────────────────────────────────────────────────────────────


def test_upi_opens_an_attempt_and_waits(till: TestClient) -> None:
    """Nothing resolves it. There is no QR to render and no PSP to ask."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")  # ₹37.40

    tender = begin_upi(till, cart_id)

    assert tender["state"] == "pending"
    assert tender["cart"]["settled"] is False
    assert tender["cart"]["outstanding"]["paise"] == 3740


def test_a_upi_attempt_carries_an_expiry(till: TestClient) -> None:
    """An abandoned payment must not wedge the cart (architecture §13.3)."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")

    tender = begin_upi(till, cart_id)

    assert tender["expires_at"] is not None
    expires = datetime.fromisoformat(tender["expires_at"])
    assert expires - datetime.now(timezone.utc) <= timedelta(minutes=5, seconds=1)


def test_cash_carries_no_expiry(till: TestClient) -> None:
    """The customer is standing there holding it."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")

    tender = till.post(
        f"/register/carts/{cart_id}/payments", json={"method": "cash"}
    ).json()

    assert tender["expires_at"] is None
    assert tender["state"] == "approved"


# ── Attestation ─────────────────────────────────────────────────────────────


def test_attesting_settles_the_sale(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")  # ₹37.40
    tender = begin_upi(till, cart_id)

    settled = attest(till, tender["attempt_id"], reference="UTR123456789")

    assert settled["state"] == "approved"
    assert settled["cart"]["settled"] is True
    assert settled["cart"]["outstanding"]["paise"] == 0


def test_a_upi_only_sale_carries_no_rounding(till: TestClient) -> None:
    """Exit criterion. A transfer has no coins, so ₹37.40 means ₹37.40."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    attest(till, tender["attempt_id"])

    sale = till.post(f"/register/carts/{cart_id}/post").json()

    assert sale["rounding_adjustment"]["paise"] == 0
    assert sale["grand_total"]["paise"] == 3740
    assert "Rounding" not in sale["receipt_text"]


def test_the_utr_reaches_the_receipt(till: TestClient) -> None:
    """With a static QR the UTR is the only reference the statement shares
    with this sale, so losing it would leave nothing to reconcile against."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    attest(till, tender["attempt_id"], reference="UTR987654321")

    sale = till.post(f"/register/carts/{cart_id}/post").json()

    assert "UTR987654321" in sale["receipt_text"]


def test_an_attested_payment_is_not_a_verified_one(
    till: TestClient, db: Database
) -> None:
    """Exit criterion, and the whole point of `verified`.

    A cashier's word and a settled transfer must never look the same in the
    data, or a shift close counts money the shop does not have.
    """
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    attest(till, tender["attempt_id"], reference="UTR1")
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    row = db.query_one(
        "SELECT method, verified, confirmation_method, tender_ref FROM payments "
        " WHERE sale_id = ?",
        (sale["sale_id"],),
    )

    assert row["method"] == "upi"
    assert row["verified"] == 0
    assert row["confirmation_method"] == "manual_attestation"
    assert row["tender_ref"] == "UTR1"


def test_cash_is_verified_because_the_drawer_saw_it(
    till: TestClient, db: Database
) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    row = db.query_one(
        "SELECT verified, confirmation_method FROM payments WHERE sale_id = ?",
        (sale["sale_id"],),
    )

    assert row["verified"] == 1
    assert row["confirmation_method"] == "immediate"


# ── The customer types the amount, so it can be wrong ───────────────────────


def test_a_short_payment_leaves_a_balance(till: TestClient) -> None:
    """Exit criterion. ₹37.40 asked, ₹30 arrived — the loop simply continues."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)

    short = attest(till, tender["attempt_id"], amount_paise=3000)

    assert short["cart"]["settled"] is False
    assert short["cart"]["paid"]["paise"] == 3000
    assert short["cart"]["outstanding"]["paise"] == 740


def test_the_rest_of_a_short_payment_is_taken_in_cash(till: TestClient) -> None:
    """Exit criterion: settled in cash *without leaving the sale*.

    And the rounding lands on the ₹7.40 remainder, not on the ₹37.40 basket —
    only the cash portion rounds (architecture §13.4).
    """
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    attest(till, tender["attempt_id"], amount_paise=3000)

    rest = till.post(
        f"/register/carts/{cart_id}/payments", json={"method": "cash"}
    ).json()
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    assert rest["cart"]["settled"] is True
    # ₹7.40 rounds to ₹7.00: a 40-paise adjustment on the cash remainder.
    assert sale["rounding_adjustment"]["paise"] == -40
    assert sale["grand_total"]["paise"] == 3700


def test_an_overpayment_becomes_change_in_cash(till: TestClient) -> None:
    """₹50 typed against a ₹37.40 balance. The till cannot refund a transfer,
    so the difference goes back across the counter as cash."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)

    over = attest(till, tender["attempt_id"], amount_paise=5000)
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    assert over["change_due"]["paise"] == 1260
    assert sale["change_due"]["paise"] == 1260
    assert over["cart"]["settled"] is True


def test_a_zero_attestation_is_refused(till: TestClient) -> None:
    """"They paid nothing" is not an attestation; it is a cancellation."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)

    response = till.post(
        f"/register/payments/{tender['attempt_id']}/confirm",
        json={"amount_paise": 0},
    )

    assert response.status_code == 400
    assert "positive" in response.json()["detail"]


# ── All three tender combinations ───────────────────────────────────────────


def test_cash_only(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    assert sale["grand_total"]["paise"] == 3700  # rounded down from ₹37.40
    assert sale["rounding_adjustment"]["paise"] == -40


def test_upi_only(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    attest(till, tender["attempt_id"])
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    assert sale["grand_total"]["paise"] == 3740  # exact — no coins involved
    assert sale["rounding_adjustment"]["paise"] == 0


def test_cash_then_upi(till: TestClient, db: Database) -> None:
    """Cash first rounds the whole balance; UPI then covers what is left.

    The cash portion was rounded when cash was chosen, which is the moment
    the coins were actually a problem.
    """
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")  # ₹37.40 → ₹37.00 in cash
    till.post(
        f"/register/carts/{cart_id}/payments",
        json={"method": "cash", "tendered_paise": 2000},
    )
    # Cash was for the full rounded balance, so nothing should remain.
    cart = till.get(f"/register/carts/{cart_id}").json()
    assert cart["settled"] is True

    sale = till.post(f"/register/carts/{cart_id}/post").json()
    rows = db.query(
        "SELECT method FROM payments WHERE sale_id = ?", (sale["sale_id"],)
    )
    methods = {row["method"] for row in rows}
    assert methods == {"cash"}


def test_split_upi_then_cash_records_both_payments(
    till: TestClient, db: Database
) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    attest(till, tender["attempt_id"], amount_paise=2000, reference="UTR-SPLIT")
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    rows = db.query(
        "SELECT method, amount, verified FROM payments WHERE sale_id = ? "
        " ORDER BY method",
        (sale["sale_id"],),
    )

    assert [(r["method"], r["amount"]) for r in rows] == [
        ("cash", 1700),  # ₹17.40 remaining, rounded to ₹17
        ("upi", 2000),
    ]
    assert [r["verified"] for r in rows] == [1, 0]
    assert sale["rounding_adjustment"]["paise"] == -40


def test_both_payments_appear_on_the_receipt(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    attest(till, tender["attempt_id"], amount_paise=2000)
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    text = sale["receipt_text"].lower()
    assert "upi" in text
    assert "cash" in text


# ── Expiry and cancellation ─────────────────────────────────────────────────


def test_an_expired_attempt_releases_the_cart(till: TestClient) -> None:
    """Exit criterion. The customer wandered off; the basket is still sellable.

    Time is moved by rewriting the attempt's expiry rather than by sleeping
    five minutes, which is the only part of this a test can reasonably fake.
    """
    from app.domain.payments import AttemptState

    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)

    carts = till.app.state.cart_service  # type: ignore[attr-defined]
    open_cart_state = carts.get(cart_id)
    open_cart_state.attempts = [
        attempt.__class__(
            **{
                **{f: getattr(attempt, f) for f in attempt.__slots__},
                "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
            }
        )
        for attempt in open_cart_state.attempts
    ]

    state = till.get(f"/register/payments/{tender['attempt_id']}").json()
    assert state["state"] == AttemptState.EXPIRED.value

    # The cart is open again: a line can be added and the sale taken in cash.
    add_barcode(till, cart_id, "8901262010016")
    cart = till.get(f"/register/carts/{cart_id}").json()
    assert cart["locked"] is False
    assert cart["item_count"] == 2


def test_a_cancelled_attempt_unfreezes_the_basket(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)

    # While pending, the basket is frozen: the customer is typing that figure
    # into their own app right now.
    blocked = till.post(
        f"/register/carts/{cart_id}/lines", json={"barcode": "8901262010016"}
    )
    assert blocked.status_code == 409

    cancelled = till.post(
        f"/register/payments/{tender['attempt_id']}/cancel"
    ).json()
    assert cancelled["state"] == "cancelled"
    assert cancelled["cart"]["locked"] is False

    add_barcode(till, cart_id, "8901262010016")


def test_an_attested_payment_cannot_be_cancelled(till: TestClient) -> None:
    """Money that arrived is refunded, not un-taken (architecture §1.4)."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    attest(till, tender["attempt_id"])

    response = till.post(f"/register/payments/{tender['attempt_id']}/cancel")

    assert response.status_code == 400
    assert "refund" in response.json()["detail"]


def test_an_unknown_attempt_id_is_a_404(till: TestClient) -> None:
    assert till.get(f"/register/payments/{new_id()}").status_code == 404
    assert till.post(f"/register/payments/{new_id()}/confirm", json={}).status_code == 404


# ── "I can't tell" → requires_review ────────────────────────────────────────


def test_an_unconfirmable_payment_posts_for_review(
    till: TestClient, db: Database
) -> None:
    """The customer insists they paid. Refusing accuses them; attesting
    invents money. So the sale posts, marked (architecture §13.5)."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)

    unsure = till.post(
        f"/register/payments/{tender['attempt_id']}/unknown",
        json={"reason": "customer showed a screenshot, no soundbox alert"},
    ).json()

    assert unsure["state"] == "unknown"
    # Nothing was collected, so the balance is still outstanding — the sale
    # cannot post on the strength of a payment nobody can see.
    assert unsure["cart"]["settled"] is False

    # The cashier settles it the only honest way left: taking the cash.
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    assert sale["status"] == "requires_review"
    row = db.query_one("SELECT status FROM sales WHERE id = ?", (sale["sale_id"],))
    assert row["status"] == "requires_review"


def test_the_review_shows_on_the_receipt(till: TestClient) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    till.post(f"/register/payments/{tender['attempt_id']}/unknown", json={})
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    assert "review" in sale["receipt_text"].lower()


# ── The review queue ────────────────────────────────────────────────────────


@pytest.fixture
def reviewed_sale(till: TestClient) -> str:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    tender = begin_upi(till, cart_id)
    till.post(f"/register/payments/{tender['attempt_id']}/unknown", json={})
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    return str(till.post(f"/register/carts/{cart_id}/post").json()["sale_id"])


def test_a_cashier_cannot_see_the_review_queue(
    till: TestClient, reviewed_sale: str
) -> None:
    """It is a supervisor's worklist. A cashier watching their own disputed
    payment sit in a queue helps nobody."""
    assert till.get("/register/reviews").status_code == 403


def test_a_cashier_cannot_resolve_a_review(
    till: TestClient, reviewed_sale: str
) -> None:
    """Explicitly not the cashier's call (architecture §13.5)."""
    response = till.post(
        f"/register/sales/{reviewed_sale}/resolve-review", json={"outcome": "paid"}
    )
    assert response.status_code == 403


def test_a_manager_sees_and_settles_the_queue(
    till: TestClient, reviewed_sale: str, seeded_manager: dict, db: Database
) -> None:
    till.post("/auth/login", json=seeded_manager)

    queue = till.get("/register/reviews").json()
    assert [item["sale_id"] for item in queue["items"]] == [reviewed_sale]
    assert queue["items"][0]["disputed_amount"]["paise"] == 3740

    resolved = till.post(
        f"/register/sales/{reviewed_sale}/resolve-review",
        json={"outcome": "paid", "note": "found on the statement next morning"},
    )
    assert resolved.status_code == 200

    # The sale still says requires_review — that is what happened, and it is
    # never rewritten. The resolution is its own row (migration 003).
    sale = db.query_one("SELECT status FROM sales WHERE id = ?", (reviewed_sale,))
    assert sale["status"] == "requires_review"

    review = db.query_one(
        "SELECT outcome, note, resolved_by FROM sale_reviews WHERE sale_id = ?",
        (reviewed_sale,),
    )
    assert review["outcome"] == "paid"
    assert "statement" in review["note"]

    assert till.get("/register/reviews").json()["items"] == []


def test_resolving_is_audited(
    till: TestClient, reviewed_sale: str, seeded_manager: dict, db: Database
) -> None:
    """A decision about money that leaves no trace of who made it is worse
    than no decision."""
    till.post("/auth/login", json=seeded_manager)
    till.post(
        f"/register/sales/{reviewed_sale}/resolve-review",
        json={"outcome": "not_paid"},
    )

    row = db.query_one(
        "SELECT actor_id, after_json FROM audit_log "
        " WHERE action = 'sale.review.resolve' AND entity_id = ?",
        (reviewed_sale,),
    )
    assert row is not None
    assert "not_paid" in row["after_json"]


def test_a_review_cannot_be_settled_twice(
    till: TestClient, reviewed_sale: str, seeded_manager: dict
) -> None:
    """A second supervisor changing their mind writes nothing; disagreement is
    a conversation, not a row."""
    till.post("/auth/login", json=seeded_manager)
    first = till.post(
        f"/register/sales/{reviewed_sale}/resolve-review", json={"outcome": "paid"}
    )
    second = till.post(
        f"/register/sales/{reviewed_sale}/resolve-review", json={"outcome": "not_paid"}
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_an_outcome_must_say_what_happened(
    till: TestClient, reviewed_sale: str, seeded_manager: dict
) -> None:
    """A variance nobody can name is a variance nobody can act on."""
    till.post("/auth/login", json=seeded_manager)
    response = till.post(
        f"/register/sales/{reviewed_sale}/resolve-review", json={"outcome": "resolved"}
    )
    assert response.status_code == 422


def test_a_settled_sale_is_not_reviewable(
    till: TestClient, seeded_manager: dict
) -> None:
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901030608278")
    till.post(f"/register/carts/{cart_id}/payments", json={"method": "cash"})
    sale = till.post(f"/register/carts/{cart_id}/post").json()

    till.post("/auth/login", json=seeded_manager)
    response = till.post(
        f"/register/sales/{sale['sale_id']}/resolve-review", json={"outcome": "paid"}
    )

    assert response.status_code == 409

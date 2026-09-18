"""Money off one line — the first act a cashier can only do by being lent it.

Everything else on the register screen is either ordinary cashier work or
hidden behind a role nobody at a till has. This is the first endpoint gated on
a key from `OVERRIDABLE`, which makes it the first place the whole override
flow can be exercised the way it will actually be used: refused, authorised,
performed, and refused again ninety seconds later.

`CartService.apply_discount` has existed since the pricing work with no caller.
That is not an oversight anybody should fix quietly — discounting needed a
permission no role on this screen holds, so a route would have been unreachable
for every person who uses it. The override is what makes it reachable.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.domain import permissions as perms
from app.domain.identity import OVERRIDE_GRANT_TTL, utcnow
from tests.conftest import TEST_STORE_ID, add_barcode, open_cart

SUPERVISOR = {
    "user_id": "018f0000-0000-7000-8000-000000000002",
    "employee_code": "S001",
    "pin": "7241",
}


@pytest.fixture
def basket(till) -> str:
    """A cashier with one line in front of them."""
    cart_id = open_cart(till)
    add_barcode(till, cart_id, "8901262010016")
    return cart_id


def _seed_supervisor(auth_service) -> None:
    auth_service.seed_local_user(
        user_id=SUPERVISOR["user_id"],
        employee_code=SUPERVISOR["employee_code"],
        full_name="Ravi Menon",
        store_id=TEST_STORE_ID,
        pin=SUPERVISOR["pin"],
        roles=frozenset({perms.SUPERVISOR}),
        permissions=perms.permissions_for(frozenset({perms.SUPERVISOR})),
    )


def _authorise(client, permission=perms.SALE_DISCOUNT_LINE):
    response = client.post(
        "/overrides/authorize",
        json={
            "approver_code": SUPERVISOR["employee_code"],
            "pin": SUPERVISOR["pin"],
            "permission": permission,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _discount(client, cart_id, amount_paise=500, **body):
    return client.post(
        f"/register/carts/{cart_id}/lines/1/discount",
        json={"amount_paise": amount_paise, **body},
    )


# ── Refused, authorised, performed ──────────────────────────────────────────


def test_a_cashier_cannot_discount_a_line(till, basket) -> None:
    """The reason the modal exists at all."""
    response = _discount(till, basket)

    assert response.status_code == 403
    assert response.json()["detail"] == "permission_denied"


def test_a_supervisor_lends_the_key_and_the_discount_goes_through(
    till, basket, auth_service
) -> None:
    """Slice 3's acceptance criterion, end to end and offline.

    A cashier is refused, a supervisor authorises at the same till without
    signing the cashier out, and the act the cashier could not do a moment ago
    now succeeds.
    """
    _seed_supervisor(auth_service)
    assert _discount(till, basket).status_code == 403

    granted = till.post(
        "/overrides/authorize",
        json={
            "approver_code": SUPERVISOR["employee_code"],
            "pin": SUPERVISOR["pin"],
            "permission": perms.SALE_DISCOUNT_LINE,
        },
    )
    assert granted.status_code == 200, granted.text

    response = _discount(till, basket)

    assert response.status_code == 200, response.text
    cart = response.json()
    assert cart["discount_total"]["paise"] == 500
    assert till.get("/auth/session").json()["employee_code"] == "C001"


def test_the_grant_expires_and_the_next_discount_is_refused(
    till, basket, auth_service, client
) -> None:
    """Ninety seconds, and the ninety-first is a refusal.

    The window is moved rather than waited out: the grant is rewritten to have
    expired, which is what the clock does anyway. What matters is that
    `require()` consults it on every request rather than once.
    """
    _seed_supervisor(auth_service)
    till.post(
        "/overrides/authorize",
        json={
            "approver_code": SUPERVISOR["employee_code"],
            "pin": SUPERVISOR["pin"],
            "permission": perms.SALE_DISCOUNT_LINE,
        },
    )
    assert _discount(till, basket).status_code == 200

    sessions = client.app.state.sessions
    expired = utcnow() - timedelta(seconds=1)
    sessions.set(
        replace(sessions.current, overrides={perms.SALE_DISCOUNT_LINE: expired}),
        access_token=None,
    )

    assert _discount(till, basket).status_code == 403


def test_one_grant_covers_every_discount_inside_its_window(
    till, basket, auth_service
) -> None:
    """Time-boxed, not use-boxed — asserted at the layer a cashier meets it.

    The architecture's choice, and defensible at a counter: the alternative is
    a supervisor standing there for a three-line correction. It is worth an
    assertion because a later reader might "fix" it into a single-use token,
    which would be a different product decision.
    """
    _seed_supervisor(auth_service)
    till.post(
        "/overrides/authorize",
        json={
            "approver_code": SUPERVISOR["employee_code"],
            "pin": SUPERVISOR["pin"],
            "permission": perms.SALE_DISCOUNT_LINE,
        },
    )

    first = _discount(till, basket, amount_paise=100)
    second = _discount(till, basket, amount_paise=100)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["discount_total"]["paise"] == 200


def test_the_window_is_the_ttl_the_domain_says(till, basket, auth_service) -> None:
    """The endpoint reports the same window the session was given.

    Two numbers that must agree: what `expires_in_seconds` tells the modal to
    count down, and how long `require()` will actually keep admitting the
    request.
    """
    _seed_supervisor(auth_service)
    granted = till.post(
        "/overrides/authorize",
        json={
            "approver_code": SUPERVISOR["employee_code"],
            "pin": SUPERVISOR["pin"],
            "permission": perms.SALE_DISCOUNT_LINE,
        },
    ).json()

    assert granted["expires_in_seconds"] <= OVERRIDE_GRANT_TTL.total_seconds()
    assert granted["expires_in_seconds"] > OVERRIDE_GRANT_TTL.total_seconds() - 5


# ── What the endpoint refuses on its own account ────────────────────────────


def test_a_discount_larger_than_the_line_is_refused(
    till, basket, auth_service
) -> None:
    """Above the line total there is no reading of the number anybody meant.

    The message names the line total rather than the amount typed, because the
    number the cashier is missing is the one they did not type.
    """
    _seed_supervisor(auth_service)
    _authorise(till)

    line = till.get(f"/register/carts/{basket}").json()["lines"][0]
    response = _discount(till, basket, amount_paise=99_999_99)

    assert response.status_code == 422
    detail = response.json()["detail"]
    # The figure the cashier needs is the line total, not the one they typed.
    assert line["line_total"]["text"] in detail, detail


def test_a_discount_equal_to_the_line_is_allowed(till, basket, auth_service) -> None:
    """Free has to be expressible, and this is the path that records it.

    Giving an item away is real in a kirana shop — damaged stock, a goodwill
    gesture, a regular who is short. Refusing it here would not stop the act.
    It would move it to deleting the line, which is `sale.create` work needing
    no supervisor and leaving no audit row, trading a recorded act by two
    people for an unrecorded one by one.
    """
    _seed_supervisor(auth_service)
    _authorise(till)

    line_total = till.get(f"/register/carts/{basket}").json()["lines"][0]
    response = _discount(till, basket, amount_paise=line_total["line_total"]["paise"])

    assert response.status_code == 200, response.text
    cart = response.json()
    assert cart["total_before_rounding"]["paise"] == 0
    assert cart["discount_total"]["paise"] == line_total["line_total"]["paise"]


def test_the_clamp_is_no_longer_reachable_through_the_api(
    till, basket, auth_service
) -> None:
    """`price_line` still caps a discount at what is left to discount.

    That cap is domain defence-in-depth and stays, because the domain does not
    get to assume its callers checked. But nothing arriving through the API can
    reach it any more: every fixed discount the service accepts is at most the
    line total, so the clamp has nothing left to clamp.

    Asserted rather than assumed, because "unreachable" is exactly the sort of
    claim that rots — and because a later change that widened the service check
    would otherwise be caught by nothing at all.
    """
    _seed_supervisor(auth_service)
    _authorise(till)

    line = till.get(f"/register/carts/{basket}").json()["lines"][0]
    exact = line["line_total"]["paise"]

    assert _discount(till, basket, amount_paise=exact).status_code == 200
    assert _discount(till, basket, amount_paise=1).status_code == 422, (
        "the line is already free; anything more would need the clamp"
    )


def test_a_discount_of_nothing_is_refused(till, basket, auth_service) -> None:
    """Zero is not a discount, and a negative one is a price increase."""
    _seed_supervisor(auth_service)
    till.post(
        "/overrides/authorize",
        json={
            "approver_code": SUPERVISOR["employee_code"],
            "pin": SUPERVISOR["pin"],
            "permission": perms.SALE_DISCOUNT_LINE,
        },
    )

    assert _discount(till, basket, amount_paise=0).status_code == 422
    assert _discount(till, basket, amount_paise=-500).status_code == 422


def test_a_line_that_is_not_there_is_a_404(till, basket, auth_service) -> None:
    _seed_supervisor(auth_service)
    till.post(
        "/overrides/authorize",
        json={
            "approver_code": SUPERVISOR["employee_code"],
            "pin": SUPERVISOR["pin"],
            "permission": perms.SALE_DISCOUNT_LINE,
        },
    )

    response = till.post(
        f"/register/carts/{basket}/lines/99/discount",
        json={"amount_paise": 100},
    )

    assert response.status_code == 404

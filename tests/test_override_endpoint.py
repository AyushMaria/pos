"""`POST /overrides/authorize` — the one route that hands out a permission.

Everything else in `app/api` asks whether the session may do something. This
one is how a session that may not comes to may, and only for ninety seconds.
Two things make it worth its own file:

* it carries no `require()`, which is a decision recorded in
  `tests/test_permission_matrix.py`'s `UNGATED` — a gate here would be
  circular, because the cashier who needs an override is by definition the one
  without the key;
* it is the second route in the application that takes a PIN, which is why
  `SENSITIVE_FIELDS` redaction exists at all. One route with that exposure is
  a thing to fix; two is a pattern.

The refusals are distinguishable on purpose. A supervisor at a till needs to
know whether to retype, to wait, to fetch somebody else or to plug the cable
back in, and a single "denied" for all four is a screen that helps nobody.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.domain import permissions as perms
from app.domain.identity import OVERRIDE_GRANT_TTL, utcnow
from app.domain.lockout import FAILURES_BEFORE_LOCK
from tests.conftest import ROLE_USERS, TEST_STORE_ID


@pytest.fixture
def till(sign_in_as) -> TestClient:
    """A cashier signed in at the terminal, which is the normal state."""
    return sign_in_as(perms.CASHIER)


def _seed(auth_service, role: str) -> dict[str, str]:
    """Seed a second person without signing them in."""
    user_id, employee_code, pin = ROLE_USERS[role]
    auth_service.seed_local_user(
        user_id=user_id,
        employee_code=employee_code,
        full_name=f"{role.title()} Testperson",
        store_id=TEST_STORE_ID,
        pin=pin,
        roles=frozenset({role}),
        permissions=perms.permissions_for(frozenset({role})),
    )
    return {"approver_code": employee_code, "pin": pin}


def _body(approver: dict[str, str], permission: str = perms.SALE_VOID) -> dict:
    return {**approver, "permission": permission}


# ── The happy path ──────────────────────────────────────────────────────────


def test_a_supervisor_lends_the_cashier_a_permission(till, auth_service) -> None:
    approver = _seed(auth_service, perms.SUPERVISOR)

    response = till.post("/overrides/authorize", json=_body(approver))

    assert response.status_code == 200, response.text
    grant = response.json()
    assert grant["permission"] == perms.SALE_VOID
    assert grant["approver_code"] == "S001"
    assert grant["actor_code"] == "C001", "the grant names the cashier receiving it"
    assert 0 < grant["expires_in_seconds"] <= OVERRIDE_GRANT_TTL.total_seconds()


def test_the_cashier_is_still_the_one_signed_in(till, auth_service) -> None:
    """The whole difference between an override and a login.

    `_login_offline` verifies somebody and becomes them. This verifies somebody
    and walks away: the session in the store is still the cashier's, their cart
    is untouched, and the supervisor goes back to the floor (§11.3).
    """
    approver = _seed(auth_service, perms.SUPERVISOR)

    till.post("/overrides/authorize", json=_body(approver))

    session = till.get("/auth/session").json()
    assert session["employee_code"] == "C001"
    assert perms.SALE_VOID not in session["permissions"], (
        "the grant has leaked into the session's own permissions, which would "
        "make it survive its ninety seconds"
    )


def test_the_grant_actually_admits_the_act_it_was_minted_for(
    till, client, auth_service
) -> None:
    """A response shaped like success is not the same as a permission.

    No route is gated on an overridable key yet — all four are in
    `NO_API_SURFACE` until the void screen exists — so this asserts against
    what `require()` itself consults, which is `Session.allows`.
    """
    approver = _seed(auth_service, perms.SUPERVISOR)
    sessions = client.app.state.sessions
    now = utcnow()
    assert not sessions.current.allows(perms.SALE_VOID, now=now)

    assert till.post("/overrides/authorize", json=_body(approver)).status_code == 200

    assert sessions.current.allows(perms.SALE_VOID, now=now)
    assert not sessions.current.allows(
        perms.SALE_VOID, now=now + OVERRIDE_GRANT_TTL + timedelta(seconds=1)
    ), "the grant outlived its window"


# ── The refusals, each of which is a different sentence on screen ───────────


def test_nobody_signed_in_is_a_401(client, auth_service) -> None:
    """A grant is lent *to* somebody, so there has to be a somebody.

    Ungated means no permission is required. It has never meant no session is
    required — the same gap `test_sync_push_is_deliberately_ungated` closes for
    the other ungated route.
    """
    _seed(auth_service, perms.SUPERVISOR)

    response = client.post(
        "/overrides/authorize", json=_body({"approver_code": "S001", "pin": "7241"})
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "not_signed_in"

    # And refused *first*. The service raises its own `NotSignedIn`, which the
    # router also answers with 401 — so the dependency looks redundant until
    # the request is one the service would reject earlier for another reason.
    # Here it would be 400 "that key cannot be lent", which tells somebody with
    # no session which keys exist.
    nonsense = client.post(
        "/overrides/authorize",
        json=_body({"approver_code": "S001", "pin": "7241"}, perms.SALE_REFUND),
    )
    assert nonsense.status_code == 401, (
        "an unauthenticated caller was told something about the permission "
        "they asked for before being told they are not signed in"
    )


def test_a_wrong_pin_is_a_401(till, auth_service) -> None:
    _seed(auth_service, perms.SUPERVISOR)

    response = till.post(
        "/overrides/authorize",
        json=_body({"approver_code": "S001", "pin": "0000"}),
    )

    assert response.status_code == 401
    assert "0000" not in response.text, "the submitted PIN came back in the refusal"


def test_authorising_yourself_is_a_403(till) -> None:
    """The control this endpoint exists to impose.

    Without it, a cashier who knows their own PIN approves their own void and
    the audit row names the same person twice while looking entirely ordinary.
    """
    response = till.post(
        "/overrides/authorize",
        json=_body({"approver_code": "C001", "pin": "4913"}),
    )

    assert response.status_code == 403
    assert "somebody else" in response.json()["detail"]


def test_an_approver_who_lacks_the_key_is_a_403(till, auth_service) -> None:
    """Nobody can lend what they do not have.

    Without this, a second cashier authorises the first one's void — which is
    two people agreeing, and no more authority than one.
    """
    approver = _seed(auth_service, perms.INVENTORY)

    response = till.post("/overrides/authorize", json=_body(approver))

    assert response.status_code == 403
    assert "I001" in response.json()["detail"]


def test_a_permission_that_is_not_lendable_is_a_400(till, auth_service) -> None:
    """`sale.refund` is not in `OVERRIDABLE`, and that is slice 3's decision.

    Refused here rather than at the write: RLS would refuse it hours later,
    into the failures queue, where the person who authorised it will never
    look.
    """
    approver = _seed(auth_service, perms.MANAGER)

    response = till.post("/overrides/authorize", json=_body(approver, perms.SALE_REFUND))

    assert response.status_code == 400
    assert perms.SALE_REFUND in response.json()["detail"]


def test_a_key_the_session_already_holds_is_a_409(sign_in_as, auth_service) -> None:
    """A modal that appears to do something while doing nothing is worse than
    a refusal, and the audit row would describe an escalation that did not
    happen.

    Reachable only with a supervisor at the register, because a cashier holds
    none of `OVERRIDABLE` — which is what makes them a cashier. That is not a
    contrived setup: one supervisor covering a till at lunchtime is the most
    ordinary way this endpoint gets asked for something already held, and
    without the check it would mint a grant and file an audit row saying an
    escalation happened.
    """
    client = sign_in_as(perms.SUPERVISOR)
    approver = _seed(auth_service, perms.MANAGER)

    response = client.post("/overrides/authorize", json=_body(approver))

    assert response.status_code == 409
    assert perms.SALE_VOID in response.json()["detail"]


def test_an_approver_this_terminal_has_never_seen_is_a_503(till) -> None:
    """Not a rejection — a different problem with a different next action.

    Offline, an approver has to have signed in on this till before, because
    their PIN hash is what is being checked. "Wrong PIN" would send a
    supervisor typing the right one over and over.
    """
    response = till.post(
        "/overrides/authorize",
        json=_body({"approver_code": "Z999", "pin": "1234"}),
    )

    assert response.status_code == 503
    assert "Z999" in response.json()["detail"]


def test_a_locked_approver_is_a_423_that_says_when(till, auth_service) -> None:
    """Waiting is the only thing that will help, so the screen has to say so.

    423 rather than 429: this is a fact about the account, not about the
    caller. The supervisor who has typed a PIN twice is not the person who
    spent the afternoon guessing at it.
    """
    approver = _seed(auth_service, perms.SUPERVISOR)
    for _ in range(FAILURES_BEFORE_LOCK):
        till.post("/overrides/authorize", json=_body({**approver, "pin": "0000"}))

    response = till.post("/overrides/authorize", json=_body(approver))

    assert response.status_code == 423
    assert "S001" in response.json()["detail"]
    assert str(utcnow().year) in response.json()["detail"], "no unlock time named"


# ── The PIN, on the way out ─────────────────────────────────────────────────


def test_a_short_pin_does_not_come_back_in_the_422(till) -> None:
    """The reason `SENSITIVE_FIELDS` exists — this is the second PIN route."""
    response = till.post(
        "/overrides/authorize",
        json={"approver_code": "S001", "pin": "12", "permission": perms.SALE_VOID},
    )

    assert response.status_code == 422
    assert "12" not in response.text
    assert any(
        error["loc"] == ["body", "pin"] for error in response.json()["detail"]
    ), "the 422 no longer says which field was wrong"


def test_the_redaction_is_narrow(till) -> None:
    """The positive control.

    A handler that dropped every `input` would pass the test above while
    making every other 422 useless. This one asserts that a harmless field is
    still echoed, so "nothing comes back" cannot masquerade as "the PIN does
    not come back".
    """
    response = till.post(
        "/overrides/authorize",
        json={"approver_code": "", "pin": "7241", "permission": perms.SALE_VOID},
    )

    assert response.status_code == 422
    assert "7241" not in response.text
    assert any(
        error.get("input") is not None for error in response.json()["detail"]
    ), "no input survived at all — the redaction is eating everything"

"""Supervisor grants — phase 7 slice 3, step 1.

`Session.overrides` and `Session.allows()` have existed since phase 1 and
nothing has ever written to them: in production the dict is always empty, so
`allows()` has been an elaborate way of saying `in self.permissions`. This is
the first thing that fills it.

What is being tested is mostly refusals. A grant that appears to work and does
not is the failure shape this project keeps producing, and a grant is unusually
good at it — the window is ninety seconds, nobody watches it expire, and the
consequence lands at the sync boundary hours later if it lands at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.data.repositories.audit import OVERRIDE_GRANTED
from app.domain import permissions as perms
from app.domain.identity import (
    OVERRIDE_GRANT_TTL,
    NotOverridable,
    Session,
    utcnow,
)
from app.services.auth_service import (
    ApproverLacksPermission,
    CannotAuthoriseSelf,
    LoginFailed,
    NoOfflineIdentity,
    NothingToAuthorise,
    NotSignedIn,
    SessionStore,
)
from tests.conftest import TEST_STORE_ID

NOW = datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc)


def cashier(**overrides: object) -> Session:
    return Session(
        user_id="018f0000-0000-7000-8000-000000000001",
        employee_code="C001",
        full_name="Anita Rao",
        store_id="018f0000-0000-7000-8000-000000000100",
        roles=frozenset({perms.CASHIER}),
        permissions=perms.permissions_for(frozenset({perms.CASHIER})),
        authenticated_at=NOW,
        **overrides,  # type: ignore[arg-type]
    )


def store_with_cashier() -> SessionStore:
    store = SessionStore()
    store.set(cashier(), access_token=None)
    return store


# ── The grant itself ────────────────────────────────────────────────────────


def test_a_grant_lets_the_cashier_do_the_thing() -> None:
    store = store_with_cashier()
    assert not store.current.allows(perms.SALE_VOID, now=NOW)  # type: ignore[union-attr]

    store.grant(perms.SALE_VOID, until=NOW + OVERRIDE_GRANT_TTL, now=NOW)

    assert store.current.allows(perms.SALE_VOID, now=NOW)  # type: ignore[union-attr]


def test_the_cashier_is_still_signed_in_afterwards() -> None:
    """The whole point, and the one thing the docstring already promised.

    A supervisor authorising a void must not sign the cashier out — their cart
    is open and a customer is standing there. The session is replaced in the
    store because it is frozen, so this asserts the identity survived the
    replacement rather than that the object did.
    """
    store = store_with_cashier()
    before = store.current

    store.grant(perms.SALE_VOID, until=NOW + OVERRIDE_GRANT_TTL, now=NOW)
    after = store.current

    assert after is not before, "frozen dataclass: expected a replacement"
    assert after.user_id == before.user_id  # type: ignore[union-attr]
    assert after.employee_code == before.employee_code  # type: ignore[union-attr]
    assert after.authenticated_at == before.authenticated_at  # type: ignore[union-attr]
    assert after.permissions == before.permissions  # type: ignore[union-attr]
    assert after.roles == before.roles  # type: ignore[union-attr]


def test_the_grant_stops_at_the_end_of_its_window() -> None:
    """Ninety seconds, and the ninety-first is a refusal.

    Asserted at the boundary rather than a second either side, because
    `allows()` is `now < granted_until` and an off-by-one there is a grant
    that outlives its window by exactly the time nobody checks.
    """
    store = store_with_cashier()
    until = NOW + OVERRIDE_GRANT_TTL
    store.grant(perms.SALE_VOID, until=until, now=NOW)
    session = store.current

    assert session.allows(perms.SALE_VOID, now=until - timedelta(seconds=1))  # type: ignore[union-attr]
    assert not session.allows(perms.SALE_VOID, now=until)  # type: ignore[union-attr]
    assert not session.allows(perms.SALE_VOID, now=until + timedelta(seconds=1))  # type: ignore[union-attr]


def test_a_grant_lends_one_key_and_not_the_others() -> None:
    """A void is not a discount, and neither is a licence for the rest."""
    store = store_with_cashier()
    store.grant(perms.SALE_VOID, until=NOW + OVERRIDE_GRANT_TTL, now=NOW)
    session = store.current

    for other in sorted(perms.OVERRIDABLE - {perms.SALE_VOID}):
        assert not session.allows(other, now=NOW), other  # type: ignore[union-attr]


def test_it_is_a_window_not_a_token() -> None:
    """Time-boxed, not use-boxed — the architecture's choice, asserted.

    One authorisation covers every void until it expires. This is worth a test
    because it is the kind of thing a later reader might "fix" into a
    single-use token, which would be a different product decision and should
    not happen by accident.
    """
    store = store_with_cashier()
    store.grant(perms.SALE_VOID, until=NOW + OVERRIDE_GRANT_TTL, now=NOW)
    session = store.current

    for moment in (NOW, NOW + timedelta(seconds=30), NOW + timedelta(seconds=89)):
        assert session.allows(perms.SALE_VOID, now=moment), moment  # type: ignore[union-attr]


# ── What it refuses ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "permission",
    sorted(perms.ALL_PERMISSIONS - perms.OVERRIDABLE),
)
def test_a_permission_outside_the_overridable_set_is_refused(permission: str) -> None:
    """The rule from slice 3's decision, enforced where grants are minted.

    `cash.payout` is the one that matters: RLS names the key, so a granted
    payout would be accepted here, pass `require()`, complete the movement at
    the counter, and be refused at push time into the failures queue. Refusing
    at the modal is the difference between a supervisor saying no and a row
    nobody finds until the till does not balance.
    """
    store = store_with_cashier()
    with pytest.raises(NotOverridable) as refused:
        store.grant(permission, until=NOW + OVERRIDE_GRANT_TTL, now=NOW)

    assert refused.value.permission == permission
    # Asserted on `overrides` rather than on `allows()`, because three of
    # these keys are ones the cashier already holds — `sale.create`,
    # `product.read`, `payment.attest`. For those, `allows()` is true for
    # reasons that have nothing to do with the grant, and asserting on it
    # would be testing the role table rather than the refusal.
    assert permission not in store.current.overrides  # type: ignore[union-attr]


def test_an_unknown_permission_is_refused() -> None:
    store = store_with_cashier()
    with pytest.raises(NotOverridable):
        store.grant("sale.teleport", until=NOW + OVERRIDE_GRANT_TTL, now=NOW)


def test_a_grant_with_nobody_signed_in_is_refused() -> None:
    """Not a silent no-op: a supervisor typed their PIN for nothing."""
    store = SessionStore()
    with pytest.raises(NotSignedIn):
        store.grant(perms.SALE_VOID, until=NOW + OVERRIDE_GRANT_TTL, now=NOW)


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=-1)])
def test_a_window_that_has_already_closed_is_refused(offset: timedelta) -> None:
    """A grant that expires now grants nothing, and must not read as success.

    The likely cause is a clock problem or an inverted sign upstream, and both
    of those are worth a stack trace rather than a cashier finding out that
    the authorisation they watched a supervisor give did not take.
    """
    store = store_with_cashier()
    with pytest.raises(ValueError, match="which is not after"):
        store.grant(perms.SALE_VOID, until=NOW + offset, now=NOW)

    assert perms.SALE_VOID not in store.current.overrides  # type: ignore[union-attr]


# ── Housekeeping the window needs ───────────────────────────────────────────


def test_a_second_grant_never_shortens_the_first() -> None:
    """Two supervisors in the same minute must not leave less time than one."""
    store = store_with_cashier()
    generous = NOW + timedelta(seconds=90)
    store.grant(perms.SALE_VOID, until=generous, now=NOW)
    store.grant(perms.SALE_VOID, until=NOW + timedelta(seconds=10), now=NOW)

    assert store.current.overrides[perms.SALE_VOID] == generous  # type: ignore[union-attr]


def test_expired_grants_are_dropped_on_the_way_past() -> None:
    """`allows()` already ignores them; this stops the dict growing all shift.

    It also keeps a later diagnostics screen from listing permissions the
    cashier does not have, which would read as a much more alarming thing than
    it is.
    """
    store = store_with_cashier()
    store.grant(perms.SALE_VOID, until=NOW + OVERRIDE_GRANT_TTL, now=NOW)

    later = NOW + timedelta(minutes=5)
    store.grant(perms.PRICE_OVERRIDE, until=later + OVERRIDE_GRANT_TTL, now=later)

    assert set(store.current.overrides) == {perms.PRICE_OVERRIDE}  # type: ignore[union-attr]


def test_the_ttl_is_ninety_seconds() -> None:
    """Architecture §11.3. Pinned because the modal's copy will quote it."""
    assert OVERRIDE_GRANT_TTL == timedelta(seconds=90)


def test_signing_out_takes_the_grants_with_it() -> None:
    store = store_with_cashier()
    store.grant(perms.SALE_VOID, until=NOW + OVERRIDE_GRANT_TTL, now=NOW)
    store.clear()

    assert store.current is None


# ── Verifying the second person ─────────────────────────────────────────────
#
# `pins.verify_pin` has had exactly one caller since phase 1: `_login_offline`,
# which verifies somebody and then *becomes* them. `authorize_override` is the
# second, and it verifies somebody and then walks away.

SUPERVISOR = {
    "user_id": "018f0000-0000-7000-8000-000000000002",
    "employee_code": "S001",
    "pin": "7241",
}


@pytest.fixture
def till(auth_service, seeded_cashier):
    """A cashier signed in at the till, with a supervisor known to it."""
    auth_service.seed_local_user(
        user_id=SUPERVISOR["user_id"],
        employee_code=SUPERVISOR["employee_code"],
        full_name="Ravi Menon",
        store_id=TEST_STORE_ID,
        pin=SUPERVISOR["pin"],
        roles=frozenset({perms.SUPERVISOR}),
        permissions=perms.permissions_for(frozenset({perms.SUPERVISOR})),
    )
    auth_service.sessions.set(cashier(), access_token=None)
    return auth_service


def authorize(service, **kwargs):
    call = {
        "approver_code": SUPERVISOR["employee_code"],
        "pin": SUPERVISOR["pin"],
        "permission": perms.SALE_VOID,
    }
    call.update(kwargs)
    return service.authorize_override(**call)


def test_a_supervisor_lends_the_cashier_a_void(till) -> None:
    grant = authorize(till)

    assert till.sessions.current.allows(perms.SALE_VOID, now=utcnow())
    assert grant.permission == perms.SALE_VOID
    assert grant.actor_code == "C001"
    assert grant.approver_code == "S001"


def test_the_cashier_is_still_the_one_signed_in(till) -> None:
    """The difference between this and `_login_offline`, in one assertion.

    Both verify a PIN against `cached_users`. That one then replaces the
    session with the person it verified; this one must not — the cashier has a
    customer in front of them and a cart open.
    """
    authorize(till)

    session = till.sessions.current
    assert session.employee_code == "C001"
    assert session.user_id != SUPERVISOR["user_id"]


def test_the_grant_names_both_people(till) -> None:
    """`audit_log.approver_id` has existed since 0001 with nothing writing it.

    An override is the one path where somebody deliberately exceeds their
    permissions. A row naming only the cashier would launder that into
    ordinary work, which is worse than no row at all — it would look like the
    cashier had the permission all along.
    """
    grant = authorize(till)
    rows = till.audit.overrides()

    assert len(rows) == 1
    row = rows[0]
    assert row["actor_id"] == grant.actor_id
    assert row["approver_id"] == grant.approver_id
    assert row["actor_id"] != row["approver_id"]
    assert row["action"] == OVERRIDE_GRANTED
    assert row["entity_id"] == perms.SALE_VOID


def test_the_row_is_written_when_the_grant_is_minted(till) -> None:
    """Not when it is spent. A grant nobody used is still a fact about the shop.

    Nothing here voids anything: the supervisor authorises and the test stops.
    The row must exist anyway, because "a supervisor was called to this till
    eleven times today" is exactly the shape an audit log is for.
    """
    authorize(till)
    assert len(till.audit.overrides()) == 1


# ── The refusals, which are most of it ──────────────────────────────────────


def test_a_person_cannot_authorise_themselves(till) -> None:
    """The whole control, and the one that would look fine if it were missing.

    A cashier who knows their own PIN would otherwise approve their own void,
    and the audit row would name the same person twice while reading
    perfectly ordinarily.
    """
    with pytest.raises(CannotAuthoriseSelf):
        authorize(till, approver_code="C001", pin="4913")

    assert not till.sessions.current.allows(perms.SALE_VOID, now=utcnow())
    assert till.audit.overrides() == []


def test_an_approver_cannot_lend_what_they_do_not_hold(till) -> None:
    """A second cashier is not a supervisor.

    `sale.discount.unlimited` belongs to manager and admin; a supervisor does
    not hold it, so they cannot lend it however senior they look.
    """
    with pytest.raises(ApproverLacksPermission) as refused:
        authorize(till, permission=perms.SALE_DISCOUNT_UNLIMITED)

    assert refused.value.approver_code == "S001"
    assert till.audit.overrides() == []


def test_a_supervisor_this_terminal_has_never_seen_is_named_as_such(till) -> None:
    """Not "wrong PIN", which would send somebody looking for the wrong fault.

    Offline this reads `cached_users`, so a supervisor who has never signed in
    here cannot be checked at all. That is a real limitation of a real shop
    and it gets its own sentence; the cloud path is what covers it.
    """
    with pytest.raises(NoOfflineIdentity) as refused:
        authorize(till, approver_code="Z999")

    assert "has not signed in on this terminal" in str(refused.value)


def test_a_wrong_pin_is_refused(till) -> None:
    with pytest.raises(LoginFailed):
        authorize(till, pin="0000")

    assert not till.sessions.current.allows(perms.SALE_VOID, now=utcnow())
    assert till.audit.overrides() == []


def test_a_permission_outside_the_rule_is_refused_before_any_pin(till) -> None:
    """`cash.payout` never reaches the PIN prompt.

    Ordering matters here rather than only the outcome: a request that cannot
    succeed must not become a reason for somebody to type a credential.
    """
    with pytest.raises(NotOverridable):
        authorize(till, permission=perms.CASH_PAYOUT, pin="definitely-wrong")

    assert till.audit.overrides() == []


def test_authorising_what_the_session_already_holds_is_refused(till) -> None:
    """Nothing is being escalated, so nothing should be logged as an escalation.

    Not reachable with a cashier at the till: no overridable key is one a
    cashier holds, which `test_nothing_the_cashier_already_holds_is_overridable`
    asserts. It is reachable when a *supervisor* is working the register — they
    already hold `sale.void` — and that is a real shift in a small shop rather
    than a contrived one.
    """
    till.sessions.set(
        Session(
            user_id=SUPERVISOR["user_id"],
            employee_code=SUPERVISOR["employee_code"],
            full_name="Ravi Menon",
            store_id=TEST_STORE_ID,
            roles=frozenset({perms.SUPERVISOR}),
            permissions=perms.permissions_for(frozenset({perms.SUPERVISOR})),
            authenticated_at=utcnow(),
        ),
        access_token=None,
    )

    with pytest.raises(NothingToAuthorise):
        authorize(till, permission=perms.SALE_VOID)

    assert till.audit.overrides() == []


def test_an_approver_whose_snapshot_aged_out_cannot_authorise(till) -> None:
    """The rule that stops them signing in stops them lending, too.

    Fourteen days is the snapshot TTL. A terminal that has been off the
    network longer than that is no longer entitled to believe what it
    remembers about anybody — including that they are still a supervisor.
    """
    till.seed_local_user(
        user_id=SUPERVISOR["user_id"],
        employee_code=SUPERVISOR["employee_code"],
        full_name="Ravi Menon",
        store_id=TEST_STORE_ID,
        pin=SUPERVISOR["pin"],
        roles=frozenset({perms.SUPERVISOR}),
        permissions=perms.permissions_for(frozenset({perms.SUPERVISOR})),
        signed_at=utcnow() - timedelta(days=20),
    )

    with pytest.raises(LoginFailed) as refused:
        authorize(till)

    assert "offline too long" in str(refused.value)
    assert till.audit.overrides() == []


def test_a_revoked_approver_is_no_longer_known_to_the_terminal(till, users) -> None:
    """`revoke()` deletes the snapshot rather than flagging it.

    So a deactivated supervisor reads as somebody this till has never seen,
    which is the honest thing for it to say: the terminal genuinely no longer
    holds anything about them to check a PIN against.
    """
    users.revoke(SUPERVISOR["user_id"])

    with pytest.raises(NoOfflineIdentity):
        authorize(till)

    assert till.audit.overrides() == []


def test_nobody_signed_in_is_refused_before_any_pin(auth_service) -> None:
    """A supervisor typing a PIN into an empty till gets told, not ignored."""
    with pytest.raises(NotSignedIn):
        authorize(auth_service)


# ── The gap this step leaves open ───────────────────────────────────────────


@pytest.mark.xfail(
    reason="phase 7 — nothing pushes a standalone audit row, so a minted "
    "grant never leaves the terminal and slice 5's viewer cannot see it",
    strict=True,
)
def test_an_override_row_can_be_pushed() -> None:
    """The trail is written locally and stops there.

    Audit rows in this system ride to the cloud attached to a parent entity:
    `payloads.py` collects `entity = 'sale'` rows when it pushes a sale, and
    `entity = 'stock_ledger'` rows when it pushes a movement. `sync_push`
    accepts exactly three entities — `sale`, `stock_movement`, `sale_review`.

    A minted grant has no parent. It is written when the supervisor
    authorises, which may be before any sale exists and may be before a sale
    that never happens. So it stays on the terminal, and slice 5's audit
    viewer — cloud-direct, like the rest of admin — will not show it.

    That makes the audit log a document that is true except where it matters,
    which is the thing the plan warns about in its own words. `audit_log_insert`
    already accepts the row under the cashier's own claim, so the policy is not
    the obstacle: what is missing is an `override` entity in `payloads.py` and
    in `sync_push`. That is the next commit, and this marker comes off with it.
    """
    from app.sync.payloads import PayloadBuilder

    assert "override" in PayloadBuilder.SUPPORTED_ENTITIES

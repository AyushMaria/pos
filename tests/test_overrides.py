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

from app.domain import permissions as perms
from app.domain.identity import (
    OVERRIDE_GRANT_TTL,
    NotOverridable,
    Session,
)
from app.services.auth_service import NotSignedIn, SessionStore

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

"""The payment lifecycle — architecture §13.1, §13.2.

Cash is the only provider in phase 3, but the machine it drives is the one
UPI and card will use. If cash gets a shortcut here, phase 4 rewrites the
register instead of adding a button.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.money import Money
from app.domain.payments import (
    DEFAULT_ATTEMPT_TTL,
    AttemptState,
    PaymentAttempt,
    PaymentError,
    approved_total,
    balance_of,
    can_transition,
    expire_stale,
)

NOW = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)


def attempt(
    amount: int = 10000,
    method: str = "cash",
    state: AttemptState = AttemptState.PENDING,
    expires_at: datetime | None = None,
) -> PaymentAttempt:
    return PaymentAttempt(
        id="a1",
        method=method,  # type: ignore[arg-type]
        amount=Money(amount),
        state=state,
        started_at=NOW,
        expires_at=expires_at,
    )


# ── The state machine ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target",
    [
        AttemptState.APPROVED,
        AttemptState.DECLINED,
        AttemptState.CANCELLED,
        AttemptState.EXPIRED,
        AttemptState.UNKNOWN,
    ],
)
def test_pending_can_reach_every_terminal_state(target: AttemptState) -> None:
    resolved = attempt().to(target, at=NOW)

    assert resolved.state is target
    assert resolved.resolved_at == NOW


@pytest.mark.parametrize(
    "state",
    [
        AttemptState.APPROVED,
        AttemptState.DECLINED,
        AttemptState.CANCELLED,
        AttemptState.EXPIRED,
        AttemptState.UNKNOWN,
    ],
)
def test_terminal_states_are_terminal(state: AttemptState) -> None:
    """An approved payment is never quietly un-approved (architecture §1.4)."""
    with pytest.raises(PaymentError, match="cannot go from"):
        attempt(state=state).to(AttemptState.APPROVED, at=NOW)


def test_cash_uses_the_same_machine_as_everything_else() -> None:
    """Modelling cash as synchronous is what phase 4 would have to undo."""
    cash = attempt(method="cash")

    assert cash.state is AttemptState.PENDING
    assert cash.to(AttemptState.APPROVED, at=NOW).state.counts_toward_balance


def test_only_approved_counts_toward_the_balance() -> None:
    for state in AttemptState:
        expected = state is AttemptState.APPROVED
        assert state.counts_toward_balance is expected


def test_recoverable_states_leave_the_cart_open() -> None:
    for state in (AttemptState.DECLINED, AttemptState.CANCELLED, AttemptState.EXPIRED):
        assert state.leaves_cart_open

    assert not AttemptState.UNKNOWN.leaves_cart_open
    assert not AttemptState.APPROVED.leaves_cart_open


def test_unknown_is_not_a_failure() -> None:
    """"Customer insists they paid" is a supervisor's problem, not a retry."""
    assert not AttemptState.UNKNOWN.leaves_cart_open
    assert not AttemptState.UNKNOWN.counts_toward_balance


def test_a_negative_attempt_is_refused() -> None:
    with pytest.raises(PaymentError):
        attempt(amount=-100)


def test_transition_table_is_explicit() -> None:
    assert can_transition(AttemptState.PENDING, AttemptState.APPROVED)
    assert not can_transition(AttemptState.APPROVED, AttemptState.CANCELLED)


# ── Expiry ──────────────────────────────────────────────────────────────────


def test_a_pending_attempt_expires() -> None:
    stale = attempt(expires_at=NOW + DEFAULT_ATTEMPT_TTL)

    assert not stale.is_expired_at(NOW)
    assert stale.is_expired_at(NOW + DEFAULT_ATTEMPT_TTL)


def test_expire_stale_releases_the_cart() -> None:
    """An abandoned attempt must not wedge the till (architecture §13.3)."""
    attempts = [attempt(expires_at=NOW + timedelta(minutes=5))]

    expired = expire_stale(attempts, now=NOW + timedelta(minutes=6))

    assert expired[0].state is AttemptState.EXPIRED
    assert expired[0].state.leaves_cart_open


def test_expire_stale_leaves_resolved_attempts_alone() -> None:
    done = attempt(state=AttemptState.APPROVED, expires_at=NOW - timedelta(hours=1))

    assert expire_stale([done], now=NOW)[0].state is AttemptState.APPROVED


def test_cash_never_expires() -> None:
    """The customer is standing there; there is nothing to time out."""
    assert not attempt(expires_at=None).is_expired_at(NOW + timedelta(days=1))


# ── The balance loop ────────────────────────────────────────────────────────


def test_a_single_cash_payment_settles_the_sale() -> None:
    paid = [attempt(amount=49800, state=AttemptState.APPROVED)]

    balance = balance_of(Money(49800), paid)

    assert balance.is_settled
    assert balance.outstanding.is_zero
    assert balance.change_due.is_zero


def test_a_partial_payment_leaves_a_balance() -> None:
    paid = [attempt(amount=10000, state=AttemptState.APPROVED)]

    balance = balance_of(Money(49800), paid)

    assert not balance.is_settled
    assert balance.outstanding == Money(39800)


def test_a_split_tender_settles_on_the_sum() -> None:
    paid = [
        PaymentAttempt(id="a", method="upi", amount=Money(10000),
                       state=AttemptState.APPROVED),
        PaymentAttempt(id="b", method="cash", amount=Money(2300),
                       state=AttemptState.APPROVED),
    ]

    balance = balance_of(Money(12340), paid, rounding_adjustment=Money(-40))

    assert balance.is_settled
    assert balance.outstanding.is_zero


def test_failed_attempts_do_not_reduce_the_balance() -> None:
    """The commonest way a till loses money is counting a declined payment."""
    attempts = [
        attempt(amount=49800, state=AttemptState.DECLINED),
        attempt(amount=49800, state=AttemptState.CANCELLED),
        attempt(amount=49800, state=AttemptState.EXPIRED),
        attempt(amount=49800, state=AttemptState.UNKNOWN),
        attempt(amount=49800, state=AttemptState.PENDING),
    ]

    assert approved_total(attempts).is_zero
    assert not balance_of(Money(49800), attempts).is_settled


def test_overpaying_in_cash_yields_change() -> None:
    paid = [attempt(amount=50000, state=AttemptState.APPROVED)]

    balance = balance_of(Money(49800), paid)

    assert balance.is_settled
    assert balance.change_due == Money(200)


def test_rounding_moves_the_balance_not_the_total() -> None:
    """₹123.40 owed, rounded to ₹123 for cash: ₹123 settles it."""
    paid = [attempt(amount=12300, state=AttemptState.APPROVED)]

    balance = balance_of(Money(12340), paid, rounding_adjustment=Money(-40))

    assert balance.grand_total == Money(12340)
    assert balance.is_settled


@given(
    st.integers(min_value=0, max_value=1_000_000),
    st.lists(st.integers(min_value=0, max_value=200_000), max_size=6),
)
def test_the_balance_always_reconciles(grand: int, payments: list[int]) -> None:
    attempts = [
        PaymentAttempt(id=str(i), method="cash", amount=Money(amount),
                       state=AttemptState.APPROVED)
        for i, amount in enumerate(payments)
    ]

    balance = balance_of(Money(grand), attempts)

    assert balance.paid + balance.outstanding == balance.grand_total
    assert balance.is_settled == (balance.paid >= Money(grand))

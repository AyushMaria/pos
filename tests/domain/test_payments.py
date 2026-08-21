"""The payment lifecycle — architecture §13.1, §13.2.

Cash was the only provider when this machine was written, and UPI arrived
without changing any of it — which was the point. What UPI *did* add is
attestation: a state transition a human performs, carrying a figure the
customer chose rather than the one the till asked for (§13.3).
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
    needs_review,
    pending,
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


# ── Attestation — architecture §13.3 ────────────────────────────────────────


def test_attesting_approves_for_the_amount_asked() -> None:
    """The common case: the customer paid exactly what they were told to."""
    upi = attempt(amount=3740, method="upi", expires_at=NOW + DEFAULT_ATTEMPT_TTL)

    attested = upi.attest(at=NOW)

    assert attested.state is AttemptState.APPROVED
    assert attested.amount == Money(3740)
    assert attested.resolved_at == NOW


def test_attesting_records_what_actually_arrived() -> None:
    """A printed counter QR embeds no amount, so the customer types it — and
    what they type is what the till must record, not what it asked for."""
    upi = attempt(amount=3740, method="upi")

    attested = upi.attest(at=NOW, amount=Money(3000))

    assert attested.amount == Money(3000)
    assert attested.state is AttemptState.APPROVED


def test_attesting_captures_the_reference() -> None:
    upi = attempt(amount=3740, method="upi")

    assert upi.attest(at=NOW, reference="UTR42").txn_ref == "UTR42"


def test_a_zero_or_negative_attestation_is_refused() -> None:
    """"They paid nothing" is a cancellation, not a payment."""
    upi = attempt(amount=3740, method="upi")

    with pytest.raises(PaymentError, match="positive"):
        upi.attest(at=NOW, amount=Money(0))


def test_an_attempt_cannot_be_attested_twice() -> None:
    """Terminal states are terminal, attestation included — otherwise a
    second tap doubles the money collected."""
    once = attempt(amount=3740, method="upi").attest(at=NOW)

    with pytest.raises(PaymentError, match="cannot go from approved"):
        once.attest(at=NOW)


def test_an_expired_attempt_cannot_be_attested() -> None:
    """The cart was released; approving now would collect against a basket
    that has moved on."""
    lapsed = attempt(
        amount=3740, method="upi", expires_at=NOW - timedelta(seconds=1)
    ).to(AttemptState.EXPIRED, at=NOW)

    with pytest.raises(PaymentError):
        lapsed.attest(at=NOW)


# ── Short and over — both possible on a static QR ───────────────────────────


def test_a_short_payment_leaves_the_balance_open() -> None:
    paid = [attempt(amount=3000, method="upi", state=AttemptState.APPROVED)]

    balance = balance_of(Money(3740), paid)

    assert balance.is_short
    assert not balance.is_settled
    assert balance.outstanding == Money(740)
    assert balance.change_due.is_zero


def test_an_overpayment_is_change_due() -> None:
    """Possible on UPI now, not only on cash: the customer typed ₹50."""
    paid = [attempt(amount=5000, method="upi", state=AttemptState.APPROVED)]

    balance = balance_of(Money(3740), paid)

    assert balance.is_settled
    assert not balance.is_short
    assert balance.change_due == Money(1260)


def test_an_untouched_balance_is_not_short() -> None:
    """Nothing paid is not the same as partly paid — one is a sale in
    progress, the other is a sale that needs finishing."""
    assert not balance_of(Money(3740), []).is_short


# ── "I can't tell" ──────────────────────────────────────────────────────────


def test_an_unknown_attempt_marks_the_sale_for_review() -> None:
    attempts = [attempt(amount=3740, method="upi").to(AttemptState.UNKNOWN, at=NOW)]

    assert needs_review(attempts)


def test_an_unknown_attempt_collects_nothing() -> None:
    """The sale posts because a queue is waiting, but the money is never
    counted as received (architecture §13.5)."""
    attempts = [attempt(amount=3740, method="upi").to(AttemptState.UNKNOWN, at=NOW)]

    assert approved_total(attempts).is_zero
    assert not balance_of(Money(3740), attempts).is_settled


def test_ordinary_attempts_need_no_review() -> None:
    assert not needs_review([attempt(state=AttemptState.APPROVED)])
    assert not needs_review([attempt(state=AttemptState.CANCELLED)])
    assert not needs_review([])


def test_pending_lists_only_what_is_still_waiting() -> None:
    waiting = attempt(amount=3740, method="upi")
    done = attempt(amount=100, state=AttemptState.APPROVED)

    assert pending([waiting, done]) == [waiting]

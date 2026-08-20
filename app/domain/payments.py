"""Payment attempts and the balance loop — architecture §13.1, §13.2.

Cash resolves instantly; UPI does not. Modelling cash as synchronous bakes in
an assumption that the very next method breaks, so **both go through the same
states from day one**:

    PENDING ──→ APPROVED ──→ (counts toward the balance)
       │
       ├──→ DECLINED / CANCELLED / EXPIRED   (cart stays open, retry)
       └──→ UNKNOWN  →  sale posted as requires_review

Adding UPI in phase 4, or a card terminal later, is then a registry entry and
a tender button rather than a change to the register's state machine
(architecture §17, last row).

The sale closes on **balance**, not on a single payment. That is what makes
split tender work, and it is why `outstanding` rather than `total` is the
figure every tender decision is made against.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from app.domain.money import ZERO, Money, total
from app.domain.tender import TenderMethod

#: An abandoned QR must not wedge a cart (architecture §13.3). Cash never
#: expires — the customer is standing there — but the field is on every
#: attempt so the lifecycle stays identical.
DEFAULT_ATTEMPT_TTL = timedelta(minutes=5)


class PaymentError(ValueError):
    """An illegal payment transition or amount."""


class AttemptState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    #: The terminal genuinely does not know. Never guess: the sale posts as
    #: `requires_review` and a supervisor resolves it (§13.5).
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self is not AttemptState.PENDING

    @property
    def counts_toward_balance(self) -> bool:
        return self is AttemptState.APPROVED

    @property
    def leaves_cart_open(self) -> bool:
        """True when the cashier can simply try again."""
        return self in {
            AttemptState.DECLINED,
            AttemptState.CANCELLED,
            AttemptState.EXPIRED,
        }


#: Legal transitions. Everything else is a bug, and raising here is how it is
#: found in a test rather than in a shift-close variance.
_TRANSITIONS: dict[AttemptState, frozenset[AttemptState]] = {
    AttemptState.PENDING: frozenset(
        {
            AttemptState.APPROVED,
            AttemptState.DECLINED,
            AttemptState.CANCELLED,
            AttemptState.EXPIRED,
            AttemptState.UNKNOWN,
        }
    ),
    # Terminal states are terminal. An approved payment is never quietly
    # un-approved; a correction is a compensating row (architecture §1.4).
    AttemptState.APPROVED: frozenset(),
    AttemptState.DECLINED: frozenset(),
    AttemptState.CANCELLED: frozenset(),
    AttemptState.EXPIRED: frozenset(),
    AttemptState.UNKNOWN: frozenset(),
}


def can_transition(current: AttemptState, target: AttemptState) -> bool:
    return target in _TRANSITIONS[current]


@dataclass(frozen=True, slots=True)
class PaymentAttempt:
    """One attempt to collect part or all of a balance."""

    id: str
    method: TenderMethod
    amount: Money
    state: AttemptState = AttemptState.PENDING
    started_at: datetime | None = None
    expires_at: datetime | None = None
    resolved_at: datetime | None = None
    txn_ref: str | None = None
    qr_payload: str | None = None
    failure_reason: str | None = None
    #: Cash handed over, when more than the amount owed.
    tendered: Money | None = None

    def __post_init__(self) -> None:
        if self.amount.is_negative:
            raise PaymentError(f"a payment attempt cannot be for {self.amount}")

    def to(
        self,
        target: AttemptState,
        *,
        at: datetime,
        reason: str | None = None,
    ) -> PaymentAttempt:
        """Move to a new state, refusing anything the machine does not allow."""
        if not can_transition(self.state, target):
            raise PaymentError(
                f"attempt {self.id}: cannot go from {self.state.value} to "
                f"{target.value}"
            )
        return replace(
            self,
            state=target,
            resolved_at=at,
            failure_reason=reason or self.failure_reason,
        )

    def is_expired_at(self, when: datetime) -> bool:
        if self.state is not AttemptState.PENDING or self.expires_at is None:
            return False
        return when >= self.expires_at


def approved_total(attempts: Iterable[PaymentAttempt]) -> Money:
    """What has actually been collected so far."""
    return total(
        [
            attempt.amount
            for attempt in attempts
            if attempt.state.counts_toward_balance
        ]
    )


@dataclass(frozen=True, slots=True)
class BalanceState:
    """Where a sale stands in the split-tender loop (architecture §13.2)."""

    grand_total: Money
    paid: Money
    rounding_adjustment: Money

    @property
    def outstanding(self) -> Money:
        """What is still owed. Zero or below means the sale can post."""
        return self.grand_total + self.rounding_adjustment - self.paid

    @property
    def is_settled(self) -> bool:
        return not self.outstanding.is_positive

    @property
    def change_due(self) -> Money:
        """Only ever non-zero for a cash-final tender.

        A UPI attempt is always for the exact outstanding balance because the
        amount is embedded in the QR, so only cash can overpay (§13.2).
        """
        outstanding = self.outstanding
        return -outstanding if outstanding.is_negative else Money.zero()


def balance_of(
    grand_total: Money,
    attempts: Iterable[PaymentAttempt],
    rounding_adjustment: Money = ZERO,
) -> BalanceState:
    return BalanceState(
        grand_total=grand_total,
        paid=approved_total(attempts),
        rounding_adjustment=rounding_adjustment,
    )


def expire_stale(
    attempts: Iterable[PaymentAttempt], *, now: datetime
) -> list[PaymentAttempt]:
    """Auto-cancel anything past its expiry, so an abandoned attempt cannot
    leave a cart wedged (architecture §13.3)."""
    return [
        attempt.to(AttemptState.EXPIRED, at=now, reason="attempt expired")
        if attempt.is_expired_at(now)
        else attempt
        for attempt in attempts
    ]

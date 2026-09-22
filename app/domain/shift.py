"""Shift arithmetic — architecture §13.4-13.5, phase 8 plan.

What a drawer should hold at the end of a day, and the figures that explain
the difference when it does not. Pure, like the rest of `domain/`: given the
rows, what are the numbers? No database, no clock, no rounding of its own —
every paisa here was already decided by the tender code in phase 4.

**Three ways this goes wrong, each of which produces "a permanent,
unexplainable variance at every shift close" (§13.4):**

* **Rounding is already inside the cash total.** A ₹123.40 basket paid in
  cash is a ₹123 payment row. The rounding figure is reported *beside* the
  total as the explanation, never added to or taken from it. Adding it
  double-counts by a few rupees a day, which an owner will read as theft.
* **A split tender is two lines.** ₹100 UPI + ₹23 cash is one sale, one UPI
  payment and one cash payment, and the rounding on that sale belongs to the
  cash portion only — which is what phase 4 already wrote.
* **A sale under review is not takings.** The customer says they paid; the
  shop does not know. It is a line with a count and a sum until a supervisor
  resolves it, and a `not_paid` outcome means it never enters the totals.

The model is deliberately over plain records rather than repository rows or
domain objects, so a test can build a day in five lines and the properties
in `tests/domain/test_shift.py` can generate thousands of them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.domain.money import Money

Direction = Literal["in", "out"]


class ShiftError(ValueError):
    """A figure that cannot be computed, or a close that should not happen."""


@dataclass(frozen=True, slots=True)
class SaleRow:
    """What the close needs to know about one sale."""

    sale_id: str
    #: `completed`, `requires_review`, `voided`. A voided sale counts for
    #: nothing; a sale under review is listed and excluded.
    status: str
    grand_total: Money
    #: Signed. Negative is the shop's loss (a ₹123.40 basket collected as
    #: ₹123 is -40 paise). Always zero on a sale with no cash portion.
    rounding_adjustment: Money
    #: The `sale_reviews` outcome if a supervisor has resolved it: `paid`,
    #: `not_paid`, or None while it is still under review.
    review_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentRow:
    """One approved payment. Cancelled and expired attempts never get here."""

    sale_id: str
    method: str
    amount: Money
    #: Phase 4's flag: 0 for a cashier's attestation, 1 once matched to a
    #: settlement. Every UPI payment today is 0.
    verified: bool = False


@dataclass(frozen=True, slots=True)
class CashMovementRow:
    direction: Direction
    amount: Money


@dataclass(frozen=True, slots=True)
class ShiftFigures:
    """Everything the Z-report shows, and everything the variance is made of."""

    opening_float: Money
    cash_sales: Money
    upi_attested: Money
    upi_verified: Money
    cash_in: Money
    cash_out: Money
    #: Sum of `rounding_adjustment` over sales that count. Reported, never
    #: added: it is already inside `cash_sales`.
    rounding: Money
    #: Sales still under review: how many and how much. Not in any total.
    under_review_count: int
    under_review_total: Money
    sales_count: int

    @property
    def expected_cash(self) -> Money:
        return self.opening_float + self.cash_sales + self.cash_in - self.cash_out

    @property
    def takings(self) -> Money:
        """What the shop took, by every method that is known to have paid."""
        return self.cash_sales + self.upi_attested + self.upi_verified

    def variance(self, counted_cash: Money) -> Money:
        """Positive is over; negative is short."""
        return counted_cash - self.expected_cash


def _counts(sale: SaleRow) -> bool:
    """Does this sale's money belong in the totals?

    A completed sale does. A voided one does not. A sale under review counts
    only once a supervisor has said `paid`; `not_paid` and "not yet" both
    keep it out, because a total that includes money the shop may not have
    is a total nobody can reconcile against.
    """
    if sale.status == "completed":
        return True
    if sale.status == "requires_review":
        return sale.review_outcome == "paid"
    return False


def figures(
    *,
    opening_float: Money,
    sales: Iterable[SaleRow],
    payments: Iterable[PaymentRow],
    movements: Iterable[CashMovementRow],
) -> ShiftFigures:
    """The arithmetic. Every input is a plain row; every output is Money."""
    by_id = {sale.sale_id: sale for sale in sales}
    counting = {sale_id for sale_id, sale in by_id.items() if _counts(sale)}

    cash_sales = Money.zero()
    upi_attested = Money.zero()
    upi_verified = Money.zero()
    for payment in payments:
        if payment.sale_id not in by_id:
            raise ShiftError(f"payment for unknown sale {payment.sale_id}")
        if payment.sale_id not in counting:
            continue
        if payment.method == "cash":
            cash_sales = cash_sales + payment.amount
        elif payment.method == "upi":
            if payment.verified:
                upi_verified = upi_verified + payment.amount
            else:
                upi_attested = upi_attested + payment.amount
        else:
            raise ShiftError(f"no line on the Z-report for method {payment.method!r}")

    rounding = Money.zero()
    for sale_id in counting:
        rounding = rounding + by_id[sale_id].rounding_adjustment

    under_review = [
        sale
        for sale in by_id.values()
        if sale.status == "requires_review" and sale.review_outcome is None
    ]
    under_review_total = Money.zero()
    for sale in under_review:
        under_review_total = under_review_total + sale.grand_total

    cash_in = Money.zero()
    cash_out = Money.zero()
    for movement in movements:
        if movement.direction == "in":
            cash_in = cash_in + movement.amount
        elif movement.direction == "out":
            cash_out = cash_out + movement.amount
        else:
            raise ShiftError(f"cash movement direction {movement.direction!r}")

    return ShiftFigures(
        opening_float=opening_float,
        cash_sales=cash_sales,
        upi_attested=upi_attested,
        upi_verified=upi_verified,
        cash_in=cash_in,
        cash_out=cash_out,
        rounding=rounding,
        under_review_count=len(under_review),
        under_review_total=under_review_total,
        sales_count=len(counting),
    )

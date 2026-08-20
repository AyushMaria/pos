"""Line pricing and discount stacking — architecture §6.

The order of operations is the whole content of this module, and it is fixed:

    gross      = unit price x quantity, rounded once, at the line
    discounts  = applied in a defined order, each on the running amount
    line total = gross - discounts, never below zero
    tax        = extracted from the *discounted* line total

Tax comes off the discounted figure because GST is due on the consideration
actually received. Computing tax on the gross and then discounting would
overcharge the customer and overstate the liability — in that order of
seriousness.

Discounts stack in a defined sequence rather than all being taken off the
gross. Two 10% discounts on ₹100 give ₹81, not ₹80. Whether that is the right
commercial answer is a policy question; that it is *predictable* is not
optional, because a cashier has to be able to explain the number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.domain.money import Money, multiply_by_quantity, percentage_of, total
from app.domain.tax import TaxCode, extract_inclusive_tax

DiscountKind = Literal["percentage", "amount"]
DiscountSource = Literal["promotion", "manual"]


class PricingError(ValueError):
    """A line could not be priced."""


@dataclass(frozen=True, slots=True)
class Discount:
    """A reduction applied to one line.

    `value` is basis points for a percentage (1000 = 10%) or paise for a fixed
    amount. Never a float, never a percentage as a decimal fraction.
    """

    kind: DiscountKind
    value: int
    reason: str = ""
    source: DiscountSource = "promotion"
    promotion_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise PricingError(f"discount value must be an int, got {self.value!r}")
        if self.value < 0:
            raise PricingError("a negative discount is a price increase; refuse it")
        if self.kind == "percentage" and self.value > 10_000:
            raise PricingError("a percentage discount cannot exceed 100%")

    def against(self, running: Money) -> Money:
        """What this discount takes off, given what is left to discount."""
        if self.kind == "percentage":
            return percentage_of(running, self.value)
        return Money(self.value)


@dataclass(frozen=True, slots=True)
class LineInput:
    """A line as the register knows it, before pricing."""

    product_id: str
    description: str
    unit_price: Money
    qty_milli: int
    tax_code: TaxCode
    discounts: tuple[Discount, ...] = ()
    barcode_scanned: str | None = None
    overridden_by: str | None = None
    override_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.qty_milli) is not int:
            raise PricingError(
                f"quantity must be integer thousandths, got {self.qty_milli!r}"
            )
        if self.qty_milli == 0:
            raise PricingError("a line with no quantity is not a line")


@dataclass(frozen=True, slots=True)
class AppliedDiscount:
    """A discount and what it actually came to, after capping."""

    discount: Discount
    amount: Money


@dataclass(frozen=True, slots=True)
class PricedLine:
    """A fully priced line. Every figure here lands on `sale_lines`."""

    line: LineInput
    gross: Money
    applied: tuple[AppliedDiscount, ...]
    line_total: Money
    tax_amount: Money
    taxable_base: Money

    @property
    def discount_total(self) -> Money:
        return self.gross - self.line_total

    @property
    def tax_code(self) -> TaxCode:
        return self.line.tax_code

    @property
    def description(self) -> str:
        return self.line.description


def price_line(line: LineInput) -> PricedLine:
    """Price one line, start to finish.

    Pure: the same input always gives the same figures, which is what makes a
    disputed receipt reproducible six months later.
    """
    gross = multiply_by_quantity(line.unit_price, line.qty_milli)

    running = gross
    applied: list[AppliedDiscount] = []
    for discount in _ordered(line.discounts):
        amount = discount.against(running)

        # A discount can never make a line negative. A fixed ₹50 off a ₹30
        # item takes ₹30, not ₹50 — otherwise the basket starts paying the
        # customer, and a promotion misconfiguration becomes a cash leak.
        if amount > running:
            amount = running
        if amount.is_zero:
            continue

        applied.append(AppliedDiscount(discount=discount, amount=amount))
        running = running - amount

    tax_amount = extract_inclusive_tax(running, line.tax_code.rate_bp)

    return PricedLine(
        line=line,
        gross=gross,
        applied=tuple(applied),
        line_total=running,
        tax_amount=tax_amount,
        taxable_base=running - tax_amount,
    )


def _ordered(discounts: tuple[Discount, ...]) -> list[Discount]:
    """Promotions first, then manual reductions.

    A manual discount is authorised by a supervisor looking at a number on
    screen. That number has to be the one the customer will actually be
    charged, so the manual reduction is applied last — to the amount left
    after every automatic promotion.
    """
    return [d for d in discounts if d.source == "promotion"] + [
        d for d in discounts if d.source == "manual"
    ]


@dataclass(frozen=True, slots=True)
class LineTotals:
    """Aggregates over a set of priced lines."""

    gross: Money = field(default_factory=Money.zero)
    discount_total: Money = field(default_factory=Money.zero)
    net: Money = field(default_factory=Money.zero)
    tax_total: Money = field(default_factory=Money.zero)
    taxable_base: Money = field(default_factory=Money.zero)


def sum_lines(lines: tuple[PricedLine, ...]) -> LineTotals:
    return LineTotals(
        gross=total([line.gross for line in lines]),
        discount_total=total([line.discount_total for line in lines]),
        net=total([line.line_total for line in lines]),
        tax_total=total([line.tax_amount for line in lines]),
        taxable_base=total([line.taxable_base for line in lines]),
    )

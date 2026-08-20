"""The cart — architecture §4, §6.

The cart lives server-side and React never sums a total (architecture §4).
This module is why that is affordable: adding a line returns a fully
recalculated basket from one pure function, so a webview crash mid-sale loses
nothing and a price bug has exactly one place to be.

A `Cart` is immutable. Every operation returns a new one. That costs nothing
at basket sizes a till sees, and it buys two things worth more: a parked
transaction is just a value that can be stored and resumed, and an operation
that raises cannot leave a half-mutated basket on the counter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.domain.money import Money, total
from app.domain.pricing import (
    Discount,
    LineInput,
    PricedLine,
    price_line,
    sum_lines,
)
from app.domain.tax import TaxComponent, breakdown
from app.domain.tender import CashRounding, TenderMethod, no_rounding, rounding_for


class CartError(ValueError):
    """An operation was not valid for this cart."""


@dataclass(frozen=True, slots=True)
class Cart:
    """A basket of priced lines.

    Line numbers are 1-based and stable: voiding line 2 of four leaves the
    others as 1, 3 and 4. A cashier reading a number off the screen to a
    supervisor must be reading the same number the audit row will carry.
    """

    lines: tuple[PricedLine, ...] = ()
    #: Monotonic, so a voided line's number is never handed out again.
    next_line_no: int = 1
    line_numbers: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if len(self.lines) != len(self.line_numbers):
            raise CartError("every line must carry a line number")

    # ── Building ────────────────────────────────────────────────────────────

    def add(self, line: LineInput) -> Cart:
        """Price a line and append it."""
        return replace(
            self,
            lines=(*self.lines, price_line(line)),
            line_numbers=(*self.line_numbers, self.next_line_no),
            next_line_no=self.next_line_no + 1,
        )

    def void_line(self, line_no: int) -> Cart:
        """Remove a line. Requires `sale.void` at the layer above."""
        index = self._index_of(line_no)
        return replace(
            self,
            lines=self.lines[:index] + self.lines[index + 1 :],
            line_numbers=self.line_numbers[:index] + self.line_numbers[index + 1 :],
        )

    def change_quantity(self, line_no: int, qty_milli: int) -> Cart:
        """Re-price a line at a new quantity.

        The unit price is *not* re-read from the catalogue: it was snapshotted
        at scan time so a mid-transaction price sync cannot change what the
        customer was quoted (architecture §6).
        """
        return self._replace_line(
            line_no, lambda current: replace(current.line, qty_milli=qty_milli)
        )

    def apply_discount(self, line_no: int, discount: Discount) -> Cart:
        """Add a discount to a line and re-price it."""
        return self._replace_line(
            line_no,
            lambda current: replace(
                current.line, discounts=(*current.line.discounts, discount)
            ),
        )

    def _replace_line(
        self, line_no: int, build: Callable[[PricedLine], LineInput]
    ) -> Cart:
        index = self._index_of(line_no)
        updated = price_line(build(self.lines[index]))
        return replace(
            self, lines=(*self.lines[:index], updated, *self.lines[index + 1 :])
        )

    def _index_of(self, line_no: int) -> int:
        try:
            return self.line_numbers.index(line_no)
        except ValueError:
            raise CartError(f"no line {line_no} in this cart") from None

    # ── Inspection ──────────────────────────────────────────────────────────

    @property
    def is_empty(self) -> bool:
        return not self.lines

    @property
    def item_count(self) -> int:
        """Number of lines, not units — what the cashier sees on screen."""
        return len(self.lines)

    def line(self, line_no: int) -> PricedLine:
        return self.lines[self._index_of(line_no)]

    def numbered(self) -> tuple[tuple[int, PricedLine], ...]:
        return tuple(zip(self.line_numbers, self.lines, strict=True))

    # ── Totals ──────────────────────────────────────────────────────────────

    @property
    def subtotal(self) -> Money:
        """Gross of every line, before discounts. GST-inclusive."""
        return sum_lines(self.lines).gross

    @property
    def discount_total(self) -> Money:
        return sum_lines(self.lines).discount_total

    @property
    def tax_total(self) -> Money:
        return sum_lines(self.lines).tax_total

    @property
    def taxable_base(self) -> Money:
        return sum_lines(self.lines).taxable_base

    @property
    def total_before_rounding(self) -> Money:
        """What is owed before any tender is chosen.

        This is the honest total: it is what a UPI customer pays to the paisa,
        and it is the figure every rounding adjustment reconciles against.
        """
        return sum_lines(self.lines).net

    @property
    def tax_breakdown(self) -> list[TaxComponent]:
        """The GST block on the receipt, one row per rate.

        Built from discounted line totals, so it reflects what was actually
        charged rather than what was on the shelf edge.
        """
        return breakdown(
            [(line.tax_code, line.line_total) for line in self.lines]
        )

    # ── Tender ──────────────────────────────────────────────────────────────

    def balance(self, paid: Money) -> Money:
        """What is still owed after approved payments (architecture §13.2)."""
        return self.total_before_rounding - paid

    def rounding_for_tender(
        self, method: TenderMethod, already_paid: Money | None = None
    ) -> CashRounding:
        """The rounding adjustment for paying the remaining balance this way.

        On a split tender, pass what has already been approved: only the
        portion actually being handed over in cash is rounded (§13.4).
        """
        outstanding = self.balance(already_paid or Money.zero())
        if self.is_empty:
            return no_rounding(outstanding)
        return rounding_for(outstanding, method)


def cart_of(lines: tuple[LineInput, ...]) -> Cart:
    """Build a cart from line inputs in one go. Convenience for tests."""
    cart = Cart()
    for line in lines:
        cart = cart.add(line)
    return cart


def units_of(lines: tuple[PricedLine, ...]) -> int:
    """Total quantity in thousandths across every line."""
    return sum(line.line.qty_milli for line in lines)


def gross_of(lines: tuple[PricedLine, ...]) -> Money:
    return total([line.line_total for line in lines])

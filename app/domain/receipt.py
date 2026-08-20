"""The receipt document — architecture §13.4, execution plan phase 3.

A pure description of what a receipt says. Two renderers consume it (screen
and PDF) and neither computes anything: every figure here was settled by the
pricing engine and the tender boundary, so the two renderings can never
disagree about a total.

Receipt delivery in v1 is **on screen always, PDF on demand**, with no
customer contact details captured (execution plan §2). A PDF is also the
prerequisite for any later sharing — you cannot send a document you have not
rendered — so this model is deliberately delivery-agnostic.

The **rounding line is not optional**. A cash sale that collects ₹123 against a
₹123.40 total must show where the 40 paise went, or the customer cannot check
the arithmetic and the shift-close variance cannot be explained (§13.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.money import Money
from app.domain.tax import TaxComponent
from app.domain.tender import TenderMethod


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    line_no: int
    description: str
    qty_milli: int
    uom: str
    unit_price: Money
    discount: Money
    line_total: Money

    @property
    def quantity_text(self) -> str:
        """Quantity as a person reads it.

        Whole units print as an integer — "3", not "3.000" — because a receipt
        for three packets of biscuits saying 3.000 looks like a machine that
        does not understand biscuits.
        """
        if self.qty_milli % 1000 == 0:
            return str(self.qty_milli // 1000)
        return f"{self.qty_milli / 1000:.3f}".rstrip("0")

    @property
    def has_discount(self) -> bool:
        return not self.discount.is_zero


@dataclass(frozen=True, slots=True)
class ReceiptPayment:
    method: TenderMethod
    amount: Money
    #: False for a UPI receipt the cashier attested but settlement has not
    #: confirmed (architecture §13.5). Always True for cash.
    verified: bool = True
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class Receipt:
    """Everything printed, in the order it is printed."""

    receipt_no: str
    sale_id: str
    issued_at: datetime

    store_name: str
    store_gstin: str | None
    terminal_code: str
    cashier_name: str

    lines: tuple[ReceiptLine, ...]
    tax_components: tuple[TaxComponent, ...]

    subtotal: Money
    discount_total: Money
    total_before_rounding: Money
    rounding_adjustment: Money
    grand_total: Money

    payments: tuple[ReceiptPayment, ...] = ()
    change_due: Money = field(default_factory=Money.zero)
    is_reprint: bool = False
    requires_review: bool = False

    @property
    def item_count(self) -> int:
        return len(self.lines)

    @property
    def tax_total(self) -> Money:
        from app.domain.money import total as sum_money

        return sum_money([component.tax_amount for component in self.tax_components])

    @property
    def taxable_total(self) -> Money:
        from app.domain.money import total as sum_money

        return sum_money([component.taxable_base for component in self.tax_components])

    @property
    def shows_rounding(self) -> bool:
        """Whether the rounding line is printed.

        Printed whenever it is non-zero. A ₹0.00 rounding line on every UPI
        receipt would be noise, and its absence is itself informative: a UPI
        sale carries no cash rounding (phase 4 exit criteria).
        """
        return not self.rounding_adjustment.is_zero

    @property
    def has_unverified_payment(self) -> bool:
        return any(not payment.verified for payment in self.payments)

    def reconciles(self) -> bool:
        """Every figure on this receipt agrees with every other.

        Asserted before a receipt is rendered, so a document that does not add
        up is never handed to a customer.
        """
        if self.subtotal - self.discount_total != self.total_before_rounding:
            return False
        if self.total_before_rounding + self.rounding_adjustment != self.grand_total:
            return False
        return self.taxable_total + self.tax_total == self.total_before_rounding


class ReceiptError(ValueError):
    """A receipt was assembled from figures that do not agree."""

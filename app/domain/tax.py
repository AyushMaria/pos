"""GST — architecture §6, execution plan §2.

Pricing is **GST-inclusive**: the number on the shelf edge is the number the
customer pays, and the tax is already inside it. So tax is *extracted* from a
line total rather than added to it:

    tax  = gross x rate / (10000 + rate)      rounded HALF_UP, once
    base = gross - tax                        exact, by subtraction

Taking the base by subtraction rather than by a second division is what makes
`base + tax == gross` hold for every input with no residue. Dividing twice and
rounding twice produces receipts whose lines do not add up to their own total,
which is the defect the week-5 risk checkpoint exists to catch.

Rates are basis points: 18% GST is 1800. An integer, so no rate ever needs a
float to express.

**CGST/SGST.** An intra-state retail invoice must show the tax halved into
central and state components. The halves are allocated rather than divided, so
an odd number of paise lands somewhere deterministic instead of vanishing.
Inter-state (IGST) is a single component and does not arise for a single-store
till, but the shape here leaves room for it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.domain.money import Money, allocate, quantize_to_paise, total

BASIS_POINTS = 10_000


class TaxError(ValueError):
    """A tax rate or code was invalid."""


@dataclass(frozen=True, slots=True)
class TaxCode:
    """A GST rate as the catalogue holds it."""

    code: str
    name: str
    rate_bp: int
    is_inclusive: bool = True

    def __post_init__(self) -> None:
        if type(self.rate_bp) is not int:
            raise TaxError(f"rate must be integer basis points, got {self.rate_bp!r}")
        if self.rate_bp < 0:
            raise TaxError(f"negative tax rate: {self.rate_bp}")

    @property
    def percent(self) -> Decimal:
        return Decimal(self.rate_bp) / 100


@dataclass(frozen=True, slots=True)
class TaxComponent:
    """One rate's contribution to a basket, as it appears on the receipt."""

    tax_code: str
    rate_bp: int
    taxable_base: Money
    tax_amount: Money
    cgst: Money
    sgst: Money

    @property
    def gross(self) -> Money:
        return self.taxable_base + self.tax_amount


def extract_inclusive_tax(gross: Money, rate_bp: int) -> Money:
    """The tax already contained within a GST-inclusive amount.

    Rounded once, HALF_UP, at this boundary and nowhere else.
    """
    if type(rate_bp) is not int:
        raise TaxError(f"rate must be integer basis points, got {rate_bp!r}")
    if rate_bp < 0:
        raise TaxError(f"negative tax rate: {rate_bp}")
    if rate_bp == 0:
        return Money.zero()

    return Money(
        quantize_to_paise(Decimal(gross.paise) * rate_bp / (BASIS_POINTS + rate_bp))
    )


def taxable_base(gross: Money, rate_bp: int) -> Money:
    """The pre-tax value inside a GST-inclusive amount.

    By subtraction, so it always reconciles with `extract_inclusive_tax`.
    """
    return gross - extract_inclusive_tax(gross, rate_bp)


def add_exclusive_tax(base: Money, rate_bp: int) -> Money:
    """Tax added on top of a net amount.

    Unused while pricing stays GST-inclusive (execution plan §2 settled that),
    but the pricing engine should not have to be rewritten if a wholesale
    counter ever needs tax-at-tender.
    """
    if rate_bp < 0:
        raise TaxError(f"negative tax rate: {rate_bp}")

    return Money(quantize_to_paise(Decimal(base.paise) * rate_bp / BASIS_POINTS))


def split_cgst_sgst(tax: Money) -> tuple[Money, Money]:
    """Halve a tax amount into central and state components.

    Allocated, not divided: ₹0.05 of tax is 3 paise CGST and 2 paise SGST, and
    the two always sum back to 5. Halving with integer division would quietly
    lose the odd paisa and leave the invoice short.
    """
    cgst, sgst = allocate(tax, [1, 1])
    return cgst, sgst


def component_for(tax_code: TaxCode, gross: Money) -> TaxComponent:
    """One rate's figures for a given GST-inclusive gross amount."""
    tax = extract_inclusive_tax(gross, tax_code.rate_bp)
    cgst, sgst = split_cgst_sgst(tax)
    return TaxComponent(
        tax_code=tax_code.code,
        rate_bp=tax_code.rate_bp,
        taxable_base=gross - tax,
        tax_amount=tax,
        cgst=cgst,
        sgst=sgst,
    )


def breakdown(items: Iterable[tuple[TaxCode, Money]]) -> list[TaxComponent]:
    """The GST summary block on a receipt, one row per rate.

    Lines are grouped by rate and the tax computed **once on each group's
    total**, not summed from per-line tax figures. Both are defensible, and
    they differ by a paisa or two on a mixed basket; grouping first is what an
    Indian GST invoice shows and what an auditor will reconcile against.

    Rows come back in ascending rate order, which is how they are read.
    """
    grouped: dict[str, tuple[TaxCode, Money]] = {}
    for tax_code, gross in items:
        if tax_code.code in grouped:
            existing_code, existing_gross = grouped[tax_code.code]
            if existing_code.rate_bp != tax_code.rate_bp:
                raise TaxError(
                    f"tax code {tax_code.code} appears with two different rates: "
                    f"{existing_code.rate_bp} and {tax_code.rate_bp}"
                )
            grouped[tax_code.code] = (existing_code, existing_gross + gross)
        else:
            grouped[tax_code.code] = (tax_code, gross)

    components = [component_for(code, gross) for code, gross in grouped.values()]
    return sorted(components, key=lambda c: (c.rate_bp, c.tax_code))


def total_tax(components: Iterable[TaxComponent]) -> Money:
    return total([component.tax_amount for component in components])


def total_base(components: Iterable[TaxComponent]) -> Money:
    return total([component.taxable_base for component in components])

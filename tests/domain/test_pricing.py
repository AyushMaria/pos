"""Line pricing and discount stacking — architecture §6."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.money import Money
from app.domain.pricing import (
    Discount,
    LineInput,
    PricingError,
    price_line,
    sum_lines,
)
from app.domain.tax import TaxCode

GST0 = TaxCode("GST0", "GST 0%", 0)
GST5 = TaxCode("GST5", "GST 5%", 500)
GST12 = TaxCode("GST12", "GST 12%", 1200)
GST18 = TaxCode("GST18", "GST 18%", 1800)


def line(
    unit_paise: int = 10000,
    qty_milli: int = 1000,
    tax_code: TaxCode = GST18,
    discounts: tuple[Discount, ...] = (),
) -> LineInput:
    return LineInput(
        product_id="p1",
        description="Test product",
        unit_price=Money(unit_paise),
        qty_milli=qty_milli,
        tax_code=tax_code,
        discounts=discounts,
    )


# ── Gross ───────────────────────────────────────────────────────────────────


def test_a_whole_unit() -> None:
    priced = price_line(line(unit_paise=11800, qty_milli=1000))

    assert priced.gross == Money(11800)
    assert priced.line_total == Money(11800)
    assert priced.tax_amount == Money(1800)
    assert priced.taxable_base == Money(10000)


def test_a_weighed_quantity() -> None:
    """1.250 kg at ₹40.00/kg. Thousandths are grams for anything sold by kilo."""
    priced = price_line(line(unit_paise=4000, qty_milli=1250, tax_code=GST0))

    assert priced.gross == Money(5000)
    assert priced.tax_amount.is_zero


def test_several_units() -> None:
    assert price_line(line(unit_paise=2500, qty_milli=3000)).gross == Money(7500)


def test_a_line_needs_a_quantity() -> None:
    with pytest.raises(PricingError, match="not a line"):
        line(qty_milli=0)


def test_a_float_quantity_is_refused() -> None:
    with pytest.raises(PricingError, match="thousandths"):
        line(qty_milli=1.5)  # type: ignore[arg-type]


# ── Discounts ───────────────────────────────────────────────────────────────


def test_a_percentage_discount() -> None:
    priced = price_line(
        line(unit_paise=10000, discounts=(Discount("percentage", 1000),))
    )

    assert priced.discount_total == Money(1000)
    assert priced.line_total == Money(9000)


def test_a_fixed_discount() -> None:
    priced = price_line(line(unit_paise=10000, discounts=(Discount("amount", 1500),)))

    assert priced.line_total == Money(8500)


def test_discounts_stack_on_the_running_amount() -> None:
    """Two 10% discounts on ₹100 give ₹81, not ₹80. Predictable, and explainable."""
    priced = price_line(
        line(
            unit_paise=10000,
            discounts=(Discount("percentage", 1000), Discount("percentage", 1000)),
        )
    )

    assert priced.line_total == Money(8100)


def test_a_manual_discount_applies_after_promotions() -> None:
    """The supervisor authorises the number they can actually see."""
    priced = price_line(
        line(
            unit_paise=10000,
            discounts=(
                Discount("percentage", 5000, source="manual"),
                Discount("percentage", 1000, source="promotion"),
            ),
        )
    )

    # Promotion first: ₹100 -> ₹90. Then manual 50%: -> ₹45.
    assert priced.line_total == Money(4500)
    assert priced.applied[0].discount.source == "promotion"
    assert priced.applied[1].discount.source == "manual"


def test_a_discount_never_makes_a_line_negative() -> None:
    """A misconfigured ₹50-off promotion on a ₹30 item is not a cash giveaway."""
    priced = price_line(line(unit_paise=3000, discounts=(Discount("amount", 5000),)))

    assert priced.line_total == Money.zero()
    assert priced.discount_total == Money(3000)
    assert priced.applied[0].amount == Money(3000)


def test_a_hundred_percent_discount_zeroes_the_line() -> None:
    priced = price_line(
        line(unit_paise=10000, discounts=(Discount("percentage", 10_000),))
    )

    assert priced.line_total.is_zero
    assert priced.tax_amount.is_zero


def test_a_zero_discount_is_not_recorded() -> None:
    """A 0% promotion should not clutter the receipt with a ₹0.00 line."""
    priced = price_line(line(discounts=(Discount("percentage", 0),)))

    assert priced.applied == ()


def test_a_negative_discount_is_refused() -> None:
    with pytest.raises(PricingError, match="price increase"):
        Discount("amount", -100)


def test_a_discount_over_one_hundred_percent_is_refused() -> None:
    with pytest.raises(PricingError, match="exceed 100%"):
        Discount("percentage", 10_001)


def test_a_float_discount_is_refused() -> None:
    with pytest.raises(PricingError):
        Discount("percentage", 10.0)  # type: ignore[arg-type]


# ── Tax on the discounted amount ────────────────────────────────────────────


def test_tax_is_taken_on_the_discounted_total() -> None:
    """GST is due on the consideration actually received, not on the shelf price."""
    priced = price_line(
        line(unit_paise=11800, discounts=(Discount("percentage", 5000),))
    )

    assert priced.line_total == Money(5900)
    assert priced.tax_amount == Money(900)
    assert priced.taxable_base == Money(5000)


@given(
    st.integers(min_value=1, max_value=10_000_000),
    st.integers(min_value=1, max_value=100_000),
    st.sampled_from([GST0, GST5, GST12, GST18]),
    st.integers(min_value=0, max_value=10_000),
)
def test_a_line_always_reconciles(
    unit_paise: int, qty_milli: int, tax_code: TaxCode, discount_bp: int
) -> None:
    """Whatever the inputs: base + tax == line total, and the line is not negative."""
    priced = price_line(
        line(
            unit_paise=unit_paise,
            qty_milli=qty_milli,
            tax_code=tax_code,
            discounts=(Discount("percentage", discount_bp),),
        )
    )

    assert priced.taxable_base + priced.tax_amount == priced.line_total
    assert not priced.line_total.is_negative
    assert priced.gross - priced.discount_total == priced.line_total


@given(st.integers(min_value=1, max_value=1_000_000), st.integers(min_value=1, max_value=50_000))
def test_discount_never_exceeds_gross(unit_paise: int, amount_off: int) -> None:
    priced = price_line(
        line(unit_paise=unit_paise, discounts=(Discount("amount", amount_off),))
    )
    assert priced.discount_total <= priced.gross


# ── Aggregation ─────────────────────────────────────────────────────────────


def test_sum_lines() -> None:
    lines = (
        price_line(line(unit_paise=10000, tax_code=GST18)),
        price_line(line(unit_paise=5000, tax_code=GST5)),
    )
    totals = sum_lines(lines)

    assert totals.gross == Money(15000)
    assert totals.net == Money(15000)
    assert totals.taxable_base + totals.tax_total == totals.net


def test_sum_of_nothing_is_zero() -> None:
    totals = sum_lines(())

    assert totals.gross.is_zero
    assert totals.net.is_zero
    assert totals.tax_total.is_zero

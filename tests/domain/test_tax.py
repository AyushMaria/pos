"""GST — architecture §6, risk checkpoint at week 5.

The defect this suite exists to prevent: a receipt whose GST lines do not add
up to its own total. It is invisible in development, obvious to an auditor,
and expensive to find at a pilot.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.money import Money, total
from app.domain.tax import (
    TaxCode,
    TaxError,
    add_exclusive_tax,
    breakdown,
    component_for,
    extract_inclusive_tax,
    split_cgst_sgst,
    taxable_base,
    total_base,
    total_tax,
)

GST0 = TaxCode("GST0", "GST 0%", 0)
GST5 = TaxCode("GST5", "GST 5%", 500)
GST12 = TaxCode("GST12", "GST 12%", 1200)
GST18 = TaxCode("GST18", "GST 18%", 1800)

RATES = [0, 500, 1200, 1800, 2800]

amounts = st.integers(min_value=0, max_value=100_000_000).map(Money)
rates = st.sampled_from(RATES)


# ── Extraction ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "gross,rate_bp,tax",
    [
        (11800, 1800, 1800),   # ₹118 inclusive of 18% -> ₹18 tax, ₹100 base
        (10500, 500, 500),     # ₹105 inclusive of 5%  -> ₹5 tax
        (11200, 1200, 1200),
        (10000, 0, 0),         # zero-rated
        (2500, 1200, 268),     # ₹25 at 12% -> 267.857 -> 268 HALF_UP
        (1, 1800, 0),          # one paisa: tax rounds to nothing
    ],
)
def test_extract_inclusive_tax(gross: int, rate_bp: int, tax: int) -> None:
    assert extract_inclusive_tax(Money(gross), rate_bp).paise == tax


@given(amounts, rates)
def test_base_and_tax_always_reconcile(gross: Money, rate_bp: int) -> None:
    """The property the whole receipt depends on."""
    tax = extract_inclusive_tax(gross, rate_bp)
    assert taxable_base(gross, rate_bp) + tax == gross


@given(amounts, rates)
def test_tax_never_exceeds_the_gross(gross: Money, rate_bp: int) -> None:
    assert Money.zero() <= extract_inclusive_tax(gross, rate_bp) <= gross


def test_zero_rate_means_no_tax() -> None:
    assert extract_inclusive_tax(Money(50000), 0).is_zero
    assert taxable_base(Money(50000), 0) == Money(50000)


def test_a_negative_rate_is_refused() -> None:
    with pytest.raises(TaxError, match="negative tax rate"):
        extract_inclusive_tax(Money(1000), -100)


def test_a_float_rate_is_refused() -> None:
    with pytest.raises(TaxError, match="basis points"):
        extract_inclusive_tax(Money(1000), 18.0)  # type: ignore[arg-type]


@given(amounts, rates)
def test_exclusive_tax_is_the_inverse_within_a_paisa(base: Money, rate_bp: int) -> None:
    """Adding tax to a base then extracting it returns where it started."""
    gross = base + add_exclusive_tax(base, rate_bp)
    assert abs((taxable_base(gross, rate_bp) - base).paise) <= 1


# ── CGST / SGST ─────────────────────────────────────────────────────────────


@given(amounts)
def test_cgst_and_sgst_always_sum_to_the_tax(tax: Money) -> None:
    cgst, sgst = split_cgst_sgst(tax)
    assert cgst + sgst == tax


def test_an_odd_paisa_is_not_lost_in_the_split() -> None:
    """Integer division would silently leave the invoice a paisa short."""
    cgst, sgst = split_cgst_sgst(Money(5))
    assert (cgst.paise, sgst.paise) == (3, 2)


def test_the_split_is_even_when_it_can_be() -> None:
    assert split_cgst_sgst(Money(1800)) == (Money(900), Money(900))


# ── The receipt block ───────────────────────────────────────────────────────


def test_component_for_a_single_rate() -> None:
    component = component_for(GST18, Money(11800))

    assert component.tax_amount == Money(1800)
    assert component.taxable_base == Money(10000)
    assert component.cgst == Money(900)
    assert component.sgst == Money(900)
    assert component.gross == Money(11800)


def test_mixed_rate_basket() -> None:
    """The week-5 risk checkpoint, worked by hand.

    A real kirana basket: milk at 0%, atta at 5%, biscuits at 12%, toothpaste
    at 18%. Every figure below is independently checkable with a calculator,
    which is the point — this is the test an owner can be walked through.
    """
    basket = [
        (GST0, Money(3300)),    # ₹33.00 milk        -> ₹0.00 tax
        (GST5, Money(27500)),   # ₹275.00 atta       -> ₹13.10 tax
        (GST12, Money(2500)),   # ₹25.00 biscuits    -> ₹2.68 tax
        (GST18, Money(11500)),  # ₹115.00 toothpaste -> ₹17.54 tax
    ]

    components = breakdown(basket)
    by_code = {component.tax_code: component for component in components}

    assert by_code["GST0"].tax_amount == Money(0)
    assert by_code["GST5"].tax_amount == Money(1310)
    assert by_code["GST12"].tax_amount == Money(268)
    assert by_code["GST18"].tax_amount == Money(1754)

    gross = total([amount for _, amount in basket])
    assert total_base(components) + total_tax(components) == gross
    assert gross == Money(44800)  # ₹448.00


def test_rows_are_ordered_by_rate() -> None:
    components = breakdown(
        [(GST18, Money(1000)), (GST0, Money(1000)), (GST12, Money(1000))]
    )
    assert [c.rate_bp for c in components] == [0, 1200, 1800]


def test_lines_sharing_a_rate_are_grouped_once() -> None:
    """Tax is computed on the group total, not summed from per-line figures.

    Three ₹25 biscuit packets at 12%: 12% of ₹75 is ₹8.04, whereas three
    separately-rounded ₹2.68 figures come to ₹8.04 as well here — but the
    grouping is what an auditor reconciles, so it is what is asserted.
    """
    components = breakdown([(GST12, Money(2500))] * 3)

    assert len(components) == 1
    assert components[0].gross == Money(7500)
    assert components[0].tax_amount == extract_inclusive_tax(Money(7500), 1200)


@given(
    st.lists(
        st.tuples(st.sampled_from([GST0, GST5, GST12, GST18]), amounts),
        min_size=1,
        max_size=30,
    )
)
def test_a_breakdown_always_reconciles_against_the_basket(
    basket: list[tuple[TaxCode, Money]],
) -> None:
    """For any basket at all: base + tax == what the customer pays."""
    components = breakdown(basket)
    gross = total([amount for _, amount in basket])

    assert total_base(components) + total_tax(components) == gross


@given(
    st.lists(
        st.tuples(st.sampled_from([GST0, GST5, GST12, GST18]), amounts),
        min_size=1,
        max_size=30,
    )
)
def test_cgst_and_sgst_reconcile_across_a_whole_basket(
    basket: list[tuple[TaxCode, Money]],
) -> None:
    components = breakdown(basket)

    cgst = total([c.cgst for c in components])
    sgst = total([c.sgst for c in components])
    assert cgst + sgst == total_tax(components)


def test_one_code_with_two_rates_is_refused() -> None:
    """A catalogue that redefines GST5 mid-basket is corrupt, not creative."""
    with pytest.raises(TaxError, match="two different rates"):
        breakdown([(GST5, Money(1000)), (TaxCode("GST5", "GST 5%", 1800), Money(1000))])


def test_an_empty_basket_has_no_rows() -> None:
    assert breakdown([]) == []


def test_tax_code_rejects_a_float_rate() -> None:
    with pytest.raises(TaxError):
        TaxCode("BAD", "bad", 18.0)  # type: ignore[arg-type]


def test_tax_code_percent_is_exact() -> None:
    from decimal import Decimal

    assert GST18.percent == Decimal("18")
    assert TaxCode("GST2_5", "GST 2.5%", 250).percent == Decimal("2.5")

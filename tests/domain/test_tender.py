"""Cash rounding at the tender boundary — architecture §13.4.

Phase 2 exit criterion: *a property test proves rounding adjustments never
exceed ₹0.50 and always reconcile against the unrounded total.* Both live
here.

The failure this prevents is specific and nasty: a rounding adjustment folded
silently into the total produces a shift-close variance that nobody can
explain, every single day, until someone decides the cashier is stealing.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.money import Money
from app.domain.tender import (
    MAX_ADJUSTMENT,
    TenderError,
    change_due,
    no_rounding,
    round_cash_tender,
    round_to_rupee,
    rounding_for,
)

payable = st.integers(min_value=0, max_value=100_000_000).map(Money)


# ── The exit criteria ───────────────────────────────────────────────────────


@given(payable)
def test_an_adjustment_never_exceeds_fifty_paise(outstanding: Money) -> None:
    """Phase 2 exit criterion, first half."""
    result = round_cash_tender(outstanding)

    assert abs(result.adjustment) <= MAX_ADJUSTMENT
    assert MAX_ADJUSTMENT == Money(50)


@given(payable)
def test_rounding_always_reconciles_against_the_unrounded_total(
    outstanding: Money,
) -> None:
    """Phase 2 exit criterion, second half.

    The adjustment is not a fudge factor: it is exactly the difference between
    what was owed and what was collected, and it always closes the gap.
    """
    result = round_cash_tender(outstanding)

    assert result.outstanding + result.adjustment == result.collected


@given(payable)
def test_the_collected_amount_is_always_whole_rupees(outstanding: Money) -> None:
    assert round_cash_tender(outstanding).collected.paise % 100 == 0


# ── Worked examples ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "outstanding,collected,adjustment",
    [
        (12340, 12300, -40),   # rounds down, in the customer's favour
        (12360, 12400, 40),    # rounds up
        (12350, 12400, 50),    # exactly half rounds up, HALF_UP
        (12300, 12300, 0),     # already whole rupees
        (49, 0, -49),          # under fifty paise collects nothing
        (50, 100, 50),         # the largest possible adjustment
        (0, 0, 0),
    ],
)
def test_worked_examples(outstanding: int, collected: int, adjustment: int) -> None:
    result = round_cash_tender(Money(outstanding))

    assert result.collected == Money(collected)
    assert result.adjustment == Money(adjustment)


def test_round_to_rupee() -> None:
    assert round_to_rupee(Money(12349)) == Money(12300)
    assert round_to_rupee(Money(12350)) == Money(12400)
    assert round_to_rupee(Money(12351)) == Money(12400)


def test_a_negative_amount_cannot_be_rounded() -> None:
    with pytest.raises(TenderError, match="negative amount"):
        round_cash_tender(Money(-100))


# ── Tender dependence ───────────────────────────────────────────────────────


@given(payable)
def test_upi_never_rounds(outstanding: Money) -> None:
    """A transfer has no coins. ₹123.40 over UPI is ₹123.40 (architecture §13.4)."""
    result = rounding_for(outstanding, "upi")

    assert result.adjustment.is_zero
    assert result.collected == outstanding


@given(payable)
def test_cash_and_upi_disagree_only_by_the_adjustment(outstanding: Money) -> None:
    cash = rounding_for(outstanding, "cash")
    upi = rounding_for(outstanding, "upi")

    assert cash.collected - upi.collected == cash.adjustment


def test_no_rounding_reconciles_too() -> None:
    result = no_rounding(Money(12340))

    assert result.collected == Money(12340)
    assert result.adjustment.is_zero


def test_a_split_tender_rounds_only_the_cash_portion() -> None:
    """₹123.40 total, ₹100 by UPI, the rest in cash.

    The cash portion is ₹23.40, collected as ₹23. The adjustment is -₹0.40 —
    computed on what is actually handed over, not on the basket.
    """
    grand_total = Money(12340)
    paid_by_upi = Money(10000)

    result = round_cash_tender(grand_total - paid_by_upi)

    assert result.outstanding == Money(2340)
    assert result.collected == Money(2300)
    assert result.adjustment == Money(-40)
    assert paid_by_upi + result.collected == grand_total + result.adjustment


@given(payable, payable)
def test_a_split_tender_always_reconciles(grand_total: Money, paid: Money) -> None:
    """However the basket is split, the books close."""
    outstanding = grand_total - paid
    if outstanding.is_negative:
        return

    result = round_cash_tender(outstanding)

    assert paid + result.collected == grand_total + result.adjustment


# ── Change ──────────────────────────────────────────────────────────────────


def test_change_due() -> None:
    assert change_due(Money(50000), Money(12300)) == Money(37700)


def test_exact_money_leaves_no_change() -> None:
    assert change_due(Money(12300), Money(12300)).is_zero


def test_underpayment_is_not_change() -> None:
    """The balance loop is not finished; this is a bug, not a discount."""
    with pytest.raises(TenderError, match="does not cover"):
        change_due(Money(10000), Money(12300))

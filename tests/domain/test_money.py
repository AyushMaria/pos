"""Money — architecture §6, §17.

The highest-value test suite in the project, per architecture §6: no fixtures,
no database, milliseconds to run. Hypothesis does the work that hand-written
examples cannot, which is proving that the arithmetic reconciles for *every*
input rather than the six someone thought of.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from app.domain.money import (
    Money,
    MoneyError,
    allocate,
    multiply_by_quantity,
    percentage_of,
    total,
)

# Realistic till amounts: up to ₹10 lakh, and negatives for refunds.
money = st.integers(min_value=-100_000_000, max_value=100_000_000).map(Money)
positive_money = st.integers(min_value=0, max_value=100_000_000).map(Money)
quantities = st.integers(min_value=1, max_value=1_000_000)


# ── Construction ────────────────────────────────────────────────────────────


def test_paise_are_whole() -> None:
    assert Money(1999).paise == 1999
    assert Money.zero().is_zero


def test_a_float_cannot_become_money() -> None:
    """The bug this whole type exists to prevent."""
    with pytest.raises(MoneyError, match="whole paise"):
        Money(19.99)  # type: ignore[arg-type]


def test_a_bool_cannot_become_money() -> None:
    """bool is an int subclass, so this would otherwise quietly be ₹0.01."""
    with pytest.raises(MoneyError):
        Money(True)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "rupees,paise",
    [("19.99", 1999), ("0.01", 1), ("1234.50", 123450), ("-5.25", -525), (7, 700)],
)
def test_from_rupees(rupees: str | int, paise: int) -> None:
    assert Money.from_rupees(rupees).paise == paise


def test_from_rupees_refuses_a_float() -> None:
    with pytest.raises(MoneyError, match=re.escape("Pass '19.99'")):
        Money.from_rupees(19.99)  # type: ignore[arg-type]


def test_from_rupees_refuses_fractional_paise() -> None:
    with pytest.raises(MoneyError, match="not a whole number of paise"):
        Money.from_rupees("1.005")


# ── Exact arithmetic ────────────────────────────────────────────────────────


@given(money, money)
def test_addition_is_exact(a: Money, b: Money) -> None:
    assert (a + b).paise == a.paise + b.paise


@given(money, money)
def test_subtraction_inverts_addition(a: Money, b: Money) -> None:
    assert a + b - b == a


@given(money, st.integers(min_value=-1000, max_value=1000))
def test_whole_number_multiplication_is_exact(a: Money, n: int) -> None:
    assert (a * n).paise == a.paise * n
    assert n * a == a * n


def test_multiplying_by_a_float_is_refused() -> None:
    with pytest.raises(MoneyError, match="whole number"):
        Money(1000) * 1.5  # type: ignore[operator]


@given(st.lists(money, max_size=50))
def test_total_reconciles(amounts: list[Money]) -> None:
    assert total(amounts).paise == sum(a.paise for a in amounts)


def test_total_of_nothing_is_zero() -> None:
    assert total([]) == Money.zero()


@given(money, money)
def test_ordering_follows_value(a: Money, b: Money) -> None:
    assert (a < b) == (a.paise < b.paise)


# ── Rounding operations ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "unit_paise,qty_milli,expected",
    [
        (4000, 1250, 5000),      # 1.250 kg at ₹40/kg — exact
        (4000, 1000, 4000),      # one kilo
        (2500, 1, 3),            # 0.001 kg at ₹25/kg = 2.5 paise -> 3
        (100, 5, 1),             # 0.5 paise -> 1, HALF_UP not banker's
        (100, 15, 2),            # 1.5 paise -> 2, HALF_UP not banker's
        (3300, 3000, 9900),      # three litres of milk
    ],
)
def test_multiply_by_quantity(unit_paise: int, qty_milli: int, expected: int) -> None:
    assert multiply_by_quantity(Money(unit_paise), qty_milli).paise == expected


def test_half_up_never_rounds_to_even() -> None:
    """Python's default is banker's rounding; tax practice is not."""
    assert multiply_by_quantity(Money(100), 5).paise == 1   # 0.5 -> 1
    assert multiply_by_quantity(Money(100), 15).paise == 2  # 1.5 -> 2 (not 2)
    assert multiply_by_quantity(Money(100), 25).paise == 3  # 2.5 -> 3 (not 2)


@given(positive_money, quantities)
def test_scaling_never_strays_more_than_half_a_paisa(price: Money, qty: int) -> None:
    exact = Decimal(price.paise) * qty / 1000
    assert abs(Decimal(multiply_by_quantity(price, qty).paise) - exact) <= Decimal("0.5")


@pytest.mark.parametrize(
    "paise,bp,expected",
    [
        (10000, 1000, 1000),   # 10% of ₹100
        (10000, 1250, 1250),   # 12.5% — expressible without a float
        (9999, 1000, 1000),    # 999.9 paise -> 1000
        (10000, 0, 0),
        (10000, 10000, 10000), # 100%
    ],
)
def test_percentage_of(paise: int, bp: int, expected: int) -> None:
    assert percentage_of(Money(paise), bp).paise == expected


# ── Allocation ──────────────────────────────────────────────────────────────


@given(money, st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=20))
def test_allocation_always_reconciles(amount: Money, weights: list[int]) -> None:
    """The property that keeps a GST breakdown adding up to its own total."""
    assert total(allocate(amount, weights)) == amount


@given(positive_money, st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=10))
def test_allocation_is_proportional_within_a_paisa(
    amount: Money, weights: list[int]
) -> None:
    shares = allocate(amount, weights)
    total_weight = sum(weights)
    for share, weight in zip(shares, weights, strict=True):
        exact = Decimal(amount.paise) * weight / total_weight
        assert abs(Decimal(share.paise) - exact) < 1


def test_allocation_hands_leftovers_to_the_largest_remainder() -> None:
    """₹1.00 across three equal ways: 34/33/33, not 33/33/33 losing a paisa."""
    assert [m.paise for m in allocate(Money(100), [1, 1, 1])] == [34, 33, 33]


def test_allocation_is_deterministic() -> None:
    """The same basket must always produce the same receipt."""
    for _ in range(5):
        assert [m.paise for m in allocate(Money(10), [1, 1, 1])] == [4, 3, 3]


def test_allocation_of_a_negative_amount() -> None:
    """A basket-level refund splits the same way, in the same direction."""
    shares = allocate(Money(-100), [1, 1, 1])
    assert total(shares) == Money(-100)
    assert [m.paise for m in shares] == [-34, -33, -33]


def test_allocation_with_zero_weights_keeps_the_money() -> None:
    shares = allocate(Money(500), [0, 0])
    assert total(shares) == Money(500)


def test_allocation_needs_weights() -> None:
    with pytest.raises(MoneyError):
        allocate(Money(100), [])


def test_allocation_refuses_negative_weights() -> None:
    with pytest.raises(MoneyError):
        allocate(Money(100), [1, -1])


# ── Display ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "paise,rendered",
    [
        (0, "₹0.00"),
        (5, "₹0.05"),
        (1999, "₹19.99"),
        (123450, "₹1,234.50"),
        (-525, "-₹5.25"),
        (10000000, "₹100,000.00"),
    ],
)
def test_display(paise: int, rendered: str) -> None:
    assert str(Money(paise)) == rendered


@given(money)
def test_rupees_is_exact(amount: Money) -> None:
    assert amount.rupees * 100 == Decimal(amount.paise)


@given(money)
def test_money_survives_a_round_trip_through_rupees(amount: Money) -> None:
    assume(amount.paise != 0)
    assert Money.from_rupees(amount.rupees) == amount

"""Shift arithmetic — the properties phase 8 rests on.

The plan says it plainly: if the variance is ₹3 wrong every day, the owner
will conclude the cashier is stealing ₹3 a day. So the two properties here
are generated over whole days, not hand-picked examples, and each one is
stated in terms of the tender code that phase 4 already proved — the close
must agree with the drawer *because* it agrees with how the cash was taken.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.money import Money
from app.domain.shift import (
    CashMovementRow,
    PaymentRow,
    SaleRow,
    ShiftError,
    figures,
)
from app.domain.tender import round_cash_tender

# ── Building a day ──────────────────────────────────────────────────────────

Basket = tuple[int, int, str]  # (total_paise, upi_paise, status)

baskets = st.tuples(
    st.integers(min_value=100, max_value=5_000_00),  # ₹1 to ₹5,000
    st.integers(min_value=0, max_value=5_000_00),
    st.sampled_from(["completed", "completed", "completed", "requires_review", "voided"]),
).map(lambda t: (t[0], min(t[1], t[0]), t[2]))

movements = st.lists(
    st.tuples(st.sampled_from(["in", "out"]), st.integers(min_value=100, max_value=50_000)),
    max_size=5,
)


def a_day(baskets_: list[Basket], review_outcomes: list[str | None]):
    """Turn generated baskets into the rows the close reads.

    Mirrors phase 4 exactly: the UPI portion is paid first at face value; the
    cash remainder is rounded with `round_cash_tender`, and the sale's
    `rounding_adjustment` is that adjustment. A UPI-only sale carries zero.
    """
    sales: list[SaleRow] = []
    payments: list[PaymentRow] = []
    expected_cash_by_hand = 0
    expected_rounding_by_hand = 0

    paired = zip(baskets_, review_outcomes, strict=False)
    for i, ((total, upi, status), outcome) in enumerate(paired):
        sale_id = f"s{i}"
        cash_owed = Money(total - upi)
        if cash_owed.is_positive:
            rounding = round_cash_tender(cash_owed)
            cash_paid = rounding.collected
            adjustment = rounding.adjustment
        else:
            cash_paid = Money.zero()
            adjustment = Money.zero()

        review = outcome if status == "requires_review" else None
        sales.append(
            SaleRow(
                sale_id=sale_id,
                status=status,
                grand_total=Money(total) + adjustment,
                rounding_adjustment=adjustment,
                review_outcome=review,
            )
        )
        if upi:
            payments.append(PaymentRow(sale_id, "upi", Money(upi)))
        if cash_paid.is_positive:
            payments.append(PaymentRow(sale_id, "cash", cash_paid))

        counts = status == "completed" or (status == "requires_review" and review == "paid")
        if counts:
            expected_cash_by_hand += cash_paid.paise
            expected_rounding_by_hand += adjustment.paise

    return sales, payments, expected_cash_by_hand, expected_rounding_by_hand


# ── The two properties ──────────────────────────────────────────────────────


@given(
    baskets_=st.lists(baskets, min_size=1, max_size=40),
    outcomes=st.lists(st.sampled_from([None, "paid", "not_paid"]), min_size=40, max_size=40),
    movements_=movements,
    float_=st.integers(min_value=0, max_value=10_000_00),
)
def test_expected_cash_is_the_float_plus_cash_taken_plus_movements(
    baskets_, outcomes, movements_, float_
) -> None:
    """`expected_cash - float - cash_in + cash_out` equals the sum of cash
    payments on counting sales, to the paisa. No special cases."""
    sales, payments, cash_by_hand, _ = a_day(baskets_, outcomes)
    moves = [CashMovementRow(d, Money(a)) for d, a in movements_]

    f = figures(
        opening_float=Money(float_), sales=sales, payments=payments, movements=moves
    )

    cash_in = sum(a for d, a in movements_ if d == "in")
    cash_out = sum(a for d, a in movements_ if d == "out")
    assert f.cash_sales.paise == cash_by_hand
    assert f.expected_cash.paise == float_ + cash_by_hand + cash_in - cash_out
    assert f.variance(f.expected_cash) == Money.zero()


@given(
    baskets_=st.lists(baskets, min_size=1, max_size=40),
    outcomes=st.lists(st.sampled_from([None, "paid", "not_paid"]), min_size=40, max_size=40),
)
def test_rounding_is_reported_beside_the_total_never_inside_it(baskets_, outcomes) -> None:
    """The rounding figure equals Σ (collected - owed) over the cash portions
    of counting sales — and `cash_sales` already includes it, so the two are
    never added. Every adjustment is under ₹0.50 (phase 2's property), so the
    figure is bounded by half a rupee per cash sale."""
    sales, payments, _, rounding_by_hand = a_day(baskets_, outcomes)

    f = figures(opening_float=Money.zero(), sales=sales, payments=payments, movements=[])

    assert f.rounding.paise == rounding_by_hand
    cash_sales_count = sum(1 for p in payments if p.method == "cash")
    assert abs(f.rounding.paise) <= 50 * cash_sales_count


# ── The rules, one example each ─────────────────────────────────────────────


def test_a_sale_under_review_is_listed_and_not_counted() -> None:
    sales = [
        SaleRow("a", "completed", Money(10_000), Money.zero()),
        SaleRow("b", "requires_review", Money(20_000), Money.zero()),
    ]
    payments = [PaymentRow("a", "upi", Money(10_000)), PaymentRow("b", "upi", Money(20_000))]

    f = figures(opening_float=Money.zero(), sales=sales, payments=payments, movements=[])

    assert f.upi_attested == Money(10_000)
    assert f.under_review_count == 1
    assert f.under_review_total == Money(20_000)
    assert f.takings == Money(10_000)


def test_a_review_resolved_paid_joins_the_totals_and_not_paid_never_does() -> None:
    paid = SaleRow("p", "requires_review", Money(500), Money.zero(), review_outcome="paid")
    lost = SaleRow("n", "requires_review", Money(700), Money.zero(), review_outcome="not_paid")
    payments = [PaymentRow("p", "upi", Money(500)), PaymentRow("n", "upi", Money(700))]

    f = figures(opening_float=Money.zero(), sales=[paid, lost], payments=payments, movements=[])

    assert f.upi_attested == Money(500)
    assert f.under_review_count == 0


def test_a_voided_sale_counts_for_nothing() -> None:
    sales = [SaleRow("v", "voided", Money(9_900), Money(-40))]
    payments = [PaymentRow("v", "cash", Money(9_900))]

    f = figures(opening_float=Money(100), sales=sales, payments=payments, movements=[])

    assert f.cash_sales == Money.zero()
    assert f.rounding == Money.zero()
    assert f.expected_cash == Money(100)


def test_a_split_tender_lands_on_two_lines_with_rounding_on_the_cash_half() -> None:
    """₹123.40 basket, ₹100 UPI, ₹23.40 cash → ₹23 collected, -40 paise."""
    sale = SaleRow("s", "completed", Money(12_300), Money(-40))
    payments = [PaymentRow("s", "upi", Money(10_000)), PaymentRow("s", "cash", Money(2_300))]

    f = figures(opening_float=Money.zero(), sales=[sale], payments=payments, movements=[])

    assert f.upi_attested == Money(10_000)
    assert f.cash_sales == Money(2_300)
    assert f.rounding == Money(-40)
    assert f.expected_cash == Money(2_300)


def test_verified_and_attested_upi_are_separate_figures() -> None:
    sale = SaleRow("s", "completed", Money(1_000), Money.zero())
    payments = [
        PaymentRow("s", "upi", Money(400), verified=True),
        PaymentRow("s", "upi", Money(600), verified=False),
    ]

    f = figures(opening_float=Money.zero(), sales=[sale], payments=payments, movements=[])

    assert f.upi_verified == Money(400)
    assert f.upi_attested == Money(600)


def test_a_payment_for_an_unknown_sale_is_refused() -> None:
    with pytest.raises(ShiftError, match="unknown sale"):
        figures(
            opening_float=Money.zero(),
            sales=[],
            payments=[PaymentRow("ghost", "cash", Money(1))],
            movements=[],
        )


def test_an_unknown_tender_method_has_no_line_and_says_so() -> None:
    sale = SaleRow("s", "completed", Money(1), Money.zero())
    with pytest.raises(ShiftError, match="card"):
        figures(
            opening_float=Money.zero(),
            sales=[sale],
            payments=[PaymentRow("s", "card", Money(1))],
            movements=[],
        )

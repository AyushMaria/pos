"""The cart — architecture §4, §6.

React never sums a total; these functions do. So the basket has to be right
for the awkward cases as well as the tidy ones: a voided middle line, a
re-priced quantity, a mixed-rate basket at the till.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.cart import Cart, CartError, cart_of
from app.domain.money import Money, total
from app.domain.pricing import Discount, LineInput
from app.domain.tax import TaxCode, total_base, total_tax

GST0 = TaxCode("GST0", "GST 0%", 0)
GST5 = TaxCode("GST5", "GST 5%", 500)
GST12 = TaxCode("GST12", "GST 12%", 1200)
GST18 = TaxCode("GST18", "GST 18%", 1800)


def item(
    product_id: str = "p1",
    unit_paise: int = 10000,
    qty_milli: int = 1000,
    tax_code: TaxCode = GST18,
    description: str = "Item",
) -> LineInput:
    return LineInput(
        product_id=product_id,
        description=description,
        unit_price=Money(unit_paise),
        qty_milli=qty_milli,
        tax_code=tax_code,
    )


# ── Building ────────────────────────────────────────────────────────────────


def test_an_empty_cart() -> None:
    cart = Cart()

    assert cart.is_empty
    assert cart.item_count == 0
    assert cart.subtotal.is_zero
    assert cart.total_before_rounding.is_zero
    assert cart.tax_breakdown == []


def test_adding_a_line() -> None:
    cart = Cart().add(item(unit_paise=11800))

    assert cart.item_count == 1
    assert cart.total_before_rounding == Money(11800)
    assert cart.tax_total == Money(1800)


def test_a_cart_is_immutable() -> None:
    """Parking a transaction is just keeping a value."""
    empty = Cart()
    one = empty.add(item())

    assert empty.is_empty
    assert one.item_count == 1


def test_line_numbers_start_at_one() -> None:
    cart = cart_of((item(), item(), item()))

    assert [n for n, _ in cart.numbered()] == [1, 2, 3]


def test_line_numbers_are_stable_after_a_void() -> None:
    """A cashier reading '3' to a supervisor must mean the same line the audit does."""
    cart = cart_of((item(description="a"), item(description="b"), item(description="c")))
    cart = cart.void_line(2)

    assert [n for n, _ in cart.numbered()] == [1, 3]
    assert cart.line(3).description == "c"


def test_a_voided_line_number_is_never_reused() -> None:
    cart = cart_of((item(), item())).void_line(2).add(item())

    assert [n for n, _ in cart.numbered()] == [1, 3]


def test_voiding_an_unknown_line() -> None:
    with pytest.raises(CartError, match="no line 9"):
        cart_of((item(),)).void_line(9)


# ── Editing ─────────────────────────────────────────────────────────────────


def test_changing_a_quantity_reprices_the_line() -> None:
    cart = cart_of((item(unit_paise=2500, qty_milli=1000),))
    cart = cart.change_quantity(1, 3000)

    assert cart.total_before_rounding == Money(7500)


def test_changing_a_quantity_keeps_the_quoted_unit_price() -> None:
    """A mid-transaction price sync must not change what the customer was quoted."""
    cart = cart_of((item(unit_paise=2500),)).change_quantity(1, 2000)

    assert cart.line(1).line.unit_price == Money(2500)


def test_applying_a_discount() -> None:
    cart = cart_of((item(unit_paise=10000),))
    cart = cart.apply_discount(1, Discount("percentage", 1000, source="manual"))

    assert cart.discount_total == Money(1000)
    assert cart.total_before_rounding == Money(9000)


# ── Totals ──────────────────────────────────────────────────────────────────


def test_a_realistic_mixed_basket() -> None:
    """The week-5 checkpoint basket, priced through the cart.

    Milk ₹33 at 0%, atta ₹275 at 5%, biscuits ₹25 x3 at 12%, toothpaste ₹115
    at 18%. Everything below is checkable on a calculator.
    """
    cart = cart_of(
        (
            item("milk", 3300, 1000, GST0, "Amul Taaza 1L"),
            item("atta", 27500, 1000, GST5, "Aashirvaad 5kg"),
            item("parle", 2500, 3000, GST12, "Parle-G 250g"),
            item("colgate", 11500, 1000, GST18, "Colgate 200g"),
        )
    )

    # 33.00 + 275.00 + (25.00 x 3) + 115.00
    assert cart.subtotal == Money(49800)             # ₹498.00
    assert cart.total_before_rounding == Money(49800)
    assert cart.discount_total.is_zero

    rates = {c.rate_bp: c for c in cart.tax_breakdown}
    assert rates[0].tax_amount.is_zero
    assert rates[500].tax_amount == Money(1310)      # ₹13.10
    assert rates[1200].tax_amount == Money(804)      # 12% of ₹75.00
    assert rates[1800].tax_amount == Money(1754)     # ₹17.54

    assert cart.taxable_base + cart.tax_total == cart.total_before_rounding


def test_the_breakdown_uses_discounted_totals() -> None:
    """Tax must follow what was charged, not what was on the shelf edge."""
    cart = cart_of((item(unit_paise=11800, tax_code=GST18),))
    cart = cart.apply_discount(1, Discount("percentage", 5000))

    assert cart.tax_breakdown[0].gross == Money(5900)
    assert cart.tax_total == Money(900)


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=1_000_000),
            st.integers(min_value=1, max_value=20_000),
            st.sampled_from([GST0, GST5, GST12, GST18]),
        ),
        min_size=1,
        max_size=20,
    )
)
def test_any_basket_reconciles(
    spec: list[tuple[int, int, TaxCode]],
) -> None:
    """For any basket: the breakdown adds up to the total the customer pays."""
    cart = cart_of(
        tuple(item("p", price, qty, code) for price, qty, code in spec)
    )

    components = cart.tax_breakdown
    assert total_base(components) + total_tax(components) == cart.total_before_rounding
    assert cart.subtotal - cart.discount_total == cart.total_before_rounding


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=1_000_000),
            st.integers(min_value=1, max_value=20_000),
        ),
        min_size=1,
        max_size=15,
    )
)
def test_the_total_is_the_sum_of_its_lines(spec: list[tuple[int, int]]) -> None:
    cart = cart_of(tuple(item("p", price, qty) for price, qty in spec))

    assert cart.total_before_rounding == total(
        [line.line_total for line in cart.lines]
    )


# ── Tender ──────────────────────────────────────────────────────────────────


def test_balance_after_a_partial_payment() -> None:
    cart = cart_of((item(unit_paise=12340),))

    assert cart.balance(Money(10000)) == Money(2340)


def test_cash_rounding_through_the_cart() -> None:
    cart = cart_of((item(unit_paise=12340),))
    result = cart.rounding_for_tender("cash")

    assert result.collected == Money(12300)
    assert result.adjustment == Money(-40)


def test_a_upi_sale_carries_no_rounding() -> None:
    """Phase 4 exit criterion, provable here already."""
    cart = cart_of((item(unit_paise=12340),))

    assert cart.rounding_for_tender("upi").adjustment.is_zero


def test_split_tender_rounds_only_the_cash_remainder() -> None:
    cart = cart_of((item(unit_paise=12340),))
    result = cart.rounding_for_tender("cash", already_paid=Money(10000))

    assert result.outstanding == Money(2340)
    assert result.adjustment == Money(-40)


def test_an_empty_cart_rounds_to_nothing() -> None:
    assert Cart().rounding_for_tender("cash").adjustment.is_zero


def test_a_cart_must_number_every_line() -> None:
    with pytest.raises(CartError, match="line number"):
        Cart(lines=(), line_numbers=(1,))

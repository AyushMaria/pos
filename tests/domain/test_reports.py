"""The owner's reports as rows and as a file — phase 8 slice 4.

The figures come from Postgres and are tested there (`tests/test_rls.py`).
What is tested here is what happens to them afterwards: the total line, the
margin withdrawn, and the table an export writes — in particular that a
number in a cell is one a spreadsheet can sum.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from app.domain.reports import (
    ProductSales,
    SalesDay,
    StockPosition,
    percent,
    products_total,
    rupees,
    sales_total,
    stock_total,
    table,
    units,
    without_margin,
)


def day(n: int, takings: int, *, cost: int | None = 0, margin: int | None = 0) -> SalesDay:
    return SalesDay(
        day=date(2026, 9, n),
        sales_count=1,
        takings=takings,
        cash=takings,
        upi_attested=0,
        upi_verified=0,
        tax=0,
        discounts=0,
        rounding=-40,
        under_review_count=0,
        under_review_total=0,
        cost=cost,
        margin=margin,
        uncosted_sales=None if cost is None else 0,
    )


def product(sales: int, cost: int | None, uncosted: int | None = 0) -> ProductSales:
    return ProductSales(
        product_id="p",
        sku="SKU-1",
        name="Atta",
        uom="each",
        qty_milli=1_500,
        sales_count=1,
        sales=sales,
        tax=0,
        discounts=0,
        cost=cost,
        margin=None if cost is None or uncosted is None else sales - uncosted - cost,
        uncosted_sales=uncosted,
    )


# ── Cells a spreadsheet can sum ────────────────────────────────────────────


def test_rupees_are_plain_numbers() -> None:
    """No ₹ and no thousands separator: Excel reads `₹1,234.00` as text."""
    assert rupees(123_400) == "1234.00"
    assert rupees(40) == "0.40"
    assert rupees(-40) == "-0.40"
    assert rupees(0) == "0.00"


@given(st.integers(min_value=-10**12, max_value=10**12))
def test_rupees_round_trip_to_the_paisa(paise: int) -> None:
    assert Decimal(rupees(paise)) * 100 == paise


def test_units_drop_the_trailing_zeros() -> None:
    assert units(2_000) == "2"
    assert units(1_500) == "1.5"
    assert units(250) == "0.25"
    assert units(-1_005) == "-1.005"


@given(st.integers(min_value=-10**9, max_value=10**9))
def test_units_round_trip_to_the_thousandth(milli: int) -> None:
    assert Decimal(units(milli)) * 1000 == milli


def test_percent_from_basis_points() -> None:
    assert percent(1_234) == "12.34"
    assert percent(-5) == "-0.05"


# ── Totals ─────────────────────────────────────────────────────────────────


@given(st.lists(st.integers(min_value=0, max_value=10**9), min_size=1, max_size=40))
def test_the_sales_total_is_the_sum_of_its_days(takings: list[int]) -> None:
    days = [day(1 + n % 28, amount) for n, amount in enumerate(takings)]
    total = sales_total(days)
    assert total.day is None
    assert total.takings == sum(takings)
    assert total.rounding == -40 * len(takings)


def test_a_withheld_cost_is_withheld_in_the_total_too() -> None:
    """A total of what you may not see is not a smaller total."""
    total = sales_total([day(1, 100, cost=None, margin=None), day(2, 100)])
    assert total.cost is None
    assert total.margin is None


def test_the_product_total_does_not_add_kilograms_to_packets() -> None:
    total = products_total([product(1_000, 800), product(2_000, 1_500)])
    assert total.qty_milli is None
    assert total.sales_count is None
    assert total.sales == 3_000
    assert total.cost == 2_300


def test_stock_value_totals_the_products_that_have_a_value() -> None:
    """A product with no price is a gap in the catalogue, not a withheld
    figure — the total is of the ones that have one."""
    rows = [
        StockPosition("a", "A", "A", "each", 2_000, 0, 500, 1_000, 400, 800),
        StockPosition("b", "B", "B", "each", 1_000, 0, None, None, None, None),
    ]
    total = stock_total(rows)
    assert total.value_at_price == 1_000
    assert total.value_at_cost == 800


# ── Margin ─────────────────────────────────────────────────────────────────


def test_margin_is_a_share_of_the_costed_sales_only() -> None:
    """₹40 on ₹400 costed is 10%, however much else sold with no cost."""
    row = product(sales=4_000_00, cost=360_00, uncosted=3_600_00)
    assert row.margin == 40_00
    assert row.margin_bp == 1_000


def test_no_costed_sales_is_no_margin_percentage() -> None:
    assert product(sales=500, cost=0, uncosted=500).margin_bp is None


def test_without_margin_withdraws_every_cost_figure() -> None:
    bare = without_margin(product(1_000, 800))
    assert (bare.cost, bare.margin, bare.uncosted_sales, bare.margin_bp) == (
        None, None, None, None,
    )
    stock = without_margin(StockPosition("a", "A", "A", "each", 1, 0, 5, 5, 4, 4))
    assert (stock.unit_cost, stock.value_at_cost) == (None, None)
    assert stock.value_at_price == 5


# ── The table ──────────────────────────────────────────────────────────────


def test_a_supervisor_export_has_no_cost_columns_at_all() -> None:
    """Absent, not empty: a blank Margin column asks why it is blank."""
    days = [without_margin(day(24, 30_800))]
    out = table("sales", days, sales_total(days), margin=False)
    header = out[0]
    assert "Takings" in header
    assert not {"Cost", "Margin", "Sales with no cost"} & set(header)
    assert all(len(line) == len(header) for line in out)


def test_a_manager_export_carries_them() -> None:
    days = [day(24, 30_800, cost=27_200, margin=3_600)]
    out = table("sales", days, sales_total(days), margin=True)
    header = out[0]
    row = dict(zip(header, out[1], strict=True))
    assert row["Cost"] == "272.00"
    assert row["Margin"] == "36.00"


def test_the_total_line_is_last_and_says_so() -> None:
    days = [day(24, 30_800), day(25, 1_200)]
    out = table("sales", days, sales_total(days), margin=False)
    total = dict(zip(out[0], out[-1], strict=True))
    assert total["Day"] == "Total"
    assert total["Takings"] == "320.00"
    assert len(out) == 1 + 2 + 1


def test_the_product_table_names_the_total_and_leaves_quantity_blank() -> None:
    rows = [product(27_500, 24_200)]
    out = table("products", rows, products_total(rows), margin=True)
    first = dict(zip(out[0], out[1], strict=True))
    total = dict(zip(out[0], out[-1], strict=True))
    assert first["Quantity"] == "1.5"
    assert first["Margin %"] == "12.00"
    assert total["SKU"] == "Total"
    assert total["Quantity"] == ""


def test_the_stock_table_hides_cost_for_a_supervisor() -> None:
    shelf = StockPosition("a", "A", "Atta", "each", 3_000, 1_000, 500, 1_500, 400, 1_200)
    rows = [without_margin(shelf)]
    out = table("stock", rows, stock_total(rows), margin=False)
    assert out[0] == ["SKU", "Product", "On hand", "Unit", "Reorder at", "Price", "Value at price"]
    assert out[1] == ["A", "Atta", "3", "each", "1", "5.00", "15.00"]

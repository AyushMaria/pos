"""The owner's reports, as rows and as a file — phase 8 slice 4.

The cloud works the figures out (`public.report_*`, migration 0026); this
module is what they are once they arrive: three row shapes, the total line
under each, and the table an export writes. It knows nothing about HTTP or
CSV files. The service fetches, the route decides who may see what, and this
decides what a figure looks like in a cell.

**Margin is a column set, not a report.** `cost`, `margin` and
`uncosted_sales` are None for a caller without `report.margin` — the database
sends nulls, and `without_margin` makes the route say the same thing again
whatever the database sent. An export for that caller has no such columns at
all, rather than empty ones: a spreadsheet with a blank Margin column invites
the question of why it is blank.

**Margin is over costed sales only** (0026's rule). `uncosted_sales` is the
part of the takings the margin does not cover, so a margin of ₹40 on ₹4,000 of
sales, ₹3,900 of it uncosted, reads as what it is.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, TypeVar

#: The longest range a report will be asked for. A year of days is 366 rows;
#: a year of products is the catalogue. Beyond that is an export nobody opens.
MAX_DAYS = 366

ReportKind = Literal["sales", "products", "stock"]


@dataclass(frozen=True, slots=True)
class SalesDay:
    """One day's trade. Money is paise; counts are counts."""

    #: None on the total line.
    day: date | None
    sales_count: int
    #: Approved payments of the sales that count — the Z-report's Takings.
    takings: int
    cash: int
    upi_attested: int
    upi_verified: int
    tax: int
    discounts: int
    #: Signed, and already inside `cash` — shown beside it, never added.
    rounding: int
    under_review_count: int
    under_review_total: int
    cost: int | None
    margin: int | None
    uncosted_sales: int | None


@dataclass(frozen=True, slots=True)
class ProductSales:
    """What one product sold over the range."""

    #: None on the total line, and so are `sku`, `name` and `uom`.
    product_id: str | None
    sku: str | None
    name: str | None
    uom: str | None
    #: Thousandths. None on the total line: kilograms and packets do not add.
    qty_milli: int | None
    #: Sales this product was on. None on the total line, where one basket
    #: holding three products would count three times.
    sales_count: int | None
    sales: int
    tax: int
    discounts: int
    cost: int | None
    margin: int | None
    uncosted_sales: int | None

    @property
    def margin_bp(self) -> int | None:
        """Margin as basis points of the sales it covers, half up."""
        if self.margin is None or self.uncosted_sales is None:
            return None
        return _basis_points(self.margin, self.sales - self.uncosted_sales)


@dataclass(frozen=True, slots=True)
class StockPosition:
    """What is on the shelf now, and what it is worth."""

    product_id: str | None
    sku: str | None
    name: str | None
    uom: str | None
    #: Thousandths. None on the total line.
    on_hand: int | None
    reorder_point: int | None
    #: The current selling price; None for a product with no open price.
    price: int | None
    value_at_price: int | None
    unit_cost: int | None
    value_at_cost: int | None


Row = TypeVar("Row", SalesDay, ProductSales, StockPosition)


def without_margin(row: Row) -> Row:
    """The same row with every cost-derived figure withdrawn."""
    if isinstance(row, StockPosition):
        return replace(row, unit_cost=None, value_at_cost=None)
    return replace(row, cost=None, margin=None, uncosted_sales=None)


def _basis_points(part: int, whole: int) -> int | None:
    if whole == 0:
        return None
    exact = Decimal(part) * 10_000 / Decimal(whole)
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _sum(values: Sequence[int | None]) -> int | None:
    """None if any figure is withheld — a total of what you may not see is
    not a smaller total, it is also withheld."""
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _sum_known(values: Sequence[int | None]) -> int | None:
    """The sum of the figures that exist, or None if none do.

    For stock value, where a product with no price is a gap in the catalogue
    rather than a withheld figure.
    """
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def sales_total(days: Sequence[SalesDay]) -> SalesDay:
    return SalesDay(
        day=None,
        sales_count=sum(d.sales_count for d in days),
        takings=sum(d.takings for d in days),
        cash=sum(d.cash for d in days),
        upi_attested=sum(d.upi_attested for d in days),
        upi_verified=sum(d.upi_verified for d in days),
        tax=sum(d.tax for d in days),
        discounts=sum(d.discounts for d in days),
        rounding=sum(d.rounding for d in days),
        under_review_count=sum(d.under_review_count for d in days),
        under_review_total=sum(d.under_review_total for d in days),
        cost=_sum([d.cost for d in days]),
        margin=_sum([d.margin for d in days]),
        uncosted_sales=_sum([d.uncosted_sales for d in days]),
    )


def products_total(rows: Sequence[ProductSales]) -> ProductSales:
    return ProductSales(
        product_id=None,
        sku=None,
        name=None,
        uom=None,
        qty_milli=None,
        sales_count=None,
        sales=sum(r.sales for r in rows),
        tax=sum(r.tax for r in rows),
        discounts=sum(r.discounts for r in rows),
        cost=_sum([r.cost for r in rows]),
        margin=_sum([r.margin for r in rows]),
        uncosted_sales=_sum([r.uncosted_sales for r in rows]),
    )


def stock_total(rows: Sequence[StockPosition]) -> StockPosition:
    return StockPosition(
        product_id=None,
        sku=None,
        name=None,
        uom=None,
        on_hand=None,
        reorder_point=None,
        price=None,
        value_at_price=_sum_known([r.value_at_price for r in rows]),
        unit_cost=None,
        value_at_cost=_sum_known([r.value_at_cost for r in rows]),
    )


# ── The table an export writes ─────────────────────────────────────────────


def rupees(paise: int) -> str:
    """`123.40`, `-0.40` — a number a spreadsheet reads as a number.

    No symbol and no thousands separator: Excel treats `₹1,234.00` as text,
    and a total nobody can sum is not a report.
    """
    sign = "-" if paise < 0 else ""
    whole, part = divmod(abs(paise), 100)
    return f"{sign}{whole}.{part:02d}"


def units(milli: int) -> str:
    """Thousandths as a quantity: `2`, `1.5`, `0.25`."""
    sign = "-" if milli < 0 else ""
    whole, part = divmod(abs(milli), 1000)
    if not part:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{part:03d}".rstrip("0")


def percent(bp: int) -> str:
    sign = "-" if bp < 0 else ""
    whole, part = divmod(abs(bp), 100)
    return f"{sign}{whole}.{part:02d}"


@dataclass(frozen=True, slots=True)
class Column:
    header: str
    #: Only for a caller who holds `report.margin`.
    margin: bool
    cell: Callable[[object], str]


def _money(value: int | None) -> str:
    return "" if value is None else rupees(value)


def _count(value: int | None) -> str:
    return "" if value is None else str(value)


def _text(value: str | None) -> str:
    return value or ""


def _sales_columns() -> list[Column]:
    def col(header: str, get: Callable[[SalesDay], str], margin: bool = False) -> Column:
        return Column(header, margin, lambda row: get(_as(row, SalesDay)))

    return [
        col("Day", lambda d: d.day.isoformat() if d.day else "Total"),
        col("Sales", lambda d: str(d.sales_count)),
        col("Takings", lambda d: rupees(d.takings)),
        col("Cash", lambda d: rupees(d.cash)),
        col("UPI attested", lambda d: rupees(d.upi_attested)),
        col("UPI verified", lambda d: rupees(d.upi_verified)),
        col("GST", lambda d: rupees(d.tax)),
        col("Discounts", lambda d: rupees(d.discounts)),
        col("Rounding (inside cash)", lambda d: rupees(d.rounding)),
        col("Under review", lambda d: str(d.under_review_count)),
        col("Under review value", lambda d: rupees(d.under_review_total)),
        col("Cost", lambda d: _money(d.cost), margin=True),
        col("Margin", lambda d: _money(d.margin), margin=True),
        col("Sales with no cost", lambda d: _money(d.uncosted_sales), margin=True),
    ]


def _product_columns() -> list[Column]:
    def col(header: str, get: Callable[[ProductSales], str], margin: bool = False) -> Column:
        return Column(header, margin, lambda row: get(_as(row, ProductSales)))

    def pct(row: ProductSales) -> str:
        bp = row.margin_bp
        return "" if bp is None else percent(bp)

    return [
        col("SKU", lambda r: _text(r.sku) if r.product_id else "Total"),
        col("Product", lambda r: _text(r.name)),
        col("Quantity", lambda r: "" if r.qty_milli is None else units(r.qty_milli)),
        col("Unit", lambda r: _text(r.uom)),
        col("Sales", lambda r: _count(r.sales_count)),
        col("Sold for", lambda r: rupees(r.sales)),
        col("GST", lambda r: rupees(r.tax)),
        col("Discounts", lambda r: rupees(r.discounts)),
        col("Cost", lambda r: _money(r.cost), margin=True),
        col("Margin", lambda r: _money(r.margin), margin=True),
        col("Margin %", pct, margin=True),
        col("Sold with no cost", lambda r: _money(r.uncosted_sales), margin=True),
    ]


def _stock_columns() -> list[Column]:
    def col(header: str, get: Callable[[StockPosition], str], margin: bool = False) -> Column:
        return Column(header, margin, lambda row: get(_as(row, StockPosition)))

    return [
        col("SKU", lambda r: _text(r.sku) if r.product_id else "Total"),
        col("Product", lambda r: _text(r.name)),
        col("On hand", lambda r: "" if r.on_hand is None else units(r.on_hand)),
        col("Unit", lambda r: _text(r.uom)),
        col("Reorder at", lambda r: "" if r.reorder_point is None else units(r.reorder_point)),
        col("Price", lambda r: _money(r.price)),
        col("Value at price", lambda r: _money(r.value_at_price)),
        col("Unit cost", lambda r: _money(r.unit_cost), margin=True),
        col("Value at cost", lambda r: _money(r.value_at_cost), margin=True),
    ]


_T = TypeVar("_T")


def _as(row: object, kind: type[_T]) -> _T:
    if not isinstance(row, kind):
        raise TypeError(f"expected {kind.__name__}, got {type(row).__name__}")
    return row


COLUMNS: dict[ReportKind, Callable[[], list[Column]]] = {
    "sales": _sales_columns,
    "products": _product_columns,
    "stock": _stock_columns,
}


def table(
    kind: ReportKind,
    rows: Sequence[SalesDay] | Sequence[ProductSales] | Sequence[StockPosition],
    total: SalesDay | ProductSales | StockPosition,
    *,
    margin: bool,
) -> list[list[str]]:
    """Header, one line per row, and the total — every cell a string.

    `margin` decides whether the cost columns exist at all. It is the
    caller's permission, not a property of the data: a manager's report on a
    catalogue with no costs still has a Cost column, empty, because that is
    true; a supervisor's has none, because that is what they may see.
    """
    columns = [c for c in COLUMNS[kind]() if margin or not c.margin]
    out = [[c.header for c in columns]]
    out += [[c.cell(row) for c in columns] for row in rows]
    out.append([c.cell(total) for c in columns])
    return out

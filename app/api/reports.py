"""The owner's reports — phase 8 slice 4.

What the shop did, as opposed to what this till did. The X/Z report is the
till's document and renders offline from SQLite; these are the owner's, read
from the cloud because only the cloud has every till's sales, and dated by
`server_received_at` because terminal clocks drift (decision 3 of the phase 8
plan). So they need the internet, and a 503 says so in a sentence.

Three enforcement points, as for everything else (§11.1):

* **`report.sales.store`** gates every route here — `require()` below, the
  Reports tab in the UI, and the guard inside each 0026 function.
* **`report.margin`** gates a *column set*, not a route. The 501 that stood
  at `/reports/margin` since phase 1 is gone; cost and margin are columns of
  the product report (and of the other two), null for a caller without the
  key. Postgres nulls them first. This router withdraws them again whatever
  Postgres sent, and an export leaves the columns out entirely. The UI hides
  them. Only the first of those is security; the other two make the refusal
  look like a report instead of a bug.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import get_admin_service, require
from app.api.schemas import (
    ProductReportResponse,
    ProductSalesOut,
    ReportExportRequest,
    ReportExportResponse,
    SalesDayOut,
    SalesReportResponse,
    StockPositionOut,
    StockReportResponse,
)
from app.domain import permissions
from app.domain import reports as rpt
from app.domain.identity import Session, utcnow
from app.services import report_export
from app.services.admin_service import AdminRejected, AdminService, AdminUnavailable

router = APIRouter(prefix="/reports", tags=["reports"])

Admin = Annotated[AdminService, Depends(get_admin_service)]
CanReport = Annotated[Session, Depends(require(permissions.REPORT_SALES_STORE))]

#: The zone a day is local to when the screen does not say. The shop's.
DEFAULT_TZ = "Asia/Kolkata"


def _sees_margin(session: Session) -> bool:
    return session.allows(permissions.REPORT_MARGIN, now=utcnow())


def _checked_range(since: date, until: date) -> None:
    if until < since:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "the range ends before it starts"
        )
    if (until - since).days + 1 > rpt.MAX_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"a report covers at most {rpt.MAX_DAYS} days — export a year at a time",
        )


def _cloud_error(exc: AdminUnavailable | AdminRejected) -> HTTPException:
    if isinstance(exc, AdminUnavailable):
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Reports are read from the cloud and need the internet. {exc}",
        )
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


# ── Fetching, with the margin withdrawn for a caller without the key ──────


async def _sales(
    admin: AdminService, session: Session, since: date, until: date, tz: str
) -> list[rpt.SalesDay]:
    _checked_range(since, until)
    try:
        days = await admin.sales_by_day(session.store_id, since, until, tz)
    except (AdminUnavailable, AdminRejected) as exc:
        raise _cloud_error(exc) from exc
    return days if _sees_margin(session) else [rpt.without_margin(d) for d in days]


async def _products(
    admin: AdminService, session: Session, since: date, until: date, tz: str
) -> list[rpt.ProductSales]:
    _checked_range(since, until)
    try:
        rows = await admin.sales_by_product(session.store_id, since, until, tz)
    except (AdminUnavailable, AdminRejected) as exc:
        raise _cloud_error(exc) from exc
    return rows if _sees_margin(session) else [rpt.without_margin(r) for r in rows]


async def _stock(admin: AdminService, session: Session) -> list[rpt.StockPosition]:
    try:
        rows = await admin.stock_position(session.store_id)
    except (AdminUnavailable, AdminRejected) as exc:
        raise _cloud_error(exc) from exc
    return rows if _sees_margin(session) else [rpt.without_margin(r) for r in rows]


def _day_out(day: rpt.SalesDay) -> SalesDayOut:
    return SalesDayOut(
        day=day.day.isoformat() if day.day else None,
        sales_count=day.sales_count,
        takings=day.takings,
        cash=day.cash,
        upi_attested=day.upi_attested,
        upi_verified=day.upi_verified,
        tax=day.tax,
        discounts=day.discounts,
        rounding=day.rounding,
        under_review_count=day.under_review_count,
        under_review_total=day.under_review_total,
        cost=day.cost,
        margin=day.margin,
        uncosted_sales=day.uncosted_sales,
    )


def _product_out(row: rpt.ProductSales) -> ProductSalesOut:
    return ProductSalesOut(
        product_id=row.product_id,
        sku=row.sku,
        name=row.name,
        uom=row.uom,
        qty_milli=row.qty_milli,
        sales_count=row.sales_count,
        sales=row.sales,
        tax=row.tax,
        discounts=row.discounts,
        cost=row.cost,
        margin=row.margin,
        margin_bp=row.margin_bp,
        uncosted_sales=row.uncosted_sales,
    )


def _stock_out(row: rpt.StockPosition) -> StockPositionOut:
    return StockPositionOut(
        product_id=row.product_id,
        sku=row.sku,
        name=row.name,
        uom=row.uom,
        on_hand=row.on_hand,
        reorder_point=row.reorder_point,
        price=row.price,
        value_at_price=row.value_at_price,
        unit_cost=row.unit_cost,
        value_at_cost=row.value_at_cost,
    )


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("/sales", response_model=SalesReportResponse)
async def sales_report(
    admin: Admin,
    session: CanReport,
    since: date,
    until: date,
    tz: str = Query(default=DEFAULT_TZ),
) -> SalesReportResponse:
    """Takings by day. The total ties to the Z-reports' Takings for the days.

    `since` and `until` are local days, inclusive, and `tz` the zone they are
    local to. The cloud turns them into instants, so a sale rung at five past
    midnight is on the day the owner thinks it is on.
    """
    days = await _sales(admin, session, since, until, tz)
    return SalesReportResponse(
        since=since.isoformat(),
        until=until.isoformat(),
        days=[_day_out(d) for d in days],
        total=_day_out(rpt.sales_total(days)),
        margin_visible=_sees_margin(session),
    )


@router.get("/products", response_model=ProductReportResponse)
async def products_report(
    admin: Admin,
    session: CanReport,
    since: date,
    until: date,
    tz: str = Query(default=DEFAULT_TZ),
) -> ProductReportResponse:
    """What sold, best first — and, under `report.margin`, what it made."""
    rows = await _products(admin, session, since, until, tz)
    return ProductReportResponse(
        since=since.isoformat(),
        until=until.isoformat(),
        rows=[_product_out(r) for r in rows],
        total=_product_out(rpt.products_total(rows)),
        margin_visible=_sees_margin(session),
    )


@router.get("/stock", response_model=StockReportResponse)
async def stock_report(admin: Admin, session: CanReport) -> StockReportResponse:
    """Every stocked product, what is on hand, and what it is worth."""
    rows = await _stock(admin, session)
    return StockReportResponse(
        rows=[_stock_out(r) for r in rows],
        total=_stock_out(rpt.stock_total(rows)),
        margin_visible=_sees_margin(session),
    )


@router.post("/export", response_model=ReportExportResponse)
async def export_report(
    body: ReportExportRequest,
    admin: Admin,
    session: CanReport,
    request: Request,
) -> ReportExportResponse:
    """Write one report as CSV under the data directory, and say where.

    The same rows the screen shows, fetched again rather than sent up from
    the page: the page is not trusted to say what a report contains, and a
    supervisor's export must lack the cost columns because the *session*
    lacks the key, not because the screen left them out.
    """
    margin = _sees_margin(session)
    now = utcnow().astimezone()

    if body.report == "stock":
        stock = await _stock(admin, session)
        table = rpt.table("stock", stock, rpt.stock_total(stock), margin=margin)
        name = f"stock-{now.date().isoformat()}"
        count = len(stock)
    else:
        if body.since is None or body.until is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"the {body.report} report needs a range: since and until",
            )
        name = f"{body.report}-{body.since.isoformat()}-to-{body.until.isoformat()}"
        if body.report == "sales":
            days = await _sales(admin, session, body.since, body.until, body.tz)
            table = rpt.table("sales", days, rpt.sales_total(days), margin=margin)
            count = len(days)
        else:
            rows = await _products(admin, session, body.since, body.until, body.tz)
            table = rpt.table("products", rows, rpt.products_total(rows), margin=margin)
            count = len(rows)

    destination = report_export.export_path(
        request.app.state.settings.data_dir, name, now=now
    )
    written = report_export.write_csv(table, destination)
    return ReportExportResponse(path=str(written), rows=count)

"""The owner's reports — phase 8 slice 4.

The SQL is tested against a real Postgres in `tests/test_rls.py`; the rows
and the CSV table in `tests/domain/test_reports.py`. This is the part in
between: the routes, what they withdraw from a caller without
`report.margin`, and the file an export leaves on disk.

The cloud is a fake here, and a deliberately careless one — it sends cost to
everybody, the way a misconfigured database would. The router must withdraw
it anyway, because the second enforcement point is only worth having if it
does not assume the first one worked.
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.domain import permissions as perms
from app.domain.reports import ProductSales, SalesDay, StockPosition
from app.services.admin_service import AdminRejected, AdminService, AdminUnavailable
from app.services.auth_service import SessionStore
from tests.conftest import TEST_STORE_ID

RANGE = "since=2026-09-24&until=2026-09-24"


def a_day(on: date = date(2026, 9, 24)) -> SalesDay:
    """₹308 taken: a ₹275 atta and a ₹33 milk, paid cash."""
    return SalesDay(
        day=on,
        sales_count=1,
        takings=30_800,
        cash=30_800,
        upi_attested=0,
        upi_verified=0,
        tax=1_310,
        discounts=0,
        rounding=0,
        under_review_count=1,
        under_review_total=3_740,
        cost=27_200,
        margin=3_600,
        uncosted_sales=0,
    )


def a_product() -> ProductSales:
    return ProductSales(
        product_id="018f0000-0000-7000-8000-000000001001",
        sku="SKU-1001",
        name="Aashirvaad Atta 5 kg",
        uom="each",
        qty_milli=1_000,
        sales_count=1,
        sales=27_500,
        tax=1_310,
        discounts=0,
        cost=24_200,
        margin=3_300,
        uncosted_sales=0,
    )


def a_shelf() -> StockPosition:
    return StockPosition(
        product_id="018f0000-0000-7000-8000-000000001001",
        sku="SKU-1001",
        name="Aashirvaad Atta 5 kg",
        uom="each",
        on_hand=12_000,
        reorder_point=4_000,
        price=27_500,
        value_at_price=330_000,
        unit_cost=24_200,
        value_at_cost=290_400,
    )


class FakeCloud:
    """Answers the three report RPCs with cost for everyone."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.asked: list[tuple[Any, ...]] = []

    async def sales_by_day(self, *args: Any) -> list[SalesDay]:
        self.asked.append(("sales", *args))
        if self.error:
            raise self.error
        return [a_day()]

    async def sales_by_product(self, *args: Any) -> list[ProductSales]:
        self.asked.append(("products", *args))
        if self.error:
            raise self.error
        return [a_product()]

    async def stock_position(self, *args: Any) -> list[StockPosition]:
        self.asked.append(("stock", *args))
        if self.error:
            raise self.error
        return [a_shelf()]


@pytest.fixture
def cloud(client: TestClient) -> FakeCloud:
    fake = FakeCloud()
    client.app.state.admin_service = fake  # type: ignore[attr-defined]
    return fake


def _read_csv(path: str) -> list[list[str]]:
    raw = Path(path).read_bytes()
    # The BOM is what makes Excel on Windows read the file as UTF-8.
    assert raw.startswith(b"\xef\xbb\xbf"), "no byte-order mark"
    return list(csv.reader(raw.decode("utf-8-sig").splitlines()))


# ── The three reports ──────────────────────────────────────────────────────


def test_a_manager_reads_takings_by_day_with_margin(
    sign_in_as: Any, cloud: FakeCloud
) -> None:
    client = sign_in_as(perms.MANAGER)

    response = client.get(f"/reports/sales?{RANGE}&tz=Asia/Kolkata")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["margin_visible"] is True
    assert body["days"][0]["day"] == "2026-09-24"
    assert body["days"][0]["margin"] == 3_600
    assert body["total"]["day"] is None
    assert body["total"]["takings"] == 30_800
    # The store is the session's, never the caller's to name.
    assert cloud.asked == [
        ("sales", TEST_STORE_ID, date(2026, 9, 24), date(2026, 9, 24), "Asia/Kolkata")
    ]


def test_a_supervisor_reads_the_same_days_without_cost(
    sign_in_as: Any, cloud: FakeCloud
) -> None:
    """The fake sent cost; the route withdrew it. The second layer does not
    assume the first one worked."""
    client = sign_in_as(perms.SUPERVISOR)

    body = client.get(f"/reports/sales?{RANGE}").json()

    assert body["margin_visible"] is False
    assert body["days"][0]["takings"] == 30_800
    assert (body["days"][0]["cost"], body["days"][0]["margin"]) == (None, None)
    assert (body["total"]["cost"], body["total"]["uncosted_sales"]) == (None, None)


def test_the_product_report_carries_the_margin_percentage(
    sign_in_as: Any, cloud: FakeCloud
) -> None:
    body = sign_in_as(perms.MANAGER).get(f"/reports/products?{RANGE}").json()

    row = body["rows"][0]
    assert row["sku"] == "SKU-1001"
    assert row["margin"] == 3_300
    assert row["margin_bp"] == 1_200
    assert body["total"]["product_id"] is None
    assert body["total"]["sales"] == 27_500


def test_stock_needs_no_range_and_hides_cost_from_a_supervisor(
    sign_in_as: Any, cloud: FakeCloud
) -> None:
    body = sign_in_as(perms.SUPERVISOR).get("/reports/stock").json()

    row = body["rows"][0]
    assert row["on_hand"] == 12_000
    assert row["value_at_price"] == 330_000
    assert (row["unit_cost"], row["value_at_cost"]) == (None, None)
    assert body["total"]["value_at_price"] == 330_000


def test_a_cashier_is_refused_every_report(sign_in_as: Any, cloud: FakeCloud) -> None:
    client = sign_in_as(perms.CASHIER)
    for path in (f"/reports/sales?{RANGE}", f"/reports/products?{RANGE}", "/reports/stock"):
        assert client.get(path).status_code == 403, path
    assert client.post("/reports/export", json={"report": "stock"}).status_code == 403
    assert cloud.asked == []


# ── The range ──────────────────────────────────────────────────────────────


def test_a_range_that_ends_before_it_starts_is_refused(
    sign_in_as: Any, cloud: FakeCloud
) -> None:
    response = sign_in_as(perms.MANAGER).get(
        "/reports/sales?since=2026-09-24&until=2026-09-23"
    )
    assert response.status_code == 422
    assert "ends before it starts" in response.json()["detail"]
    assert cloud.asked == []


def test_a_range_longer_than_a_year_is_refused(sign_in_as: Any, cloud: FakeCloud) -> None:
    response = sign_in_as(perms.MANAGER).get(
        "/reports/products?since=2025-01-01&until=2026-09-24"
    )
    assert response.status_code == 422
    assert "366 days" in response.json()["detail"]


def test_a_date_that_is_not_a_date_is_refused(sign_in_as: Any, cloud: FakeCloud) -> None:
    assert (
        sign_in_as(perms.MANAGER)
        .get("/reports/sales?since=yesterday&until=2026-09-24")
        .status_code
        == 422
    )


# ── Offline, and refused ───────────────────────────────────────────────────


def test_offline_is_a_503_with_a_sentence(sign_in_as: Any, client: TestClient) -> None:
    client.app.state.admin_service = FakeCloud(  # type: ignore[attr-defined]
        error=AdminUnavailable("no route to host")
    )
    response = sign_in_as(perms.MANAGER).get(f"/reports/sales?{RANGE}")
    assert response.status_code == 503
    assert "need the internet" in response.json()["detail"]


def test_a_refusal_from_the_database_is_a_422(sign_in_as: Any, client: TestClient) -> None:
    """A bad timezone, say — Postgres refuses it, and trying later won't help."""
    client.app.state.admin_service = FakeCloud(  # type: ignore[attr-defined]
        error=AdminRejected("22023 — time zone \"Mars/Olympus\" not recognized")
    )
    response = sign_in_as(perms.MANAGER).get(f"/reports/sales?{RANGE}&tz=Mars/Olympus")
    assert response.status_code == 422
    assert "Mars/Olympus" in response.json()["detail"]


# ── The export: a file the owner can open without the app ──────────────────


def test_a_manager_exports_the_days_with_cost_and_margin(
    sign_in_as: Any, cloud: FakeCloud, settings: Any
) -> None:
    response = sign_in_as(perms.MANAGER).post(
        "/reports/export",
        json={"report": "sales", "since": "2026-09-24", "until": "2026-09-24"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"] == 1
    path = Path(body["path"])
    assert path.parent == settings.data_dir / "reports"
    assert path.name.startswith("sales-2026-09-24-to-2026-09-24-")

    lines = _read_csv(body["path"])
    header, total = lines[0], dict(zip(lines[0], lines[-1], strict=True))
    assert {"Cost", "Margin"} <= set(header)
    assert total["Day"] == "Total"
    # The figure the plan's proof ties to the Z-report's Takings line.
    assert total["Takings"] == "308.00"


def test_a_supervisor_export_of_the_same_days_has_no_cost_columns(
    sign_in_as: Any, cloud: FakeCloud
) -> None:
    """Decided by the session, not by the page: the export fetches again."""
    body = sign_in_as(perms.SUPERVISOR).post(
        "/reports/export",
        json={"report": "sales", "since": "2026-09-24", "until": "2026-09-24"},
    ).json()

    header = _read_csv(body["path"])[0]
    assert "Takings" in header
    assert not {"Cost", "Margin", "Sales with no cost"} & set(header)


def test_the_product_export_keeps_a_hindi_name_readable(
    sign_in_as: Any, client: TestClient
) -> None:
    class HindiCloud(FakeCloud):
        async def sales_by_product(self, *args: Any) -> list[ProductSales]:
            return [replace(a_product(), name="आशीर्वाद आटा")]

    client.app.state.admin_service = HindiCloud()  # type: ignore[attr-defined]
    body = sign_in_as(perms.MANAGER).post(
        "/reports/export",
        json={"report": "products", "since": "2026-09-24", "until": "2026-09-24"},
    ).json()

    lines = _read_csv(body["path"])
    assert lines[1][1] == "आशीर्वाद आटा"
    assert lines[1][lines[0].index("Margin %")] == "12.00"


def test_the_stock_export_needs_no_range(sign_in_as: Any, cloud: FakeCloud) -> None:
    body = sign_in_as(perms.MANAGER).post("/reports/export", json={"report": "stock"}).json()

    lines = _read_csv(body["path"])
    assert Path(body["path"]).name.startswith("stock-")
    assert lines[0][-2:] == ["Unit cost", "Value at cost"]
    assert lines[1][:3] == ["SKU-1001", "Aashirvaad Atta 5 kg", "12"]


def test_a_sales_export_without_a_range_says_so(sign_in_as: Any, cloud: FakeCloud) -> None:
    response = sign_in_as(perms.MANAGER).post("/reports/export", json={"report": "sales"})
    assert response.status_code == 422
    assert "needs a range" in response.json()["detail"]


def test_two_exports_of_the_same_range_keep_both(
    sign_in_as: Any, cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once before a late sale pushed, once after: neither overwrites the other."""
    from app.api import reports as route

    ticks = count()
    start = datetime(2026, 9, 25, 9, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(route, "utcnow", lambda: start + timedelta(seconds=next(ticks)))
    client = sign_in_as(perms.MANAGER)
    request = {"report": "sales", "since": "2026-09-24", "until": "2026-09-24"}

    first = client.post("/reports/export", json=request).json()["path"]
    second = client.post("/reports/export", json=request).json()["path"]

    assert first != second
    assert Path(first).exists() and Path(second).exists()


# ── What the service sends to PostgREST ────────────────────────────────────


class Recorder:
    def __init__(self, reply: list[dict[str, Any]]) -> None:
        self.reply = reply
        self.seen: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(200, json=self.reply)


def _service(recorder: Recorder) -> AdminService:
    sessions = SessionStore()
    sessions._access_token = "the-users-own-jwt"
    return AdminService(
        "https://project.supabase.co",
        "anon-key",
        sessions,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder.handle)),
    )


async def test_the_service_asks_for_local_days_under_the_users_own_token() -> None:
    recorder = Recorder(
        [
            {
                "day": "2026-09-24", "sales_count": 1, "takings": 30800, "cash": 30800,
                "upi_attested": 0, "upi_verified": 0, "tax": 1310, "discounts": 0,
                "rounding": -40, "under_review_count": 0, "under_review_total": 0,
                "cost": None, "margin": None, "uncosted_sales": None,
            }
        ]
    )

    days = await _service(recorder).sales_by_day(
        "store-1", date(2026, 9, 24), date(2026, 9, 24), "Asia/Kolkata"
    )

    request = recorder.seen[0]
    assert request.method == "POST"
    assert request.url.path == "/rest/v1/rpc/report_sales_by_day"
    assert request.headers["Authorization"] == "Bearer the-users-own-jwt"
    assert json.loads(request.content) == {
        "p_store_id": "store-1",
        "p_from": "2026-09-24",
        "p_to": "2026-09-24",
        "p_tz": "Asia/Kolkata",
    }
    # Null cost is what a supervisor gets from 0026, and it survives parsing.
    assert days[0].takings == 30_800
    assert days[0].cost is None


async def test_the_service_reads_the_other_two() -> None:
    products = Recorder(
        [
            {
                "product_id": "p1", "sku": "SKU-1", "name": "Atta", "uom": "each",
                "qty_milli": 1000, "sales_count": 1, "sales": 27500, "tax": 1310,
                "discounts": 0, "cost": 24200, "margin": 3300, "uncosted_sales": 0,
            }
        ]
    )
    stock = Recorder(
        [
            {
                "product_id": "p1", "sku": "SKU-1", "name": "Atta", "uom": "each",
                "on_hand": 12000, "reorder_point": 4000, "price": None,
                "value_at_price": None, "unit_cost": 24200, "value_at_cost": 290400,
            }
        ]
    )

    sold = await _service(products).sales_by_product(
        "store-1", date(2026, 9, 1), date(2026, 9, 24), "Asia/Kolkata"
    )
    shelf = await _service(stock).stock_position("store-1")

    assert products.seen[0].url.path == "/rest/v1/rpc/report_sales_by_product"
    assert sold[0].margin_bp == 1_200
    assert stock.seen[0].url.path == "/rest/v1/rpc/report_stock_position"
    assert json.loads(stock.seen[0].content) == {"p_store_id": "store-1"}
    assert shelf[0].price is None
    assert shelf[0].value_at_cost == 290_400

"""Pulling reference data — architecture §9.3.

Watermarks, tombstones, and the property that matters most on a till: a price
that changed upstream reaches the counter, and a product withdrawn upstream
stops being sellable — without anything local being erased.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.data.db import Database
from app.data.repositories.catalog import CatalogRepository
from app.sync.puller import ENTITIES, EPOCH, Entity, Puller

PRODUCT_ID = "018f0000-0000-7000-8000-000000002001"


class FakeRest:
    """PostgREST, as far as the puller is concerned."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        table = request.url.path.rsplit("/", 1)[-1]
        rows = self.tables.get(table, [])

        cursor_param = next(
            (
                value
                for key, value in request.url.params.items()
                if value.startswith("gt.")
            ),
            None,
        )
        column = request.url.params.get("order", "updated_at.asc").split(".")[0]
        if cursor_param:
            cutoff = cursor_param[3:]
            rows = [row for row in rows if str(row.get(column, "")) > cutoff]

        return httpx.Response(200, json=rows)

    @property
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


@pytest.fixture
def rest() -> FakeRest:
    return FakeRest()


def puller(db: Database, rest: FakeRest, token: str | None = "jwt") -> Puller:
    return Puller(
        db,
        base_url="https://example.supabase.co",
        anon_key="anon",
        token_provider=lambda: token,
        client=rest.client,
    )


def a_product(**overrides: Any) -> dict[str, Any]:
    return {
        "id": PRODUCT_ID,
        "sku": "SKU-NEW",
        "name": "Tata Salt 1 kg",
        "short_name": "Tata Salt",
        "category_id": None,
        "uom": "each",
        "is_weighed": False,
        "track_stock": True,
        "tax_code": "GST0",
        "is_active": True,
        "updated_at": "2026-08-21T09:00:00+00:00",
        "deleted_at": None,
        **overrides,
    }


PRODUCTS = next(entity for entity in ENTITIES if entity.name == "products")


def a_sellable_catalogue(rest: FakeRest, price: int = 2800) -> None:
    """Everything the register needs before a barcode resolves to a line.

    A product on its own is not sellable: the lookup joins its tax code and
    its price, and a missing either is the same as a missing product. Pulling
    them in the right order is the puller's job; having them all is this
    fixture's.
    """
    now = "2026-08-21T09:00:00+00:00"
    rest.tables["tax_codes"] = [
        {
            "code": "GST0",
            "name": "GST 0%",
            "rate_bp": 0,
            "is_inclusive": True,
            "updated_at": now,
        }
    ]
    rest.tables["products"] = [a_product()]
    rest.tables["product_barcodes"] = [
        {
            "id": "018f0000-0000-7000-8000-000000002101",
            "product_id": PRODUCT_ID,
            "barcode": "8901491101837",
            "symbology": "EAN13",
            "pack_size": 1,
            "is_primary": True,
            "updated_at": now,
            "deleted_at": None,
        }
    ]
    rest.tables["product_prices"] = [
        {
            "id": "018f0000-0000-7000-8000-000000002201",
            "product_id": PRODUCT_ID,
            "store_id": "ST01",
            "price": price,
            "valid_from": now,
            "valid_to": None,
        }
    ]


# ── Watermarks ──────────────────────────────────────────────────────────────


def test_a_fresh_terminal_asks_from_the_beginning(db: Database, rest: FakeRest) -> None:
    assert puller(db, rest).cursor_for("products") == EPOCH


@pytest.mark.asyncio
async def test_pulling_advances_the_watermark(db: Database, rest: FakeRest) -> None:
    rest.tables["products"] = [a_product()]
    engine = puller(db, rest)

    await engine.pull(PRODUCTS)

    assert engine.cursor_for("products") == "2026-08-21T09:00:00+00:00"


@pytest.mark.asyncio
async def test_a_second_pull_asks_only_for_what_changed(
    db: Database, rest: FakeRest
) -> None:
    """The whole point of a watermark: a till that has been on all day is not
    re-downloading the catalogue every ninety seconds."""
    rest.tables["products"] = [a_product()]
    engine = puller(db, rest)
    await engine.pull(PRODUCTS)

    result = await engine.pull(PRODUCTS)

    assert result.rows == 0
    last = rest.requests[-1]
    assert last.url.params["updated_at"] == "gt.2026-08-21T09:00:00+00:00"


@pytest.mark.asyncio
async def test_a_signed_out_terminal_pulls_nothing(
    db: Database, rest: FakeRest
) -> None:
    rest.tables["products"] = [a_product()]

    result = await puller(db, rest, token=None).pull(PRODUCTS)

    assert result.rows == 0
    assert rest.requests == []


# ── What lands locally ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_product_becomes_sellable(db: Database, rest: FakeRest) -> None:
    rest.tables["products"] = [a_product()]

    await puller(db, rest).pull(PRODUCTS)

    row = db.query_one("SELECT * FROM products WHERE id = ?", (PRODUCT_ID,))
    assert row is not None
    assert row["name"] == "Tata Salt 1 kg"
    # SQLite has no boolean; Postgres sends one.
    assert row["is_active"] == 1
    assert row["is_weighed"] == 0


@pytest.mark.asyncio
async def test_an_upstream_edit_wins(db: Database, rest: FakeRest) -> None:
    """Reference data is pull-only and nothing local edits it, so there is no
    merge to perform — server wins, always (§9.1)."""
    rest.tables["products"] = [a_product()]
    engine = puller(db, rest)
    await engine.pull(PRODUCTS)

    rest.tables["products"] = [
        a_product(name="Tata Salt 1 kg (new pack)",
                  updated_at="2026-08-21T10:00:00+00:00")
    ]
    await engine.pull(PRODUCTS)

    row = db.query_one("SELECT name FROM products WHERE id = ?", (PRODUCT_ID,))
    assert row["name"] == "Tata Salt 1 kg (new pack)"


@pytest.mark.asyncio
async def test_a_withdrawn_product_arrives_as_a_tombstone(
    db: Database, rest: FakeRest
) -> None:
    """Soft deletes propagate; nothing is erased.

    The row stays, carrying `deleted_at`, so the product stops being sellable
    and an upstream mistake can be undone upstream.
    """
    rest.tables["products"] = [a_product()]
    engine = puller(db, rest)
    await engine.pull(PRODUCTS)

    rest.tables["products"] = [
        a_product(
            deleted_at="2026-08-21T11:00:00+00:00",
            updated_at="2026-08-21T11:00:00+00:00",
        )
    ]
    await engine.pull(PRODUCTS)

    row = db.query_one("SELECT deleted_at FROM products WHERE id = ?", (PRODUCT_ID,))
    assert row is not None, "the row is kept, not deleted"
    assert row["deleted_at"] == "2026-08-21T11:00:00+00:00"


@pytest.mark.asyncio
async def test_a_withdrawn_product_stops_being_found(
    db: Database, rest: FakeRest
) -> None:
    """The tombstone has to actually stop a sale, or it is just a column."""
    a_sellable_catalogue(rest)
    engine = puller(db, rest)
    await engine.pull_all()

    catalog = CatalogRepository(db)
    assert catalog.by_lookup_key("8901491101837") is not None

    rest.tables["products"] = [
        a_product(
            deleted_at="2026-08-21T11:00:00+00:00",
            updated_at="2026-08-21T11:00:00+00:00",
        )
    ]
    await engine.pull(PRODUCTS)

    assert catalog.by_lookup_key("8901491101837") is None


@pytest.mark.asyncio
async def test_a_price_change_reaches_the_counter(db: Database, rest: FakeRest) -> None:
    """The reason the poll interval is 90 seconds and not an hour."""
    a_sellable_catalogue(rest)
    engine = puller(db, rest)
    await engine.pull_all()

    catalog = CatalogRepository(db)
    assert catalog.by_lookup_key("8901491101837").unit_price.paise == 2800

    # Upstream closes the old row and opens a new one, which is how a price
    # change actually arrives.
    rest.tables["product_prices"] = [
        {
            "id": "018f0000-0000-7000-8000-000000002201",
            "product_id": PRODUCT_ID,
            "store_id": "ST01",
            "price": 2800,
            "valid_from": "2026-08-21T09:00:00+00:00",
            "valid_to": "2026-08-21T12:00:00+00:00",
        },
        {
            "id": "018f0000-0000-7000-8000-000000002202",
            "product_id": PRODUCT_ID,
            "store_id": "ST01",
            "price": 3000,
            "valid_from": "2026-08-21T12:00:00+00:00",
            "valid_to": None,
        },
    ]
    await engine.pull_all()

    assert catalog.by_lookup_key("8901491101837").unit_price.paise == 3000


@pytest.mark.asyncio
async def test_two_open_prices_charge_the_newest(
    db: Database, rest: FakeRest
) -> None:
    """This should never happen, and the till should still be predictable.

    Upstream is supposed to close a price row when it opens the next one. If
    it does not — or a page arrives out of order — a plain join would charge
    whichever row SQLite read first, which is a pricing bug nobody can
    reproduce. The newest open price is at least the one the shop meant.
    """
    a_sellable_catalogue(rest)
    engine = puller(db, rest)
    await engine.pull_all()

    rest.tables["product_prices"].append(
        {
            "id": "018f0000-0000-7000-8000-000000002203",
            "product_id": PRODUCT_ID,
            "store_id": "ST01",
            "price": 3100,
            "valid_from": "2026-08-21T13:00:00+00:00",
            "valid_to": None,
        }
    )
    await engine.pull_all()

    catalog = CatalogRepository(db)
    assert catalog.by_lookup_key("8901491101837").unit_price.paise == 3100


# ── Ordering ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_products_are_pulled_before_the_things_that_reference_them(
    db: Database, rest: FakeRest
) -> None:
    """Foreign keys are on locally, so order is not cosmetic."""
    names = [entity.name for entity in ENTITIES]

    assert names.index("products") < names.index("product_barcodes")
    assert names.index("products") < names.index("product_prices")
    assert names.index("tax_codes") < names.index("products")


@pytest.mark.asyncio
async def test_each_entity_asks_only_for_the_columns_it_stores(
    db: Database, rest: FakeRest
) -> None:
    """A `select=*` would break the moment the server grows a column this
    schema does not have — which is exactly what happens to a terminal that
    has not been updated."""
    rest.tables["products"] = []
    await puller(db, rest).pull(PRODUCTS)

    selected = rest.requests[-1].url.params["select"].split(",")
    assert set(selected) == set(PRODUCTS.columns)


@pytest.mark.asyncio
async def test_a_page_of_one_timestamp_does_not_spin(
    db: Database, rest: FakeRest
) -> None:
    """`gt.` cannot advance past a page that is entirely one timestamp, so the
    loop stops rather than asking forever."""
    same = "2026-08-21T09:00:00+00:00"
    rest.tables["widgets"] = []
    entity = Entity("widgets", "widgets", ("id", "updated_at"))

    async def one_page(request: httpx.Request) -> httpx.Response:
        rest.requests.append(request)
        if len(rest.requests) > 5:
            raise AssertionError("the puller is spinning")
        return httpx.Response(
            200, json=[{"id": "a", "updated_at": same}, {"id": "b", "updated_at": same}]
        )

    engine = Puller(
        db,
        base_url="https://example.supabase.co",
        anon_key="anon",
        token_provider=lambda: "jwt",
        client=httpx.AsyncClient(transport=httpx.MockTransport(one_page)),
    )
    with db.write() as conn:
        conn.execute("CREATE TABLE widgets (id TEXT PRIMARY KEY, updated_at TEXT)")

    result = await engine.pull(entity)

    assert result.rows == 2


@pytest.mark.asyncio
async def test_json_from_postgres_is_stored_as_text(
    db: Database, rest: FakeRest
) -> None:
    """SQLite has no JSON column; a promotion's scope arrives as an object."""
    from app.sync.puller import _local

    assert _local({"category": "dairy"}) == json.dumps({"category": "dairy"})
    assert _local(True) == 1
    assert _local(None) is None

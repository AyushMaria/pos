"""Pulling reference data — architecture §9.3.

Watermarks, tombstones, and the property that matters most on a till: a price
that changed upstream reaches the counter, and a product withdrawn upstream
stops being sellable — without anything local being erased.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.data.db import Database
from app.data.repositories.catalog import CatalogRepository
from app.sync.puller import (
    ENTITIES,
    EPOCH,
    Entity,
    Puller,
    _decode_position,
    _keyset_filter,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

PRODUCT_ID = "018f0000-0000-7000-8000-000000002001"


def _split_top_level(text: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def _matches_keyset(row: dict[str, Any], keyset: str) -> bool:
    """Evaluate `or=(a.gt.X,and(a.eq.X,b.gt.Y))` against one row.

    Only the two operators the puller emits. Enough of PostgREST to prove the
    keyset walks a page of identical timestamps instead of stopping on one.
    """

    def compare(term: str) -> bool:
        column, operator, value = term.split(".", 2)
        actual = str(row.get(column, ""))
        return actual > value if operator == "gt" else actual == value

    for clause in _split_top_level(keyset[1:-1]):
        if clause.startswith("and("):
            if all(compare(term) for term in _split_top_level(clause[4:-1])):
                return True
        elif compare(clause):
            return True
    return False


class FakeRest:
    """PostgREST, as far as the puller is concerned."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        table = request.url.path.rsplit("/", 1)[-1]
        rows = self.tables.get(table, [])

        order = [
            term.split(".")[0]
            for term in request.url.params.get("order", "updated_at.asc").split(",")
        ]
        rows = sorted(rows, key=lambda row: tuple(str(row.get(c, "")) for c in order))

        keyset = request.url.params.get("or")
        if keyset:
            rows = [row for row in rows if _matches_keyset(row, keyset)]
        else:
            cursor_param = next(
                (
                    value
                    for key, value in request.url.params.items()
                    if value.startswith("gt.")
                ),
                None,
            )
            if cursor_param:
                cutoff = cursor_param[3:]
                rows = [
                    row for row in rows if str(row.get(order[0], "")) > cutoff
                ]

        limit = int(request.url.params.get("limit", len(rows) or 1))
        return httpx.Response(200, json=rows[:limit])

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

    # Stored as a keyset now — watermark plus the key that breaks a tie on it.
    assert engine.position_for(PRODUCTS) == {
        "updated_at": "2026-08-21T09:00:00+00:00",
        "id": PRODUCT_ID,
    }


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
    assert last.url.params["or"] == (
        "(updated_at.gt.2026-08-21T09:00:00+00:00,"
        f"and(updated_at.eq.2026-08-21T09:00:00+00:00,id.gt.{PRODUCT_ID}))"
    )


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
async def test_a_catalogue_sharing_one_timestamp_is_pulled_in_full(
    db: Database, rest: FakeRest
) -> None:
    """The regression this file exists for.

    A catalogue imported in one transaction gives every row the same
    `updated_at`. A watermark alone cannot page that: `updated_at=gt.{cursor}`
    returns a full page whose last timestamp *is* the cursor, and the pull
    stops holding one page. On a real terminal that meant 1,000 of 19,000
    products, no error, and then a `FOREIGN KEY constraint failed` on the
    barcode pull — an error naming neither the table nor the layer at fault.

    The cursor is now (updated_at, id), so ties are walked through.
    """
    same = "2026-08-21T09:00:00+00:00"
    entity = Entity("widgets", "widgets", ("id", "updated_at"))
    rest.tables["widgets"] = [
        {"id": f"{index:05d}", "updated_at": same}
        for index in range(2500)
    ]

    engine = Puller(
        db,
        base_url="https://example.supabase.co",
        anon_key="anon",
        token_provider=lambda: "jwt",
        client=rest.client,
    )
    with db.write() as conn:
        conn.execute("CREATE TABLE widgets (id TEXT PRIMARY KEY, updated_at TEXT)")

    result = await engine.pull(entity)

    assert result.rows == 2500
    stored = db.query_one("SELECT COUNT(*) AS n FROM widgets")
    assert stored is not None and stored["n"] == 2500


@pytest.mark.asyncio
async def test_a_page_with_no_usable_cursor_stops_rather_than_spinning(
    db: Database, rest: FakeRest
) -> None:
    """The loop guard still has to hold when a row cannot produce a cursor."""
    entity = Entity("widgets", "widgets", ("id", "updated_at"))

    async def cursorless(request: httpx.Request) -> httpx.Response:
        rest.requests.append(request)
        if len(rest.requests) > 5:
            raise AssertionError("the puller is spinning")
        return httpx.Response(200, json=[{"id": "a"}, {"id": "b"}])

    engine = Puller(
        db,
        base_url="https://example.supabase.co",
        anon_key="anon",
        token_provider=lambda: "jwt",
        client=httpx.AsyncClient(transport=httpx.MockTransport(cursorless)),
    )
    with db.write() as conn:
        conn.execute("CREATE TABLE widgets (id TEXT PRIMARY KEY, updated_at TEXT)")

    result = await engine.pull(entity)

    assert result.rows == 2


def test_a_cursor_written_by_an_older_build_still_works() -> None:
    """Upgrading a terminal must not re-pull the catalogue or skip it.

    Before this change the stored cursor was a bare timestamp. It has to
    decode to a watermark with no tiebreak — the old behaviour for exactly one
    page, which then writes a full position and carries on.
    """
    products = next(e for e in ENTITIES if e.name == "products")
    legacy = "2026-08-22T12:47:31.229489+00:00"

    position = _decode_position(legacy, products)

    assert position == {"updated_at": legacy}
    assert _keyset_filter(products, position) == {"updated_at": f"gt.{legacy}"}


@pytest.mark.asyncio
async def test_stock_levels_lands_in_a_table_that_has_no_store_id(
    db: Database, rest: FakeRest
) -> None:
    """The terminal's stock_levels is keyed on product_id alone — a till
    serves one store. The cloud's is keyed on the pair, so `store_id` has to
    be selected for the keyset and then dropped before the local insert.

    Sent it straight through and the pull died with `table stock_levels has no
    column named store_id`, which nothing noticed because the pull had already
    been failing earlier on for two other reasons.
    """
    stock = next(e for e in ENTITIES if e.name == "stock_levels")
    rest.tables["products"] = [a_product()]
    rest.tables["stock_levels"] = [
        {
            "store_id": "018f0000-0000-7000-8000-000000000100",
            "product_id": PRODUCT_ID,
            "on_hand": 7000,
            "reorder_point": 2000,
            "updated_at": "2026-08-21T09:00:00+00:00",
        }
    ]
    engine = puller(db, rest)
    await engine.pull(PRODUCTS)

    result = await engine.pull(stock)

    assert result.rows == 1
    row = db.query_one(
        "SELECT * FROM stock_levels WHERE product_id = ?", (PRODUCT_ID,)
    )
    assert row is not None
    assert row["on_hand"] == 7000
    # Selected, so the keyset can page a table whose key is the pair.
    assert "store_id" in stock.columns
    assert "store_id" not in stock.stored_columns


def test_every_entity_stores_only_columns_its_local_table_has() -> None:
    """`columns` is what the server is asked for; `stored_columns` is what
    SQLite is given. Where they differ it must be deliberate."""
    local_schema = (REPO_ROOT / "app" / "data" / "migrations" / "001_init.sql").read_text()
    for entity in ENTITIES:
        start = local_schema.index(f"CREATE TABLE {entity.table} (")
        body = local_schema[start : local_schema.index(");", start)]
        for column in entity.stored_columns:
            assert f"\n    {column} " in body, f"{entity.table}.{column} is not local"


def test_every_entity_can_break_a_tie() -> None:
    """A tiebreak that is not unique leaves the stall in place, so each
    entity's (watermark, tiebreak) must identify a row."""
    unique_keys = {
        "tax_codes": ("code",),
        "products": ("id",),
        "product_barcodes": ("id",),
        "product_prices": ("id",),
        "stock_levels": ("store_id", "product_id"),
    }
    for entity in ENTITIES:
        assert entity.tiebreak_columns == unique_keys[entity.name], entity.name
        for column in entity.tiebreak_columns:
            assert column in entity.columns, f"{entity.name}.{column} is not selected"


@pytest.mark.asyncio
async def test_json_from_postgres_is_stored_as_text(
    db: Database, rest: FakeRest
) -> None:
    """SQLite has no JSON column; a promotion's scope arrives as an object."""
    from app.sync.puller import _local

    assert _local({"category": "dairy"}) == json.dumps({"category": "dairy"})
    assert _local(True) == 1
    assert _local(None) is None

"""Pulling reference data down — architecture §9.3.

Per-entity keyset, oldest first — a watermark plus whatever breaks a tie on
it, because a catalogue imported in one transaction shares a single
`updated_at` across every row and a watermark alone cannot page past that:

    GET /rest/v1/products
        ?or=(updated_at.gt.{t},and(updated_at.eq.{t},id.gt.{id}))
        &order=updated_at.asc,id.asc&limit=1000

**Polling, not Realtime.** `supabase-py`'s Realtime support is thinner than the
JS client's, and a till that quietly stops receiving price changes is worse
than one that checks every ninety seconds. Realtime can be added later as an
optimisation; the watermark stays the mechanism, because it is the only part
that is correct after the terminal has been switched off for a week.

**Soft deletes propagate as tombstones.** A product withdrawn upstream arrives
as a row with `deleted_at` set, and is written locally exactly as it came. The
catalogue query already excludes deleted rows, so a withdrawn product stops
being sellable without anything being erased — and if the deletion was a
mistake, undoing it upstream restores it here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.data.db import Database
from app.domain.identity import utcnow

log = logging.getLogger(__name__)

PAGE_SIZE = 1000
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

#: Beginning of time, for a terminal that has never pulled.
EPOCH = "1970-01-01T00:00:00+00:00"


@dataclass(frozen=True, slots=True)
class Entity:
    """One pull-only table, and how to write it locally."""

    name: str
    table: str
    columns: tuple[str, ...]
    #: Ordering and watermark column. Every reference table has one.
    cursor_column: str = "updated_at"
    #: What breaks a tie when several rows share a watermark. Together with
    #: `cursor_column` these must identify a row uniquely, or a page can still
    #: fail to advance.
    tiebreak_columns: tuple[str, ...] = ("id",)
    #: Columns to write locally, when the terminal's table is deliberately
    #: narrower than the cloud's. Defaults to everything selected.
    local_columns: tuple[str, ...] | None = None

    @property
    def stored_columns(self) -> tuple[str, ...]:
        return self.local_columns if self.local_columns is not None else self.columns


#: Order matters: a barcode references a product, and a price references a
#: product, so products land first. Foreign keys are on locally.
ENTITIES: tuple[Entity, ...] = (
    Entity(
        "tax_codes", "tax_codes",
        ("code", "name", "rate_bp", "is_inclusive", "updated_at"),
        tiebreak_columns=("code",),
    ),
    Entity(
        "products", "products",
        ("id", "sku", "name", "short_name", "category_id", "uom", "is_weighed",
         "track_stock", "tax_code", "is_active", "updated_at", "deleted_at"),
    ),
    Entity(
        "product_barcodes", "product_barcodes",
        ("id", "product_id", "barcode", "symbology", "pack_size", "is_primary",
         "updated_at", "deleted_at"),
    ),
    Entity(
        "product_prices", "product_prices",
        ("id", "product_id", "store_id", "price", "valid_from", "valid_to"),
        cursor_column="valid_from",
    ),
    Entity(
        "stock_levels", "stock_levels",
        ("store_id", "product_id", "on_hand", "reorder_point", "updated_at"),
        # No surrogate key on this one; its primary key is the pair, so both
        # halves are needed to page it.
        tiebreak_columns=("store_id", "product_id"),
        # But the terminal's own stock_levels is keyed on product_id alone and
        # has no store_id column at all: a till serves one store, so carrying
        # the id would be a column with one value in it. `store_id` is
        # therefore selected — the keyset needs it — and dropped before the
        # local insert, which is what `local_columns` is for.
        #
        # The pull previously died here with `table stock_levels has no column
        # named store_id`, unseen because it never got past the barcode pull.
        #
        # If a terminal ever serves two stores, this is the line that has to
        # change first: rows for two stores would collide on product_id.
        local_columns=("product_id", "on_hand", "reorder_point", "updated_at"),
    ),
)


@dataclass(frozen=True, slots=True)
class PullResult:
    entity: str
    rows: int
    cursor: str | None


class Puller:
    def __init__(
        self,
        db: Database,
        *,
        base_url: str,
        anon_key: str,
        token_provider: Any,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.db = db
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        self.token_provider = token_provider
        self._client = client

    # ── Watermarks ──────────────────────────────────────────────────────────

    def cursor_for(self, entity_name: str) -> str:
        row = self.db.query_one(
            "SELECT cursor FROM sync_state WHERE entity = ?", (entity_name,)
        )
        return str(row["cursor"]) if row and row["cursor"] else EPOCH

    def position_for(self, entity: Entity) -> dict[str, str]:
        """The stored watermark, as a keyset position.

        Cursors written before this was a compound key are bare timestamps.
        They decode to a watermark with no tiebreak, which is exactly the
        old behaviour for one page and self-corrects from the next one — so
        an upgraded terminal needs no migration and re-pulls nothing.
        """
        return _decode_position(self.cursor_for(entity.name), entity)

    def _advance(self, entity: str, cursor: str) -> None:
        with self.db.write() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (entity, cursor, last_pulled_at)
                VALUES (?, ?, ?)
                ON CONFLICT (entity) DO UPDATE
                    SET cursor = excluded.cursor,
                        last_pulled_at = excluded.last_pulled_at
                """,
                (entity, cursor, utcnow().isoformat()),
            )

    # ── Pulling ─────────────────────────────────────────────────────────────

    async def pull_all(self) -> list[PullResult]:
        return [await self.pull(entity) for entity in ENTITIES]

    async def pull(self, entity: Entity) -> PullResult:
        position = self.position_for(entity)
        total = 0

        while True:
            rows = await self._fetch(entity, position)
            if not rows:
                break

            self._write(entity, rows)
            total += len(rows)

            advanced = _position_from_row(entity, rows[-1])
            if advanced is None or advanced == position:
                # Genuinely stuck rather than merely tied: the last row is
                # missing the columns the cursor is built from. Loud, because
                # the visible symptom downstream is a foreign key failure on
                # a child table and nothing that names this table.
                log.warning(
                    "%s pull stalled at %s: the last row carried no usable "
                    "cursor, so %d row(s) may be missing locally",
                    entity.name, position, PAGE_SIZE,
                )
                break

            position = advanced
            self._advance(entity.name, _encode_position(position))

            if len(rows) < PAGE_SIZE:
                break

        if total:
            log.info("pulled %d %s", total, entity.name)
        return PullResult(
            entity=entity.name, rows=total, cursor=_encode_position(position)
        )

    async def _fetch(
        self, entity: Entity, position: dict[str, str]
    ) -> list[dict[str, Any]]:
        token = self.token_provider()
        if not token:
            return []

        params = {
            "order": _order_by(entity),
            "limit": str(PAGE_SIZE),
            "select": ",".join(entity.columns),
        }
        params.update(_keyset_filter(entity, position))
        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {token}",
        }

        client = self._client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            response = await client.get(
                f"{self.base_url}/rest/v1/{entity.table}",
                params=params,
                headers=headers,
            )
        finally:
            if self._client is None:
                await client.aclose()

        response.raise_for_status()
        payload = response.json()
        return list(payload) if isinstance(payload, list) else []

    def _write(self, entity: Entity, rows: list[dict[str, Any]]) -> None:
        """Server wins, always.

        Reference data is pull-only and nothing local ever edits it, so there
        is no merge to perform and no conflict to resolve — which is exactly
        why the classification in §9.1 is worth keeping strict.
        """
        stored = entity.stored_columns
        columns = ",".join(stored)
        placeholders = ",".join("?" for _ in stored)
        with self.db.write() as conn:
            for row in rows:
                conn.execute(
                    f"INSERT OR REPLACE INTO {entity.table} ({columns}) "
                    f"VALUES ({placeholders})",
                    tuple(_local(row.get(column)) for column in stored),
                )


# ── The keyset ──────────────────────────────────────────────────────────────
#
# A watermark alone cannot page a table where many rows share a timestamp.
# `updated_at=gt.{cursor}` returns a full page, the last row's timestamp *is*
# the cursor, and the next request asks for rows strictly after a value that
# most of the unread rows also carry. The pull stops, silently, holding
# whatever fitted in one page.
#
# That is not hypothetical. A catalogue imported in a single transaction gives
# every one of its rows the same `now()`, so a 19,000-product import left the
# terminal with 1,000 products — and then failed the `product_barcodes` pull
# outright with `FOREIGN KEY constraint failed`, because barcodes arrived for
# products that were never sent. The error named the wrong table and the wrong
# layer.
#
# So the cursor is the watermark *plus* enough of a key to be unique, and the
# filter is the standard keyset comparison:
#
#     or=(updated_at.gt.T,
#         and(updated_at.eq.T, id.gt.I))
#
# ordered by the same columns. Ties are now walked through rather than
# stumbled over, and the page size stops being a cap on the catalogue.


def _order_by(entity: Entity) -> str:
    columns = (entity.cursor_column, *entity.tiebreak_columns)
    return ",".join(f"{column}.asc" for column in columns)


def _keyset_filter(entity: Entity, position: dict[str, str]) -> dict[str, str]:
    """PostgREST params selecting everything strictly after `position`."""
    watermark = position.get(entity.cursor_column, EPOCH)
    tiebreaks = [
        (column, position[column])
        for column in entity.tiebreak_columns
        if position.get(column) is not None
    ]

    # First pull of this entity, or a cursor written by an older build: no
    # tiebreak to compare against, so the plain watermark is both correct and
    # cheaper. The next page carries a full position.
    if not tiebreaks:
        return {entity.cursor_column: f"gt.{watermark}"}

    clauses = [f"{entity.cursor_column}.gt.{watermark}"]
    for index, (column, value) in enumerate(tiebreaks):
        equalities = [f"{entity.cursor_column}.eq.{watermark}"]
        equalities += [f"{c}.eq.{v}" for c, v in tiebreaks[:index]]
        equalities.append(f"{column}.gt.{value}")
        clauses.append(f"and({','.join(equalities)})")

    return {"or": f"({','.join(clauses)})"}


def _position_from_row(entity: Entity, row: dict[str, Any]) -> dict[str, str] | None:
    watermark = row.get(entity.cursor_column)
    if watermark is None:
        return None

    position = {entity.cursor_column: str(watermark)}
    for column in entity.tiebreak_columns:
        value = row.get(column)
        if value is None:
            # Without the tiebreak the cursor cannot be trusted to advance.
            return None
        position[column] = str(value)
    return position


def _encode_position(position: dict[str, str]) -> str:
    return json.dumps(position, sort_keys=True)


def _decode_position(raw: str, entity: Entity) -> dict[str, str]:
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {entity.cursor_column: raw}
    if isinstance(decoded, dict):
        return {str(k): str(v) for k, v in decoded.items()}
    return {entity.cursor_column: raw}


def _local(value: Any) -> Any:
    """SQLite has no booleans and no JSON columns; Postgres sends both."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value)
    return value

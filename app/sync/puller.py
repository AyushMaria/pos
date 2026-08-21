"""Pulling reference data down — architecture §9.3.

Per-entity watermark, oldest first:

    GET /rest/v1/products?updated_at=gt.{cursor}&order=updated_at.asc&limit=1000

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


#: Order matters: a barcode references a product, and a price references a
#: product, so products land first. Foreign keys are on locally.
ENTITIES: tuple[Entity, ...] = (
    Entity(
        "tax_codes", "tax_codes",
        ("code", "name", "rate_bp", "is_inclusive", "updated_at"),
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

    def cursor_for(self, entity: str) -> str:
        row = self.db.query_one(
            "SELECT cursor FROM sync_state WHERE entity = ?", (entity,)
        )
        return str(row["cursor"]) if row and row["cursor"] else EPOCH

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
        cursor = self.cursor_for(entity.name)
        total = 0

        while True:
            rows = await self._fetch(entity, cursor)
            if not rows:
                break

            self._write(entity, rows)
            total += len(rows)

            last = rows[-1].get(entity.cursor_column)
            if not last or last == cursor:
                # No forward progress: stop rather than spin. Happens when a
                # page is entirely one timestamp, which is rare and bounded.
                break
            cursor = str(last)
            self._advance(entity.name, cursor)

            if len(rows) < PAGE_SIZE:
                break

        if total:
            log.info("pulled %d %s", total, entity.name)
        return PullResult(entity=entity.name, rows=total, cursor=cursor)

    async def _fetch(self, entity: Entity, cursor: str) -> list[dict[str, Any]]:
        token = self.token_provider()
        if not token:
            return []

        params = {
            entity.cursor_column: f"gt.{cursor}",
            "order": f"{entity.cursor_column}.asc",
            "limit": str(PAGE_SIZE),
            "select": ",".join(entity.columns),
        }
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
        columns = ",".join(entity.columns)
        placeholders = ",".join("?" for _ in entity.columns)
        with self.db.write() as conn:
            for row in rows:
                conn.execute(
                    f"INSERT OR REPLACE INTO {entity.table} ({columns}) "
                    f"VALUES ({placeholders})",
                    tuple(_local(row.get(column)) for column in entity.columns),
                )


def _local(value: Any) -> Any:
    """SQLite has no booleans and no JSON columns; Postgres sends both."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value)
    return value

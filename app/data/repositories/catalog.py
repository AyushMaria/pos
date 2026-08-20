"""Catalogue reads — the scan-to-line path.

Architecture §16 targets a scan on screen inside 150 ms including the local
HTTP round trip, over a catalogue of 200k products. Both queries here are
index-driven and neither grows with catalogue size: the barcode lookup rides
`ux_barcode`, and search rides the FTS5 index.

Returns domain values (`Money`, `TaxCode`) rather than raw rows, so nothing
above this layer ever sees an integer it has to remember is paise.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.data.repositories.base import Repository
from app.domain.money import Money
from app.domain.tax import TaxCode

#: A search returning thousands of rows helps nobody at a counter.
SEARCH_LIMIT = 25


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    """Everything needed to put a product on a line."""

    product_id: str
    sku: str
    name: str
    short_name: str
    uom: str
    is_weighed: bool
    tax_code: TaxCode
    unit_price: Money
    #: >1 when the code scanned was a case/outer (architecture §10.2).
    pack_size: int = 1
    barcode: str | None = None

    @property
    def display_name(self) -> str:
        return self.short_name or self.name


_SELECT = """
    SELECT p.id, p.sku, p.name, p.short_name, p.uom, p.is_weighed,
           t.code AS tax_code, t.name AS tax_name, t.rate_bp, t.is_inclusive,
           pr.price
      FROM products p
      JOIN tax_codes t       ON t.code = p.tax_code
      LEFT JOIN product_prices pr
             ON pr.product_id = p.id AND pr.valid_to IS NULL
"""


def _to_product(
    row: sqlite3.Row, pack_size: int = 1, barcode: str | None = None
) -> CatalogProduct | None:
    if row["price"] is None:
        # A product with no price cannot be sold. Returning None sends the
        # cashier to the same "unknown item" path as a missing barcode, which
        # is a message they can act on rather than a ₹0.00 line.
        return None

    return CatalogProduct(
        product_id=row["id"],
        sku=row["sku"],
        name=row["name"],
        short_name=row["short_name"] or row["name"],
        uom=row["uom"],
        is_weighed=bool(row["is_weighed"]),
        tax_code=TaxCode(
            code=row["tax_code"],
            name=row["tax_name"],
            rate_bp=int(row["rate_bp"]),
            is_inclusive=bool(row["is_inclusive"]),
        ),
        unit_price=Money(int(row["price"])),
        pack_size=pack_size,
        barcode=barcode,
    )


class CatalogRepository(Repository):
    def by_barcode(self, barcode: str) -> CatalogProduct | None:
        row = self._row(
            _SELECT
            + """
              JOIN product_barcodes b ON b.product_id = p.id
             WHERE b.barcode = ? AND b.deleted_at IS NULL
               AND p.deleted_at IS NULL AND p.is_active = 1
             LIMIT 1
            """,
            (barcode,),
        )
        if row is None:
            return None
        pack = self._scalar(
            "SELECT pack_size FROM product_barcodes WHERE barcode = ? "
            "AND deleted_at IS NULL",
            (barcode,),
        )
        return _to_product(row, pack_size=int(pack or 1), barcode=barcode)

    def by_id(self, product_id: str) -> CatalogProduct | None:
        row = self._row(
            _SELECT + " WHERE p.id = ? AND p.deleted_at IS NULL LIMIT 1",
            (product_id,),
        )
        return None if row is None else _to_product(row)

    def by_lookup_key(self, key: str) -> CatalogProduct | None:
        """Resolve a parsed scan.

        A weighed code's key is the item code rather than the whole barcode,
        so both the barcode table and the SKU are tried — the same entry point
        serves an ordinary scan and a `22…` weighing.
        """
        found = self.by_barcode(key)
        if found is not None:
            return found

        row = self._row(
            _SELECT + " WHERE p.sku = ? AND p.deleted_at IS NULL LIMIT 1", (key,)
        )
        return None if row is None else _to_product(row)

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> list[CatalogProduct]:
        """Name and SKU search, for when a code is unreadable or absent.

        38.6% of the pilot catalogue has no barcode at all, so this is not a
        fallback — for more than a third of the shop it is the only way in.
        """
        term = _fts_query(query)
        if not term:
            return []

        rows = self._rows(
            _SELECT
            + """
              JOIN products_fts f ON f.rowid = p.rowid
             WHERE products_fts MATCH ?
               AND p.deleted_at IS NULL AND p.is_active = 1
             ORDER BY rank
             LIMIT ?
            """,
            (term, limit),
        )
        found = [_to_product(row) for row in rows]
        return [product for product in found if product is not None]

    def count(self) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM products") or 0)


def _fts_query(raw: str) -> str:
    """Turn what a cashier typed into an FTS5 prefix query.

    FTS5 treats punctuation as syntax, so `Colgate 200g -` would be a query
    error rather than a search. Everything non-alphanumeric is dropped and
    each surviving token becomes a prefix match, which is what makes the
    search usable while someone is still typing.
    """
    # Punctuation becomes a word break, not a deleted character: "parle-g" has
    # to find "PARLE G BISCUITS", which it cannot do if the hyphen is simply
    # removed and the query becomes "parleg".
    cleaned = "".join(ch if ch.isalnum() else " " for ch in raw)
    return " ".join(f'"{token}"*' for token in cleaned.split())

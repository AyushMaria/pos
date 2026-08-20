"""Seed the terminal's local catalogue from the legacy billing project.

Execution plan §1: *do not build the admin UI early — seed the catalog with an
import script.* Phase 3 needs something to sell; the admin screens are phase 6
and the real catalogue migration is week 18.

    python scripts/import_catalog.py                # everything
    python scripts/import_catalog.py --limit 500    # a smaller dev set
    python scripts/import_catalog.py --dry-run      # report, change nothing

**Two honest warnings about the data this produces.**

*Prices.* The legacy column is a float. Every value is converted through
`Decimal(str(...))` — never through binary floating point — and anything that
is not a whole number of paise is reported and skipped rather than silently
rounded. A product with no price is not importable: a till cannot sell it.

*Tax codes.* The legacy system has no tax column, so rates here are inferred
from the product group using the table below. **That is a development
assumption, not a fact about your business.** It is deliberately a small,
printed, editable table so the owner can review it, and replacing it with real
per-product rates is part of the week-18 catalogue migration (plan §6 step 3).
Do not ship a pilot on inferred GST.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from import_barcode_corpus import fetch_rows, load_credentials  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.data.db import Database  # noqa: E402
from app.data.migrations import migrate  # noqa: E402
from app.domain.barcode import parse  # noqa: E402
from app.domain.ids import new_id  # noqa: E402

# GST rates in basis points. These four are what the pilot store's mix needs.
TAX_CODES = [
    ("GST0", "GST 0%", 0),
    ("GST5", "GST 5%", 500),
    ("GST12", "GST 12%", 1200),
    ("GST18", "GST 18%", 1800),
    ("GST28", "GST 28%", 2800),
]

# ── The development tax assumption. Review this with the owner. ─────────────
# Keyed on the legacy `main_group`. Anything unlisted falls to DEFAULT_TAX_CODE.
GROUP_TAX_CODES = {
    "GROCERY & STAPLES": "GST5",
    "MASALE & SPICES": "GST5",
    "DAIRY & BAKERY": "GST0",
    "FRUITS & VEGETABLES": "GST0",
    "BRANDED FOODS & SNACKS": "GST12",
    "BEVERAGES": "GST12",
    "BABY & CHILD CARE": "GST12",
    "TEXTILE AND HOSIERY": "GST5",
    "PERSONAL CARE": "GST18",
    "HOME & KITCHEN": "GST18",
    "HOUSE HOLD PRODUCTS": "GST18",
    "HOUSE HOLD OTHER": "GST18",
    "CROCKERY & APPLINCES": "GST18",
    "TOYS  AND GAMES SET": "GST18",
    "GENERAL": "GST18",
}
DEFAULT_TAX_CODE = "GST18"


def to_paise(value: object) -> int | None:
    """A legacy float price to whole paise, or None if it cannot be one.

    `Decimal(str(x))` rather than `Decimal(x)`: the former reads the decimal
    the number was written as, the latter reads the binary approximation and
    hands back 499.99999999999994.
    """
    if value is None:
        return None
    try:
        rupees = Decimal(str(value))
    except InvalidOperation:
        return None
    if rupees < 0:
        return None
    paise = rupees * 100
    if paise != paise.to_integral_value():
        return None
    return int(paise)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_directories()

    url, key = load_credentials()
    print("reading the legacy catalogue ...")
    rows = fetch_rows_with_groups(url, key)
    if args.limit:
        rows = rows[: args.limit]
    print(f"fetched {len(rows)} rows\n")

    now = datetime.now(timezone.utc).isoformat()
    skipped: Counter[str] = Counter()
    tax_used: Counter[str] = Counter()

    products: list[tuple[Any, ...]] = []
    barcodes: list[tuple[Any, ...]] = []
    prices: list[tuple[Any, ...]] = []
    seen_barcodes: set[str] = set()

    for index, row in enumerate(rows):
        name = (row.get("item_name") or "").strip()
        if not name:
            skipped["no name"] += 1
            continue

        price = to_paise(row.get("mrp"))
        if price is None:
            # A till cannot ring up a product with no price.
            skipped["no usable price"] += 1
            continue

        group = (row.get("main_group") or "").strip().upper()
        tax_code = GROUP_TAX_CODES.get(group, DEFAULT_TAX_CODE)
        tax_used[tax_code] += 1

        product_id = new_id()
        products.append(
            (
                product_id,
                f"SKU-{index + 1:06d}",
                name,
                name[:24],
                "each",
                0,
                1,
                tax_code,
                1,
                now,
            )
        )
        prices.append((new_id(), product_id, settings.store_code, price, None, now))

        raw = str(row.get("upc_ean_code") or "").strip()
        if not raw:
            skipped["no barcode (searchable by name only)"] += 1
            continue

        scan = parse(raw)
        if not scan.is_usable:
            # Bucketed by cause, not by the individual digits: 106 distinct
            # "expected 7, got 0" lines tell you nothing a total does not.
            cause = "bad check digit" if "check digit" in (scan.error or "") else scan.error
            skipped[f"unusable barcode: {cause}"] += 1
            continue
        if scan.carries_quantity:
            # A code that reads as carrying a weight is not a product identity.
            skipped["barcode reads as a weighed code"] += 1
            continue
        if raw in seen_barcodes:
            # ux_barcode is unique for a reason: the same code on two products
            # means the till picks one at random (plan §6 step 2).
            skipped["duplicate barcode"] += 1
            continue

        seen_barcodes.add(raw)
        barcodes.append(
            (new_id(), product_id, raw, scan.symbology, 1, 1, now)
        )

    print(f"{len(products)} products, {len(barcodes)} barcodes, {len(prices)} prices")
    print("\nskipped:")
    for reason, count in skipped.most_common():
        print(f"  {reason:<45} {count}")

    print("\ntax codes assigned (DEVELOPMENT ASSUMPTION — review before pilot):")
    for code, count in tax_used.most_common():
        print(f"  {code:<8} {count}")

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    db = Database(settings.db_path)
    migrate(db)

    with db.write() as conn:
        conn.execute("DELETE FROM product_prices")
        conn.execute("DELETE FROM product_barcodes")
        conn.execute("DELETE FROM products")
        conn.executemany(
            "INSERT INTO tax_codes (code, name, rate_bp, is_inclusive, updated_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT (code) DO UPDATE SET rate_bp = excluded.rate_bp",
            [(code, name, rate, now) for code, name, rate in TAX_CODES],
        )
        conn.executemany(
            "INSERT INTO products (id, sku, name, short_name, uom, is_weighed, "
            "track_stock, tax_code, is_active, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            products,
        )
        conn.executemany(
            "INSERT INTO product_barcodes (id, product_id, barcode, symbology, "
            "pack_size, is_primary, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            barcodes,
        )
        conn.executemany(
            "INSERT INTO product_prices (id, product_id, store_id, price, cost, "
            "valid_from) VALUES (?, ?, ?, ?, ?, ?)",
            prices,
        )

    print(f"\nwrote {settings.db_path}")
    db.close()
    return 0


def fetch_rows_with_groups(url: str, key: str) -> list[dict[str, Any]]:
    """The legacy rows, including the grouping columns the tax guess needs."""
    import import_barcode_corpus as importer

    original = importer.LEGACY_BARCODE_COLUMN, importer.LEGACY_NAME_COLUMN
    try:
        # fetch_rows selects only barcode and name; widen it for this import.
        importer.LEGACY_BARCODE_COLUMN = "upc_ean_code,main_group,sub_group,mrp"
        importer.LEGACY_NAME_COLUMN = "item_name"
        return fetch_rows(url, key)
    finally:
        importer.LEGACY_BARCODE_COLUMN, importer.LEGACY_NAME_COLUMN = original


if __name__ == "__main__":
    raise SystemExit(main())

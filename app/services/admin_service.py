"""Catalogue editing, straight to the cloud — phase 6 decision 2.

Products are pull-only on a terminal: the puller writes them, server wins
always, and nothing local edits them. Making them two-way is roughly a second
phase 6, so the admin screens skip the local database entirely. Nothing here
touches SQLite and nothing here enters the outbox. **Editing the catalogue
needs the internet, and the owner is told so in those words.**

"Directly to Supabase" is about the *write path*, not about the transport. The
requests below are made by this process rather than by the page, because the
page must never hold a token that outlives the launch it was minted for
(architecture §5) — and it holds the user's own access token rather than a
service key, so PostgREST evaluates precisely the RLS every other client gets.
A manager who lacks `product.edit` is refused by Postgres, not by this file.

That is deliberate: the three enforcement points (architecture §11.1) stay
intact. React hides the control, the router rejects the request, and RLS
rejects the write. Only the third is security.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.domain.barcode import parse as parse_barcode
from app.services.auth_service import SessionStore

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=3.0)


class AdminUnavailable(RuntimeError):
    """The cloud could not be reached, or this terminal has no cloud at all.

    Not a rejection. The caller should say "this needs the internet" rather
    than "you may not do that" — they are different problems with different
    next actions.
    """


class AdminRejected(RuntimeError):
    """Postgres refused the write. Usually RLS, sometimes a constraint."""


class DuplicateBarcode(AdminRejected):
    """This code is already on another product.

    Carries the holder because the constraint name is useless to a shopkeeper:
    they need to know *which* product to go and look at.
    """

    def __init__(self, barcode: str, sku: str | None, name: str | None) -> None:
        self.barcode = barcode
        self.sku = sku
        self.name = name
        if sku and name:
            super().__init__(f"{barcode} is already on {sku} — {name}")
        else:
            super().__init__(f"{barcode} is already on another product")


class ScanNotCatalogued(AdminRejected):
    """Closed as catalogued, but the code is still on nothing (0021).

    The backstop for the queue's oldest bug: a close that changed the row and
    catalogued nothing, and looked from the screen exactly like a close that
    worked. The database refuses it now, and this is that refusal in words a
    shopkeeper can act on.
    """

    def __init__(self) -> None:
        super().__init__(
            "that code is still not on any product — attach it to one, "
            "or dismiss the scan if it is never going to be a product"
        )


class DuplicateSku(AdminRejected):
    """This SKU is taken.

    The predictable failure of the new-product form, and the one the raw
    constraint text serves worst: somebody typing a code they believe is free
    needs to be told it is not, rather than shown the name of an index.
    """

    def __init__(self, sku: str) -> None:
        self.sku = sku
        super().__init__(f"{sku} is already the code for another product")


@dataclass(frozen=True)
class AdminProduct:
    product_id: str
    sku: str
    name: str
    short_name: str | None
    uom: str
    tax_code: str
    is_weighed: bool
    track_stock: bool
    is_active: bool


@dataclass(frozen=True)
class AdminBarcode:
    barcode_id: str
    product_id: str
    barcode: str
    symbology: str
    pack_size: int
    is_primary: bool


@dataclass(frozen=True)
class AdminPrice:
    price_id: str
    product_id: str
    store_id: str
    price: int
    valid_from: str
    valid_to: str | None


@dataclass(frozen=True)
class UnknownScan:
    scan_id: str
    store_id: str
    barcode: str
    scanned_at: str
    terminal_id: str | None
    resolved: bool
    #: 'catalogued', 'dismissed', or None — open, or closed before 0021.
    resolution: str | None = None


@dataclass(frozen=True)
class StockLevel:
    """What a product's level is now. Quantities are thousandths."""

    product_id: str
    store_id: str
    on_hand: int
    reorder_point: int


@dataclass(frozen=True)
class LowStockRow:
    product_id: str
    sku: str
    name: str
    uom: str
    on_hand: int
    reorder_point: int


def _product(row: dict[str, Any]) -> AdminProduct:
    return AdminProduct(
        product_id=row["id"],
        sku=row["sku"],
        name=row["name"],
        short_name=row.get("short_name"),
        uom=row["uom"],
        tax_code=row["tax_code"],
        is_weighed=bool(row["is_weighed"]),
        track_stock=bool(row["track_stock"]),
        is_active=bool(row["is_active"]),
    )


def _barcode(row: dict[str, Any]) -> AdminBarcode:
    return AdminBarcode(
        barcode_id=row["id"],
        product_id=row["product_id"],
        barcode=row["barcode"],
        symbology=row["symbology"],
        pack_size=int(row["pack_size"]),
        is_primary=bool(row["is_primary"]),
    )


def _price(row: dict[str, Any]) -> AdminPrice:
    return AdminPrice(
        price_id=row["id"],
        product_id=row["product_id"],
        store_id=row["store_id"],
        price=int(row["price"]),
        valid_from=row["valid_from"],
        valid_to=row.get("valid_to"),
    )


def _scan(row: dict[str, Any]) -> UnknownScan:
    return UnknownScan(
        scan_id=row["id"],
        store_id=row["store_id"],
        barcode=row["barcode"],
        scanned_at=row["scanned_at"],
        terminal_id=row.get("terminal_id"),
        resolved=bool(row["resolved"]),
        resolution=row.get("resolution"),
    )


def _stock_level(row: dict[str, Any]) -> StockLevel:
    return StockLevel(
        product_id=str(row["product_id"]),
        store_id=str(row["store_id"]),
        on_hand=int(row["on_hand"]),
        reorder_point=int(row["reorder_point"]),
    )


def _low_stock(row: dict[str, Any]) -> LowStockRow:
    return LowStockRow(
        product_id=row["product_id"],
        sku=row["sku"],
        name=row["name"],
        uom=row["uom"],
        on_hand=int(row["on_hand"]),
        reorder_point=int(row["reorder_point"]),
    )


class AdminService:
    def __init__(
        self,
        base_url: str,
        anon_key: str,
        sessions: SessionStore,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        self.sessions = sessions
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.anon_key)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _headers(self, *, prefer: str | None = None) -> dict[str, str]:
        if not self.configured:
            raise AdminUnavailable("this terminal has no cloud project configured")
        token = self.sessions.access_token
        if not token:
            # Signed in from the offline cache: there is a session, but no
            # cloud identity behind it. Reading the catalogue still works from
            # the local copy; editing it cannot.
            raise AdminUnavailable("signed in offline — editing needs the internet")
        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    async def _send(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        prefer: str | None = None,
    ) -> list[dict[str, Any]]:
        client = await self._http()
        try:
            response = await client.request(
                method,
                f"{self.base_url}/rest/v1/{table}",
                headers=self._headers(prefer=prefer),
                params=params,
                json=json,
            )
        except httpx.HTTPError as exc:
            raise AdminUnavailable(str(exc)) from exc

        if response.status_code in (401, 403):
            raise AdminRejected("the server refused this edit for this user")
        if response.status_code >= 500:
            raise AdminUnavailable(f"the server returned {response.status_code}")
        if response.status_code >= 400:
            raise AdminRejected(_detail(response))

        if not response.content:
            return []
        body = response.json()
        return body if isinstance(body, list) else [body]

    # ── Products ──────────────────────────────────────────────────────────

    async def search_products(self, q: str, limit: int = 25) -> list[AdminProduct]:
        """Name or SKU fragment. The same two ways in as the till's search."""
        pattern = f"*{q}*"
        rows = await self._send(
            "GET",
            "products",
            params={
                "or": f"(sku.ilike.{pattern},name.ilike.{pattern})",
                "deleted_at": "is.null",
                "order": "sku.asc",
                "limit": str(limit),
                "select": "id,sku,name,short_name,uom,tax_code,is_weighed,"
                "track_stock,is_active",
            },
        )
        return [_product(row) for row in rows]

    async def product(self, product_id: str) -> AdminProduct | None:
        rows = await self._send(
            "GET",
            "products",
            params={
                "id": f"eq.{product_id}",
                "select": "id,sku,name,short_name,uom,tax_code,is_weighed,"
                "track_stock,is_active",
            },
        )
        return _product(rows[0]) if rows else None

    async def create_product(self, **fields: Any) -> AdminProduct:
        """A new product, which cannot be sold until it has a code and a price.

        Deliberately does not create either. A product is one row; making it
        sellable is two more, each with its own way of going wrong, and doing
        all three here would mean deciding what to leave behind when the
        second one fails.
        """
        try:
            rows = await self._send(
                "POST", "products", json=_clean(fields), prefer="return=representation"
            )
        except AdminRejected as exc:
            if "23505" in str(exc) or "duplicate key" in str(exc):
                raise DuplicateSku(str(fields.get("sku", ""))) from exc
            raise
        return _product(rows[0])

    async def update_product(self, product_id: str, **fields: Any) -> AdminProduct:
        rows = await self._send(
            "PATCH",
            "products",
            params={"id": f"eq.{product_id}"},
            json=_clean(fields),
            prefer="return=representation",
        )
        if not rows:
            # PostgREST returns 200 with an empty body when RLS filtered the
            # row out. Success and "you may not" look identical on the wire.
            raise AdminRejected("no product was changed — check your permissions")
        return _product(rows[0])

    # ── Barcodes ──────────────────────────────────────────────────────────

    async def barcodes(self, product_id: str) -> list[AdminBarcode]:
        rows = await self._send(
            "GET",
            "product_barcodes",
            params={
                "product_id": f"eq.{product_id}",
                "deleted_at": "is.null",
                "order": "is_primary.desc,barcode.asc",
                "select": "id,product_id,barcode,symbology,pack_size,is_primary",
            },
        )
        return [_barcode(row) for row in rows]

    async def add_barcode(
        self, product_id: str, barcode: str, pack_size: int = 1
    ) -> AdminBarcode:
        """Symbology is derived, not asked for.

        The person typing a code off a packet knows the digits and nothing
        else; the parser already classifies them for the till, so it can
        classify them here and the form loses a field.
        """
        scan = parse_barcode(barcode)
        try:
            rows = await self._send(
                "POST",
                "product_barcodes",
                json={
                    "product_id": product_id,
                    "barcode": barcode,
                    "symbology": scan.symbology,
                    "pack_size": pack_size,
                },
                prefer="return=representation",
            )
        except AdminRejected as exc:
            if "23505" in str(exc) or "duplicate key" in str(exc):
                raise await self._duplicate(barcode) from exc
            raise
        return _barcode(rows[0])

    async def _duplicate(self, barcode: str) -> DuplicateBarcode:
        """Turn a constraint violation into a sentence with a product in it."""
        try:
            rows = await self._send(
                "GET",
                "product_barcodes",
                params={
                    "barcode": f"eq.{barcode}",
                    "deleted_at": "is.null",
                    "select": "product_id,products(sku,name)",
                },
            )
        except (AdminRejected, AdminUnavailable):
            return DuplicateBarcode(barcode, None, None)

        holder = (rows[0].get("products") or {}) if rows else {}
        return DuplicateBarcode(barcode, holder.get("sku"), holder.get("name"))

    async def withdraw_barcode(self, barcode_id: str) -> None:
        """Soft delete. The partial unique index frees the code immediately,
        and last month's `sale_lines.barcode_scanned` still resolves."""
        rows = await self._send(
            "PATCH",
            "product_barcodes",
            params={"id": f"eq.{barcode_id}"},
            json={"deleted_at": "now()"},
            prefer="return=representation",
        )
        if not rows:
            raise AdminRejected("no barcode was withdrawn — check your permissions")

    # ── Prices ────────────────────────────────────────────────────────────

    async def prices(self, product_id: str, store_id: str) -> list[AdminPrice]:
        rows = await self._send(
            "GET",
            "product_prices",
            params={
                "product_id": f"eq.{product_id}",
                "store_id": f"eq.{store_id}",
                "deleted_at": "is.null",
                "order": "valid_from.desc",
                "select": "id,product_id,store_id,price,valid_from,valid_to",
            },
        )
        return [_price(row) for row in rows]

    async def set_price(
        self, product_id: str, store_id: str, price: int
    ) -> AdminPrice:
        """Open the new price, then close the old one.

        That order is deliberate. PostgREST has no transaction across two
        calls, so one of them can be the last thing that happens. Closing
        first and failing leaves the product with no price at all, which stops
        the till selling it; opening first and failing leaves two open prices
        for as long as it takes somebody to notice, and the till reads the
        latest `valid_from`. An overlap is recoverable, a gap is a customer
        waiting at the counter.
        """
        opened = await self._send(
            "POST",
            "product_prices",
            json={"product_id": product_id, "store_id": store_id, "price": price},
            prefer="return=representation",
        )
        new = _price(opened[0])

        closed = await self._send(
            "PATCH",
            "product_prices",
            params={
                "product_id": f"eq.{product_id}",
                "store_id": f"eq.{store_id}",
                "valid_to": "is.null",
                "id": f"neq.{new.price_id}",
            },
            json={"valid_to": "now()"},
            prefer="return=representation",
        )
        if len(closed) > 1:
            log.warning(
                "closed %d overlapping prices for product %s — "
                "an earlier set_price probably half-finished",
                len(closed),
                product_id,
            )
        return new

    async def stock_level(self, product_id: str, store_id: str) -> StockLevel | None:
        """What this product's level and minimum are now, or None if untracked.

        None is the ordinary answer for a product that has never been counted
        or received: `stock_levels` gets its rows from `apply_stock_delta`, so
        one exists only once the ledger has said something about the product.
        """
        rows = await self._send(
            "GET",
            "stock_levels",
            params={
                "product_id": f"eq.{product_id}",
                "store_id": f"eq.{store_id}",
                "limit": "1",
            },
        )
        return _stock_level(rows[0]) if rows else None

    async def set_reorder_point(
        self, product_id: str, store_id: str, reorder_point: int
    ) -> StockLevel | None:
        """The minimum below which this product needs reordering.

        A PATCH and never an upsert. 0018 grants `update (reorder_point)` and
        no insert at all, so there is no row to write for a product that has
        never been counted or received — and inventing one would mean writing
        an `on_hand` the ledger never agreed to.

        Returns the level as it now stands, taken from the write's own
        representation rather than read back. Reading it back through
        `low_stock` would have been wrong in the ordinary case: that view
        selects `reorder_point > 0 and on_hand <= reorder_point`, so a product
        that is *not* low — which is most of them, and the point of setting a
        minimum early — would come back empty and look like a failure.

        `None` means nothing was written, which has two readings this layer
        cannot separate: no stock row yet, or RLS filtered it away. The router
        turns that into a sentence about counting the product in, because that
        is the true answer in almost every case and the actionable one in both.
        """
        rows = await self._send(
            "PATCH",
            "stock_levels",
            params={
                "product_id": f"eq.{product_id}",
                "store_id": f"eq.{store_id}",
            },
            json={"reorder_point": reorder_point},
            prefer="return=representation",
        )
        return _stock_level(rows[0]) if rows else None

    # ── The unknown-scan queue ────────────────────────────────────────────

    async def unknown_scans(
        self, store_id: str, *, resolved: bool = False, limit: int = 100
    ) -> list[UnknownScan]:
        return [
            _scan(row)
            for row in await self._send(
                "GET",
                "unknown_scans",
                params={
                    "store_id": f"eq.{store_id}",
                    "resolved": f"is.{str(resolved).lower()}",
                    "order": "scanned_at.desc",
                    "limit": str(limit),
                    "select": (
                        "id,store_id,barcode,scanned_at,terminal_id,"
                        "resolved,resolution"
                    ),
                },
            )
        ]

    async def resolve_scan(self, scan_id: str) -> None:
        """Close a queue entry, the code now being on a product.

        0021 checks that claim rather than taking it: if the barcode is on
        nothing when this lands, the update is refused. So this method is
        only ever correct *after* the barcode has been attached, which is the
        order `attachAndClose` already uses on the screen.

        Note what it does *not* do. It does not go back and repoint the
        `sale_lines` rows that were rung against the placeholder: a sold line
        records what was charged and what the cashier typed, whatever the
        catalogue later decides the item is called. `sale_lines` has no update
        policy at all (0003), so that guarantee is enforced by Postgres rather
        than by this comment.
        """
        await self._close_scan(scan_id, "catalogued")

    async def dismiss_scan(self, scan_id: str) -> None:
        """Close a queue entry that is never going to be a product.

        A torn label, a loyalty card, a code off a delivery note. This is a
        real answer and the queue has always needed it — what it did not need
        was for the answer to be indistinguishable from having done the work,
        which is what "Done" meant before 0021.
        """
        await self._close_scan(scan_id, "dismissed")

    async def _close_scan(self, scan_id: str, resolution: str) -> None:
        """One PATCH, two outcomes, and neither of them silent.

        `resolved` and `resolution` are sent together because 0021 refuses
        them apart: closing an entry says how it was closed, or it does not
        close.
        """
        try:
            rows = await self._send(
                "PATCH",
                "unknown_scans",
                params={"id": f"eq.{scan_id}"},
                json={"resolved": True, "resolution": resolution},
                prefer="return=representation",
            )
        except AdminRejected as exc:
            if "not on any product yet" in str(exc):
                raise ScanNotCatalogued() from exc
            raise
        if not rows:
            raise AdminRejected("no scan was resolved — check your permissions")

    # ── Low stock ─────────────────────────────────────────────────────────

    async def low_stock(self, store_id: str, limit: int = 100) -> list[LowStockRow]:
        return [
            _low_stock(row)
            for row in await self._send(
                "GET",
                "low_stock",
                params={
                    "store_id": f"eq.{store_id}",
                    "order": "on_hand.asc",
                    "limit": str(limit),
                },
            )
        ]


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the caller did not set, so a PATCH touches what it names."""
    return {key: value for key, value in fields.items() if value is not None}


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        parts = [str(body.get(key)) for key in ("code", "message", "details") if body.get(key)]
        return " — ".join(parts) or str(body)
    return str(body)

"""Catalogue admin — phase 6 slice 6.

The first screens in this application that write to the cloud instead of to
the terminal. Everything else here degrades when the internet goes; these
stop, and say so. That is decision 2 of the phase 6 plan, and the line it
draws is: **selling something unrecognised works offline, cataloguing it
properly does not.**

Two failures have to stay distinguishable all the way to the screen, because
the next action differs:

  * 503 — the cloud is unreachable. Try later, from anywhere.
  * 422 — Postgres refused the write. Trying later will not help.

`AdminUnavailable` is the first and `AdminRejected` the second, and the UI
client already reads 503 as "could not reach" rather than "you may not".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_admin_service, require
from app.api.schemas import (
    AdminBarcodeOut,
    AdminPriceOut,
    AdminProductOut,
    AdminProductsResponse,
    AuditEntryOut,
    AuditLogResponse,
    BarcodeAddRequest,
    BarcodesResponse,
    LowStockOut,
    LowStockResponse,
    PriceSetRequest,
    PricesResponse,
    ProductCreateRequest,
    ProductUpdateRequest,
    ReorderPointSetRequest,
    StockLevelOut,
    UnknownScanOut,
    UnknownScansResponse,
)
from app.domain import permissions
from app.domain.identity import Session
from app.services.admin_service import (
    AdminBarcode,
    AdminPrice,
    AdminProduct,
    AdminRejected,
    AdminService,
    AdminUnavailable,
    DuplicateBarcode,
    DuplicateSku,
    LowStockRow,
    StockLevel,
    UnknownScan,
)

router = APIRouter(prefix="/admin", tags=["admin"])

Admin = Annotated[AdminService, Depends(get_admin_service)]

CanRead = Annotated[Session, Depends(require(permissions.PRODUCT_READ))]
CanCreate = Annotated[Session, Depends(require(permissions.PRODUCT_CREATE))]
CanEdit = Annotated[Session, Depends(require(permissions.PRODUCT_EDIT))]
#: `user.manage` gates exactly one thing today, and it is read-only: the
#: audit log. The key has existed since §11.1 with no route behind it,
#: which `NO_API_SURFACE` has recorded as a hole rather than a decision.
CanReadAudit = Annotated[Session, Depends(require(permissions.USER_MANAGE))]


def _offline(exc: AdminUnavailable) -> HTTPException:
    """503, and a sentence an owner can act on.

    Deliberately not 500. Nothing is broken — the shop is somewhere the
    internet is not, which the plan says is the normal case for this shop.
    """
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"Editing the catalogue needs the internet. {exc}",
    )


def _refused(exc: AdminRejected) -> HTTPException:
    """422 rather than 403.

    403 is what `require()` returns when this session may not do this at all.
    Anything reaching here got past that and was still refused by the
    database, which is a different sentence and a different fix.
    """
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


def _product_out(product: AdminProduct) -> AdminProductOut:
    return AdminProductOut(
        product_id=product.product_id,
        sku=product.sku,
        name=product.name,
        short_name=product.short_name,
        uom=product.uom,
        tax_code=product.tax_code,
        is_weighed=product.is_weighed,
        track_stock=product.track_stock,
        is_active=product.is_active,
    )


def _barcode_out(barcode: AdminBarcode) -> AdminBarcodeOut:
    return AdminBarcodeOut(
        barcode_id=barcode.barcode_id,
        product_id=barcode.product_id,
        barcode=barcode.barcode,
        symbology=barcode.symbology,
        pack_size=barcode.pack_size,
        is_primary=barcode.is_primary,
    )


def _price_out(price: AdminPrice) -> AdminPriceOut:
    return AdminPriceOut(
        price_id=price.price_id,
        product_id=price.product_id,
        store_id=price.store_id,
        price=price.price,
        valid_from=price.valid_from,
        valid_to=price.valid_to,
    )


def _scan_out(scan: UnknownScan) -> UnknownScanOut:
    return UnknownScanOut(
        scan_id=scan.scan_id,
        store_id=scan.store_id,
        barcode=scan.barcode,
        scanned_at=scan.scanned_at,
        terminal_id=scan.terminal_id,
        resolved=scan.resolved,
        resolution=scan.resolution,
    )


def _stock_level_out(level: StockLevel) -> StockLevelOut:
    return StockLevelOut(
        product_id=level.product_id,
        store_id=level.store_id,
        on_hand=level.on_hand,
        reorder_point=level.reorder_point,
    )


def _low_stock_out(row: LowStockRow) -> LowStockOut:
    return LowStockOut(
        product_id=row.product_id,
        sku=row.sku,
        name=row.name,
        uom=row.uom,
        on_hand=row.on_hand,
        reorder_point=row.reorder_point,
    )


# ── Products ──────────────────────────────────────────────────────────────


@router.get("/products", response_model=AdminProductsResponse)
async def search_products(
    admin: Admin,
    session: CanRead,
    q: str = Query(min_length=1, description="Name or SKU fragment"),
    limit: int = Query(default=25, ge=1, le=100),
) -> AdminProductsResponse:
    try:
        found = await admin.search_products(q, limit)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    return AdminProductsResponse(products=[_product_out(p) for p in found])


@router.get("/products/{product_id}", response_model=AdminProductOut)
async def get_product(product_id: str, admin: Admin, session: CanRead) -> AdminProductOut:
    try:
        product = await admin.product(product_id)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no product with that id")
    return _product_out(product)


@router.post("/products", response_model=AdminProductOut, status_code=201)
async def create_product(
    body: ProductCreateRequest, admin: Admin, session: CanCreate
) -> AdminProductOut:
    try:
        product = await admin.create_product(**body.model_dump())
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except DuplicateSku as exc:
        # 409 for the same reason a duplicate barcode is: the request was
        # well formed and the answer is "that one is taken", which is a
        # conflict rather than a mistake in what was sent.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    return _product_out(product)


@router.patch("/products/{product_id}", response_model=AdminProductOut)
async def update_product(
    product_id: str, body: ProductUpdateRequest, admin: Admin, session: CanEdit
) -> AdminProductOut:
    try:
        product = await admin.update_product(
            product_id, **body.model_dump(exclude_unset=True)
        )
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    return _product_out(product)


# ── Barcodes ──────────────────────────────────────────────────────────────


@router.get("/products/{product_id}/barcodes", response_model=BarcodesResponse)
async def list_barcodes(
    product_id: str, admin: Admin, session: CanRead
) -> BarcodesResponse:
    try:
        found = await admin.barcodes(product_id)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    return BarcodesResponse(barcodes=[_barcode_out(b) for b in found])


@router.post(
    "/products/{product_id}/barcodes", response_model=AdminBarcodeOut, status_code=201
)
async def add_barcode(
    product_id: str, body: BarcodeAddRequest, admin: Admin, session: CanEdit
) -> AdminBarcodeOut:
    """409 when the code is already on something else.

    The message names the product holding it. `ux_product_barcodes_barcode`
    is the most useful constraint in the schema and the least useful error
    string: nobody standing at a counter can act on "duplicate key value
    violates unique constraint".
    """
    try:
        created = await admin.add_barcode(product_id, body.barcode, body.pack_size)
    except DuplicateBarcode as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    return _barcode_out(created)


@router.delete("/barcodes/{barcode_id}", status_code=204)
async def withdraw_barcode(barcode_id: str, admin: Admin, session: CanEdit) -> None:
    """DELETE on the wire, a soft delete in the database.

    The row survives so that a `sale_lines.barcode_scanned` from last month
    still resolves to something; the partial unique index frees the code for
    another product immediately.
    """
    try:
        await admin.withdraw_barcode(barcode_id)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc


# ── Prices ────────────────────────────────────────────────────────────────


@router.get("/products/{product_id}/prices", response_model=PricesResponse)
async def list_prices(
    product_id: str, admin: Admin, session: CanRead
) -> PricesResponse:
    try:
        found = await admin.prices(product_id, session.store_id)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    return PricesResponse(prices=[_price_out(p) for p in found])


@router.put("/products/{product_id}/price", response_model=AdminPriceOut)
async def set_price(
    product_id: str, body: PriceSetRequest, admin: Admin, session: CanEdit
) -> AdminPriceOut:
    """The price for *this* store. `store_id` comes from the session, never
    from the body — a manager edits the shop they are signed in to."""
    try:
        price = await admin.set_price(product_id, session.store_id, body.price)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    return _price_out(price)


# ── The audit log ─────────────────────────────────────────────────────────


@router.get("/audit", response_model=AuditLogResponse)
async def audit_log(
    admin: Admin,
    session: CanReadAudit,
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    action: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> AuditLogResponse:
    """The first screen whose whole job is to answer "who did that?".

    Read-only, and read under the caller's own token: `audit_log_select` wants
    `user.manage` and does the deciding. `require()` here is the second of the
    three enforcement points — it makes the refusal a 403 with a sentence
    instead of an empty list from PostgREST, which is the difference between
    "you may not" and "nothing happened".

    The actions list comes back with the entries so the filter offers what the
    log actually contains. A menu of constants would offer filters that return
    nothing, which reads as a broken screen rather than a quiet shop.
    """
    try:
        entries = await admin.audit_log(
            store_id=session.store_id,
            since=since,
            until=until,
            action=action,
            entity_id=entity_id,
            limit=limit,
        )
        actions = await admin.audit_actions(store_id=session.store_id)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc

    return AuditLogResponse(
        entries=[
            AuditEntryOut(
                id=entry.id,
                action=entry.action,
                entity=entry.entity,
                entity_id=entry.entity_id,
                store_id=entry.store_id,
                occurred_at=entry.occurred_at,
                actor_id=entry.actor_id,
                approver_id=entry.approver_id,
                actor_code=entry.actor_code,
                actor_name=entry.actor_name,
                approver_code=entry.approver_code,
                approver_name=entry.approver_name,
                before=entry.before,
                after=entry.after,
            )
            for entry in entries
        ],
        actions=actions,
    )


# ── The unknown-scan queue ────────────────────────────────────────────────


@router.get("/unknown-scans", response_model=UnknownScansResponse)
async def unknown_scans(
    admin: Admin,
    session: CanEdit,
    resolved: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> UnknownScansResponse:
    """Gated on `product.edit`, not `product.read`.

    Working this queue is catalogue editing — the plan moved it here from
    slice 5 for exactly that reason. A cashier files entries and does not
    read them back.
    """
    try:
        found = await admin.unknown_scans(
            session.store_id, resolved=resolved, limit=limit
        )
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    return UnknownScansResponse(scans=[_scan_out(s) for s in found])


@router.post("/unknown-scans/{scan_id}/resolve", status_code=204)
async def resolve_scan(scan_id: str, admin: Admin, session: CanEdit) -> None:
    """Close an entry, having catalogued the thing behind it.

    Two endpoints rather than one with a flag, because they are two different
    claims about the world and only one of them can be checked. This one says
    the code is now on a product; 0021 refuses it if that is not true, so a
    422 here means the barcode never got attached and the entry is still
    waiting — which is the honest outcome.

    Resolving changes nothing about the sales that were already rung against
    the placeholder. A sold line records what was charged; the catalogue
    catching up later does not rewrite it, and `sale_lines` has no update
    policy for anyone to try.
    """
    try:
        await admin.resolve_scan(scan_id)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc


@router.post("/unknown-scans/{scan_id}/dismiss", status_code=204)
async def dismiss_scan(scan_id: str, admin: Admin, session: CanEdit) -> None:
    """Close an entry that is never going to be a product.

    The other half of the queue, and the half the screen used to call "Done".
    A torn label or a customer's loyalty card is a real answer; recording it
    as the same event as cataloguing an item is what let five scans of one
    biscuit packet close without anything being catalogued at all.

    No body and no reason field. Who and when is enough to go back and ask,
    and a required reason on a button people press twenty times a morning
    gets typed as "x".
    """
    try:
        await admin.dismiss_scan(scan_id)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc


# ── Low stock ─────────────────────────────────────────────────────────────


@router.get("/products/{product_id}/stock-level", response_model=StockLevelOut)
async def stock_level(
    product_id: str, admin: Admin, session: CanRead
) -> StockLevelOut:
    """This product's level and reorder point in the signed-in store."""
    try:
        level = await admin.stock_level(product_id, session.store_id)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc

    if level is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "this product has no stock record yet",
        )
    return _stock_level_out(level)


@router.put(
    "/products/{product_id}/reorder-point", response_model=StockLevelOut
)
async def set_reorder_point(
    product_id: str, body: ReorderPointSetRequest, admin: Admin, session: CanEdit
) -> StockLevelOut:
    """The level at which this product wants reordering.

    The store comes from the session, never the body — the same rule as
    `set_price`. A manager signed in to one shop cannot set another shop's
    minimum by naming it.
    """
    try:
        level = await admin.set_reorder_point(
            product_id, session.store_id, body.reorder_point
        )
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc

    if level is None:
        # 0018 grants update and no insert, so there is nothing to write until
        # the product has a level. 409 rather than 404: the product exists, the
        # request was well formed, and the answer is "not yet" — which is a
        # state the person can change, and the sentence says how.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this product has no stock yet — count it in or receive it first, "
            "then set the reorder point",
        )
    return _stock_level_out(level)


@router.get("/low-stock", response_model=LowStockResponse)
async def low_stock(
    admin: Admin,
    session: CanRead,
    limit: int = Query(default=100, ge=1, le=500),
) -> LowStockResponse:
    """`on_hand <= reorder_point` where the reorder point was set at all.

    A query behind a screen, with no alerting behind it until somebody asks.
    """
    try:
        rows = await admin.low_stock(session.store_id, limit)
    except AdminUnavailable as exc:
        raise _offline(exc) from exc
    except AdminRejected as exc:
        raise _refused(exc) from exc
    return LowStockResponse(rows=[_low_stock_out(r) for r in rows])

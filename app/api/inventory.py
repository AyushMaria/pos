"""Inventory endpoints — architecture §9.4.

Receiving, counting and adjusting. Thin, like every router here: the
arithmetic is in `app/domain/receiving.py` and the writes are one transaction
in `InventoryRepository`.

Each is gated on its own permission rather than a single `inventory` one.
Receiving a delivery and adjusting stock by hand are different acts of trust —
one has a supplier's document behind it, the other has only a sentence
somebody typed — and the matrix already separates them (§11.1).

Nothing here waits on the cloud. A delivery arrives when it arrives.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_inventory_service, require
from app.api.schemas import (
    AdjustmentRequest,
    MovementsResponse,
    ReceiptLineOut,
    ReceiptPreviewRequest,
    ReceiptRequest,
    StockCountRequest,
)
from app.domain import permissions
from app.domain.identity import Session
from app.domain.receiving import ReceivingError
from app.services.cart_service import UnknownBarcode, UnreadableBarcode
from app.services.inventory_service import InventoryService, UnknownProduct

router = APIRouter(prefix="/inventory", tags=["inventory"])

Inventory = Annotated[InventoryService, Depends(get_inventory_service)]


def _no_such_product(exc: UnknownProduct) -> HTTPException:
    """404, with the sentence the service wrote.

    Until phase 7's matrix probed these routes with `"no-such-product"`, this
    was a 200: the row went to the ledger and the outbox, and the cloud's
    foreign key refused it hours later into the failures queue. The refusal
    now happens while the person who typed the id is still looking.
    """
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


def _refused(exc: ReceivingError) -> HTTPException:
    """A movement the domain refused to make.

    422 rather than 400: the request was understood and is not allowed to mean
    anything. The message is the domain's own, because it was written to be
    read by whoever typed the number.
    """
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


@router.post("/receipts/preview", response_model=ReceiptLineOut)
def preview_line(
    body: ReceiptPreviewRequest,
    inventory: Inventory,
    session: Annotated[Session, Depends(require(permissions.STOCK_RECEIVE))],
) -> ReceiptLineOut:
    """What scanning this code that many times would receive.

    Exists so the screen can show a running list before anything is written.
    A delivery entered wrong and committed is a correction with a reason
    attached; caught here it is a keystroke.
    """
    try:
        line = inventory.preview_receipt_line(body.barcode, body.packs)
    except UnreadableBarcode as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"That code could not be read: {exc.reason}. Check the digits.",
        ) from exc
    except UnknownBarcode as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No product found for {exc.barcode}. Catalogue it before receiving it.",
        ) from exc
    except ReceivingError as exc:
        raise _refused(exc) from exc

    return ReceiptLineOut(
        product_id=line.product.product_id,
        description=line.product.display_name,
        barcode=body.barcode,
        packs=line.packs,
        pack_size=line.product.pack_size,
        units=line.units,
        delta_milli=line.movement.delta_milli,
    )


@router.post("/receipts", response_model=MovementsResponse)
def receive(
    body: ReceiptRequest,
    inventory: Inventory,
    session: Annotated[Session, Depends(require(permissions.STOCK_RECEIVE))],
) -> MovementsResponse:
    """Commit a delivery. One transaction — all lines or none.

    Half a delivery is worse than none: the shelf and the screen disagree and
    nothing says which half arrived.
    """
    try:
        lines = [
            inventory.preview_receipt_line(line.barcode, line.packs)
            for line in body.lines
        ]
        written = inventory.receive(session, lines)
    except UnreadableBarcode as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"That code could not be read: {exc.reason}. Check the digits.",
        ) from exc
    except UnknownBarcode as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No product found for {exc.barcode}. Catalogue it before receiving it.",
        ) from exc
    except ReceivingError as exc:
        raise _refused(exc) from exc

    return MovementsResponse(movement_ids=written)


@router.post("/counts", response_model=MovementsResponse)
def count(
    body: StockCountRequest,
    inventory: Inventory,
    session: Annotated[Session, Depends(require(permissions.STOCK_COUNT))],
) -> MovementsResponse:
    """A stock count for a section.

    The correction is written, not the count: `delta = counted - expected`,
    with `expected` read in the same transaction so a sale landing mid-count
    is not silently reversed. A product that matches expected produces no row.
    """
    try:
        written = inventory.count(
            session, {line.product_id: line.counted_milli for line in body.lines}
        )
    except UnknownProduct as exc:
        raise _no_such_product(exc) from exc
    except ReceivingError as exc:
        raise _refused(exc) from exc

    return MovementsResponse(movement_ids=written)


@router.post("/adjustments", response_model=MovementsResponse)
def adjust(
    body: AdjustmentRequest,
    inventory: Inventory,
    session: Annotated[Session, Depends(require(permissions.STOCK_ADJUST))],
) -> MovementsResponse:
    """A manual correction — breakage, theft, a gift, a mistake undone.

    The only movement with no document behind it, which is why the reason is
    required rather than optional. An adjustment column full of blanks is
    where shrinkage goes to be forgotten about.
    """
    try:
        written = inventory.adjust(
            session, body.product_id, body.delta_milli, body.note
        )
    except UnknownProduct as exc:
        raise _no_such_product(exc) from exc
    except ReceivingError as exc:
        raise _refused(exc) from exc

    return MovementsResponse(movement_ids=written)

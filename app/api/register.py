"""The register — cart and tender endpoints (architecture §4).

Every response carries the **fully recalculated basket**. The UI renders what
it is given and never adds anything up, so there is exactly one place a total
can be wrong and one place to fix it.

Errors here are shaped for the person at the counter, not for a developer: an
unknown barcode and a mistyped one are different problems with different next
actions, and the cashier needs to be told which one they have.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentSession, get_cart_service, get_sale_service, require
from app.api.schemas import (
    AddLineRequest,
    CartLineOut,
    CartOut,
    ChangeQuantityRequest,
    MoneyOut,
    PostSaleResponse,
    ReceiptPdfResponse,
    TaxComponentOut,
    TenderQuote,
    TenderRequest,
    TenderResponse,
)
from app.domain import permissions
from app.domain.identity import Session
from app.domain.money import QUANTITY_SCALE, Money
from app.domain.payments import PaymentError, approved_total
from app.domain.receipt import ReceiptLine
from app.domain.tender import TenderMethod
from app.services.cart_service import (
    CartLocked,
    CartNotFound,
    CartService,
    OpenCart,
    UnknownBarcode,
    UnreadableBarcode,
)
from app.services.receipt_render import receipt_path, render_html, render_pdf, render_text
from app.services.sale_service import SaleNotSettled, SaleService

router = APIRouter(prefix="/register", tags=["register"])

CartSvc = Annotated[CartService, Depends(get_cart_service)]
SaleSvc = Annotated[SaleService, Depends(get_sale_service)]


# ── Serialisation ───────────────────────────────────────────────────────────


def _to_cart_out(open_cart: OpenCart) -> CartOut:
    cart = open_cart.cart
    balance = open_cart.balance()

    return CartOut(
        cart_id=open_cart.id,
        lines=[
            CartLineOut(
                line_no=line_no,
                product_id=line.line.product_id,
                description=line.description,
                qty_milli=line.line.qty_milli,
                quantity_text=ReceiptLine(
                    line_no=line_no,
                    description=line.description,
                    qty_milli=line.line.qty_milli,
                    uom="each",
                    unit_price=line.line.unit_price,
                    discount=line.discount_total,
                    line_total=line.line_total,
                ).quantity_text,
                unit_price=MoneyOut.of(line.line.unit_price),
                discount=MoneyOut.of(line.discount_total),
                tax_amount=MoneyOut.of(line.tax_amount),
                line_total=MoneyOut.of(line.line_total),
            )
            for line_no, line in cart.numbered()
        ],
        item_count=cart.item_count,
        subtotal=MoneyOut.of(cart.subtotal),
        discount_total=MoneyOut.of(cart.discount_total),
        tax_total=MoneyOut.of(cart.tax_total),
        total_before_rounding=MoneyOut.of(cart.total_before_rounding),
        tax_breakdown=[
            TaxComponentOut(
                tax_code=component.tax_code,
                rate_bp=component.rate_bp,
                taxable_base=MoneyOut.of(component.taxable_base),
                tax_amount=MoneyOut.of(component.tax_amount),
                cgst=MoneyOut.of(component.cgst),
                sgst=MoneyOut.of(component.sgst),
            )
            for component in cart.tax_breakdown
        ],
        paid=MoneyOut.of(balance.paid),
        outstanding=MoneyOut.of(balance.outstanding),
        rounding_adjustment=MoneyOut.of(open_cart.rounding_adjustment),
        settled=balance.is_settled,
        locked=approved_total(open_cart.attempts).is_positive,
    )


def _found(cart_id: str, service: CartService) -> OpenCart:
    try:
        return service.get(cart_id)
    except CartNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no open cart") from None


# ── Cart lifecycle ──────────────────────────────────────────────────────────


@router.post("/carts", response_model=CartOut, status_code=status.HTTP_201_CREATED)
def open_cart(
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
    carts: CartSvc,
) -> CartOut:
    return _to_cart_out(carts.open(session))


@router.get("/carts/{cart_id}", response_model=CartOut)
def read_cart(cart_id: str, carts: CartSvc, session: CurrentSession) -> CartOut:
    return _to_cart_out(_found(cart_id, carts))


@router.post("/carts/{cart_id}/lines", response_model=CartOut)
def add_line(
    cart_id: str,
    body: AddLineRequest,
    carts: CartSvc,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> CartOut:
    _found(cart_id, carts)

    try:
        if body.barcode:
            updated = carts.add_scanned(cart_id, body.barcode)
            if body.qty_milli is not None:
                updated = carts.change_quantity(
                    cart_id, updated.cart.line_numbers[-1], body.qty_milli
                )
        elif body.product_id:
            updated = carts.add_product(
                cart_id, body.product_id, body.qty_milli or QUANTITY_SCALE
            )
        else:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "give either a barcode or a product_id",
            )
    except UnreadableBarcode as exc:
        # A mistyped digit. The cashier should retype, not go hunting.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"That code could not be read: {exc.reason}. Check the digits.",
        ) from exc
    except UnknownBarcode as exc:
        # A valid code for a product this shop does not have. Different
        # problem, different action: search by name instead.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No product found for {exc.barcode}. Try searching by name.",
        ) from exc
    except CartLocked as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This sale has already taken payment; the basket cannot change.",
        ) from exc

    return _to_cart_out(updated)


@router.patch("/carts/{cart_id}/lines/{line_no}", response_model=CartOut)
def change_quantity(
    cart_id: str,
    line_no: int,
    body: ChangeQuantityRequest,
    carts: CartSvc,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> CartOut:
    _found(cart_id, carts)
    try:
        return _to_cart_out(carts.change_quantity(cart_id, line_no, body.qty_milli))
    except CartLocked:
        raise HTTPException(status.HTTP_409_CONFLICT, "basket is locked") from None
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete("/carts/{cart_id}/lines/{line_no}", response_model=CartOut)
def void_line(
    cart_id: str,
    line_no: int,
    carts: CartSvc,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> CartOut:
    """Void a line from an *open* basket.

    Deliberately gated on `sale.create`, not `sale.void`: removing a line
    before the sale exists is ordinary cashier work. `sale.void` governs
    reversing a sale that has already been posted and taken money, which is a
    different act with a different audit trail.
    """
    _found(cart_id, carts)
    try:
        return _to_cart_out(carts.void_line(cart_id, line_no))
    except CartLocked:
        raise HTTPException(status.HTTP_409_CONFLICT, "basket is locked") from None
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete("/carts/{cart_id}", status_code=status.HTTP_204_NO_CONTENT)
def abandon_cart(
    cart_id: str,
    carts: CartSvc,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> None:
    carts.discard(cart_id)


# ── Tender ──────────────────────────────────────────────────────────────────


@router.get("/carts/{cart_id}/tender-quote", response_model=TenderQuote)
def tender_quote(
    cart_id: str, method: str, carts: CartSvc, session: CurrentSession
) -> TenderQuote:
    """What this method would collect — shown before the money is taken.

    Cash rounds to the rupee; UPI does not. The cashier sees the difference
    before committing, which is the whole point of §13.4.
    """
    _found(cart_id, carts)
    quote = carts.quote_tender(cart_id, _method(method))
    return TenderQuote(
        method=method,
        outstanding=MoneyOut.of(quote.outstanding),
        collected=MoneyOut.of(quote.collected),
        rounding_adjustment=MoneyOut.of(quote.adjustment),
    )


@router.post("/carts/{cart_id}/payments", response_model=TenderResponse)
async def take_payment(
    cart_id: str,
    body: TenderRequest,
    carts: CartSvc,
    sales: SaleSvc,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> TenderResponse:
    _found(cart_id, carts)
    tendered = (
        Money(body.tendered_paise) if body.tendered_paise is not None else None
    )

    try:
        result = await sales.take_payment(cart_id, _method(body.method), tendered)
    except PaymentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return TenderResponse(
        attempt_id=result.attempt.id,
        state=result.attempt.state.value,
        cart=_to_cart_out(carts.get(cart_id)),
        change_due=MoneyOut.of(sales.change_for(cart_id)),
    )


@router.post("/carts/{cart_id}/post", response_model=PostSaleResponse)
def post_sale(
    cart_id: str,
    carts: CartSvc,
    sales: SaleSvc,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> PostSaleResponse:
    """Commit the sale, then render its receipt.

    Order matters: the sale is durable before anything is rendered. Printing
    is a side effect and is retryable; the sale is not (architecture §8).
    """
    open_cart = _found(cart_id, carts)

    try:
        posted = sales.post(cart_id)
    except SaleNotSettled as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    receipt = sales.receipt_for_cart(open_cart, posted, session.full_name)

    return PostSaleResponse(
        sale_id=posted.sale_id,
        receipt_no=posted.receipt_no,
        status=posted.status,
        grand_total=MoneyOut.of(posted.grand_total),
        rounding_adjustment=MoneyOut.of(posted.rounding_adjustment),
        change_due=MoneyOut.of(posted.change_due),
        receipt_html=render_html(receipt),
        receipt_text=render_text(receipt),
    )


@router.post("/sales/{sale_id}/receipt.pdf", response_model=ReceiptPdfResponse)
def write_receipt_pdf(
    sale_id: str,
    carts: CartSvc,
    sales: SaleSvc,
    session: CurrentSession,
) -> ReceiptPdfResponse:
    """Write the PDF for a posted sale. On demand, never automatically."""
    receipt = sales.receipt_for_sale(sale_id, session.full_name)
    if receipt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such sale")

    destination = receipt_path(sales.settings.data_dir, receipt)
    render_pdf(receipt, destination)
    return ReceiptPdfResponse(
        sale_id=sale_id, receipt_no=receipt.receipt_no, path=str(destination)
    )


def _method(value: str) -> TenderMethod:
    if value not in ("cash", "upi"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown tender method {value!r}"
        )
    return value  # type: ignore[return-value]

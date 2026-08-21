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

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import CurrentSession, get_cart_service, get_sale_service, require
from app.api.schemas import (
    AddLineRequest,
    AttemptOut,
    AttestRequest,
    CartLineOut,
    CartOut,
    ChangeQuantityRequest,
    MoneyOut,
    PostSaleResponse,
    ReceiptPdfResponse,
    ResolveReviewRequest,
    ResolveReviewResponse,
    ReviewItemOut,
    ReviewQueueResponse,
    TaxComponentOut,
    TenderQuote,
    TenderRequest,
    TenderResponse,
    UnknownPaymentRequest,
)
from app.domain import permissions
from app.domain.identity import Session, utcnow
from app.domain.money import QUANTITY_SCALE, Money
from app.domain.payments import PaymentAttempt, PaymentError, approved_total, pending
from app.domain.receipt import ReceiptLine
from app.domain.tender import TenderMethod
from app.services.cart_service import (
    AttemptNotFound,
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
        locked=(
            approved_total(open_cart.attempts).is_positive
            or bool(pending(open_cart.attempts))
        ),
    )


def _found(cart_id: str, service: CartService) -> OpenCart:
    try:
        return service.get(cart_id)
    except CartNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no open cart") from None


def _to_attempt_out(attempt: PaymentAttempt) -> AttemptOut:
    return AttemptOut(
        attempt_id=attempt.id,
        method=attempt.method,
        state=attempt.state.value,
        amount=MoneyOut.of(attempt.amount),
        reference=attempt.txn_ref,
        expires_at=attempt.expires_at.isoformat() if attempt.expires_at else None,
        is_pending=not attempt.state.is_terminal,
    )


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

    return _tender_response(result.attempt, cart_id, carts, sales)


# ── Resolving an attempt the terminal cannot resolve itself ─────────────────
#
# Cash resolves in `begin`. UPI cannot: the QR is printed on the counter, so
# the only signal that money arrived is a human hearing a soundbox
# (architecture §13.3). These three endpoints are that human's answers —
# yes, no, and the one that matters most, "I can't tell".


def _tender_response(
    attempt: PaymentAttempt, cart_id: str, carts: CartService, sales: SaleService
) -> TenderResponse:
    return TenderResponse(
        attempt_id=attempt.id,
        state=attempt.state.value,
        cart=_to_cart_out(carts.get(cart_id)),
        change_due=MoneyOut.of(sales.change_for(cart_id)),
        expires_at=attempt.expires_at.isoformat() if attempt.expires_at else None,
    )


def _attempt_or_404(attempt_id: str, carts: CartService) -> OpenCart:
    try:
        return carts.cart_for_attempt(attempt_id)
    except AttemptNotFound:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no such payment attempt"
        ) from None


@router.get("/payments/{attempt_id}", response_model=AttemptOut)
def payment_attempt(
    attempt_id: str, carts: CartSvc, session: CurrentSession
) -> AttemptOut:
    """Where an attempt stands.

    There is nothing to poll a provider *about* — no PSP, no webhook, no
    device — so this reports the terminal's own view, which includes an
    attempt that has quietly expired since the screen last looked.
    """
    open_cart = _attempt_or_404(attempt_id, carts)
    found = next(a for a in open_cart.attempts if a.id == attempt_id)
    return _to_attempt_out(found)


@router.post("/payments/{attempt_id}/confirm", response_model=TenderResponse)
def confirm_payment(
    attempt_id: str,
    body: AttestRequest,
    carts: CartSvc,
    sales: SaleSvc,
    session: Annotated[Session, Depends(require(permissions.PAYMENT_ATTEST))],
) -> TenderResponse:
    """The cashier confirms the money arrived, and says how much.

    `amount_paise` is what actually landed. It defaults to what was asked for,
    which is the common case — but it is a default, not an assumption: on a
    printed counter QR the customer types the figure themselves.
    """
    open_cart = _attempt_or_404(attempt_id, carts)
    amount = Money(body.amount_paise) if body.amount_paise is not None else None

    try:
        result = sales.attest(attempt_id, amount=amount, reference=body.reference)
    except PaymentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return _tender_response(result.attempt, open_cart.id, carts, sales)


@router.post("/payments/{attempt_id}/unknown", response_model=TenderResponse)
def unknown_payment(
    attempt_id: str,
    body: UnknownPaymentRequest,
    carts: CartSvc,
    sales: SaleSvc,
    session: Annotated[Session, Depends(require(permissions.PAYMENT_ATTEST))],
) -> TenderResponse:
    """"The customer says they paid and I can't tell."

    The sale still posts — refusing to serve someone who may well have paid is
    not an option with a queue behind them — but it posts as `requires_review`
    for a supervisor, and the money is never counted as received (§13.5).
    """
    open_cart = _attempt_or_404(attempt_id, carts)
    try:
        result = sales.mark_unknown(attempt_id, body.reason)
    except PaymentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return _tender_response(result.attempt, open_cart.id, carts, sales)


@router.post("/payments/{attempt_id}/cancel", response_model=TenderResponse)
async def cancel_payment(
    attempt_id: str,
    carts: CartSvc,
    sales: SaleSvc,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> TenderResponse:
    """Abandon an attempt. The basket unfreezes and the cart stays open."""
    open_cart = _attempt_or_404(attempt_id, carts)
    try:
        result = await sales.cancel_attempt(attempt_id)
    except PaymentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return _tender_response(result.attempt, open_cart.id, carts, sales)


@router.post("/carts/{cart_id}/post", response_model=PostSaleResponse)
def post_sale(
    cart_id: str,
    request: Request,
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

    # The sale is committed and queued. Wake the sync loop rather than making
    # it wait out its idle interval — and do it *after* the commit, so a
    # failure to nudge can never be a failure to sell (architecture §9).
    engine = getattr(request.app.state, "sync", None)
    if engine is not None:
        engine.nudge()

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


# ── The review queue — architecture §13.5 ───────────────────────────────────


@router.get("/reviews", response_model=ReviewQueueResponse)
def review_queue(
    sales: SaleSvc,
    session: Annotated[Session, Depends(require(permissions.SALE_REVIEW_RESOLVE))],
) -> ReviewQueueResponse:
    """Sales nobody could confirm, still waiting on a supervisor.

    Gated on the resolving permission rather than on reading sales, because
    the queue is a worklist for the person who can act on it. A cashier
    watching their own disputed payment sit there helps nobody.
    """
    return ReviewQueueResponse(
        items=[
            ReviewItemOut(
                sale_id=row["id"],
                receipt_no=row["receipt_no"],
                grand_total=MoneyOut.of(Money(int(row["grand_total"]))),
                disputed_amount=MoneyOut.of(Money(int(row["disputed_amount"] or 0))),
                posted_at=row["client_created_at"],
            )
            for row in sales.sales.open_reviews()
        ]
    )


@router.post("/sales/{sale_id}/resolve-review", response_model=ResolveReviewResponse)
def resolve_review(
    sale_id: str,
    body: ResolveReviewRequest,
    sales: SaleSvc,
    session: Annotated[Session, Depends(require(permissions.SALE_REVIEW_RESOLVE))],
) -> ResolveReviewResponse:
    """Settle a disputed payment — a supervisor's call, never the cashier's.

    The sale's own status is not rewritten. `requires_review` is what happened
    and it stays true; this records what was decided about it, as its own row
    (migration 003). Voiding a possibly-real payment is explicitly not what
    this does (architecture §13.5).
    """
    if body.outcome not in ("paid", "not_paid"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "outcome must be 'paid' or 'not_paid'",
        )

    sale = sales.sales.get(sale_id)
    if sale is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such sale")
    if sale["status"] != "requires_review":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "that sale is not awaiting review"
        )
    if sales.sales.review_for(sale_id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "that sale has already been reviewed"
        )

    resolved_at = utcnow()
    sales.sales.resolve_review(
        sale_id=sale_id,
        outcome=body.outcome,
        resolved_by=session.user_id,
        resolved_at=resolved_at,
        note=body.note,
    )
    return ResolveReviewResponse(
        sale_id=sale_id, outcome=body.outcome, resolved_at=resolved_at.isoformat()
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

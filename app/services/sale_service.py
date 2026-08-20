"""Taking payment and posting the sale — architecture §8, §13.2.

The balance loop lives here:

    balance = grand_total - approved payments
    while balance > 0:  choose method -> begin attempt -> resolve
    post when balance <= 0;  change due = -balance   (cash only)

Rounding is applied when the *first* tender method is chosen, to the balance
outstanding at that moment, and never to the basket (architecture §13.4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.data.repositories.sales import PostedSale, SalesRepository
from app.data.repositories.terminal import TerminalRepository
from app.domain.cart import Cart
from app.domain.identity import utcnow
from app.domain.money import Money
from app.domain.payments import (
    DEFAULT_ATTEMPT_TTL,
    AttemptState,
    BalanceState,
    PaymentAttempt,
    PaymentError,
)
from app.domain.receipt import Receipt, ReceiptError, ReceiptLine, ReceiptPayment
from app.domain.tax import TaxCode, breakdown
from app.domain.tender import TenderMethod, change_due
from app.services.cart_service import CartService, OpenCart
from app.services.payment_providers import ProviderRegistry, new_attempt

log = logging.getLogger(__name__)


class SaleNotSettled(RuntimeError):
    """The balance is still outstanding; the sale cannot post yet."""


@dataclass(frozen=True, slots=True)
class TenderResult:
    attempt: PaymentAttempt
    balance: BalanceState
    settled: bool


class SaleService:
    def __init__(
        self,
        carts: CartService,
        sales: SalesRepository,
        terminal: TerminalRepository,
        providers: ProviderRegistry,
        settings: Settings,
    ) -> None:
        self.carts = carts
        self.sales = sales
        self.terminal = terminal
        self.providers = providers
        self.settings = settings

    # ── The balance loop ────────────────────────────────────────────────────

    async def take_payment(
        self,
        cart_id: str,
        method: TenderMethod,
        tendered: Money | None = None,
    ) -> TenderResult:
        """Begin and resolve one attempt against the outstanding balance."""
        open_cart = self.carts.get(cart_id)
        if open_cart.cart.is_empty:
            raise PaymentError("cannot take payment for an empty basket")

        # Rounding is settled once, when the first tender method is chosen,
        # against the balance outstanding at that moment (architecture §13.4).
        if open_cart.rounding is None:
            open_cart.rounding = self.carts.quote_tender(cart_id, method)

        outstanding = open_cart.balance().outstanding
        if not outstanding.is_positive:
            raise PaymentError("nothing left to pay")

        attempt = new_attempt(
            method=method,
            amount=outstanding,
            expires_at=None if method == "cash" else utcnow() + DEFAULT_ATTEMPT_TTL,
            tendered=tendered,
        )

        provider = self.providers.for_method(method)
        resolved = await provider.begin(attempt)

        open_cart.attempts.append(resolved)
        balance = open_cart.balance()
        return TenderResult(
            attempt=resolved, balance=balance, settled=balance.is_settled
        )

    def change_for(self, cart_id: str) -> Money:
        """Change owed, from the cash actually handed over.

        Only cash can overpay: a UPI attempt is always for the exact balance
        because the amount is embedded in the QR (architecture §13.2).
        """
        open_cart = self.carts.get(cart_id)
        cash_tendered = Money.zero()
        cash_owed = Money.zero()
        for attempt in open_cart.attempts:
            if attempt.method != "cash" or attempt.state is not AttemptState.APPROVED:
                continue
            cash_owed = cash_owed + attempt.amount
            cash_tendered = cash_tendered + (attempt.tendered or attempt.amount)

        if cash_tendered <= cash_owed:
            return Money.zero()
        return change_due(cash_tendered, cash_owed)

    # ── Posting ─────────────────────────────────────────────────────────────

    def post(self, cart_id: str) -> PostedSale:
        """Commit the sale. One transaction, or nothing at all."""
        open_cart = self.carts.get(cart_id)
        balance = open_cart.balance()

        if not balance.is_settled:
            raise SaleNotSettled(
                f"{balance.outstanding} still outstanding on cart {cart_id}"
            )

        receipt_no = self.terminal.next_receipt_no(
            self.settings.store_code, self.settings.terminal_code
        )

        posted = self.sales.post_sale(
            cart=open_cart.cart,
            cart_id=open_cart.id,
            store_id=open_cart.store_id,
            terminal_id=self.settings.terminal_code,
            cashier_id=open_cart.cashier_id,
            receipt_no=receipt_no,
            attempts=open_cart.attempts,
            rounding=open_cart.rounding,
            change_due=self.change_for(cart_id),
            client_created_at=open_cart.opened_at,
            posted_at=utcnow(),
        )

        # The cart is only forgotten after the transaction has committed. A
        # crash before this point leaves an in-memory cart and no sale, which
        # is recoverable; the reverse would not be.
        self.carts.discard(cart_id)
        log.info(
            "posted %s: %s, %d lines", posted.receipt_no, posted.grand_total,
            open_cart.cart.item_count,
        )
        return posted

    # ── Receipts ────────────────────────────────────────────────────────────

    def receipt_for_cart(
        self, open_cart: OpenCart, posted: PostedSale, cashier_name: str
    ) -> Receipt:
        return _build_receipt(
            cart=open_cart.cart,
            posted=posted,
            attempts=open_cart.attempts,
            settings=self.settings,
            cashier_name=cashier_name,
            is_reprint=False,
        )


    def receipt_for_sale(self, sale_id: str, fallback_name: str) -> Receipt | None:
        """Rebuild a receipt from what was stored — reprint by sale id.

        Every figure comes from the sale rows, including the GST rate that was
        in force at the time (snapshotted on the line by migration 002). A
        reprint therefore reproduces the invoice the customer was given, not
        the one today's catalogue would produce.
        """
        sale = self.sales.get(sale_id)
        if sale is None:
            return None

        rows = self.sales.lines_for(sale_id)
        lines = tuple(
            ReceiptLine(
                line_no=int(row["line_no"]),
                description=row["description"],
                qty_milli=int(row["qty_milli"]),
                uom="each",
                unit_price=Money(int(row["unit_price"])),
                discount=Money(int(row["discount_amount"])),
                line_total=Money(int(row["line_total"])),
            )
            for row in rows
        )

        components = breakdown(
            [
                (
                    TaxCode(
                        code=row["tax_code"] or "GST0",
                        name=row["tax_code"] or "GST0",
                        rate_bp=int(row["tax_rate_bp"] or 0),
                    ),
                    Money(int(row["line_total"])),
                )
                for row in rows
            ]
        )

        payments = tuple(
            ReceiptPayment(
                method=row["method"],
                amount=Money(int(row["amount"])),
                verified=bool(row["verified"]),
                reference=row["tender_ref"],
            )
            for row in self.sales.payments_for(sale_id)
        )

        receipt = Receipt(
            receipt_no=sale["receipt_no"],
            sale_id=sale_id,
            issued_at=datetime.fromisoformat(sale["client_created_at"]),
            store_name=self.settings.store_name,
            store_gstin=self.settings.store_gstin or None,
            terminal_code=sale["terminal_id"],
            cashier_name=self._cashier_name(sale["cashier_id"], fallback_name),
            lines=lines,
            tax_components=tuple(components),
            subtotal=Money(int(sale["subtotal"])),
            discount_total=Money(int(sale["discount_total"])),
            total_before_rounding=Money(int(sale["grand_total"]))
            - Money(int(sale["rounding_adjustment"])),
            rounding_adjustment=Money(int(sale["rounding_adjustment"])),
            grand_total=Money(int(sale["grand_total"])),
            payments=payments,
            is_reprint=True,
            requires_review=sale["status"] == "requires_review",
        )

        if not receipt.reconciles():
            raise ReceiptError(
                f"stored sale {sale_id} does not reconcile; refusing to reprint"
            )
        return receipt

    def _cashier_name(self, cashier_id: str, fallback: str) -> str:
        """A reprint shows the cashier who *made* the sale, not who reprinted."""
        row = self.sales.db.query_one(
            "SELECT full_name FROM cached_users WHERE user_id = ?", (cashier_id,)
        )
        return row[0] if row else fallback


def _build_receipt(
    *,
    cart: Cart,
    posted: PostedSale,
    attempts: list[PaymentAttempt],
    settings: Settings,
    cashier_name: str,
    is_reprint: bool,
) -> Receipt:
    lines = tuple(
        ReceiptLine(
            line_no=line_no,
            description=line.description,
            qty_milli=line.line.qty_milli,
            uom="each",
            unit_price=line.line.unit_price,
            discount=line.discount_total,
            line_total=line.line_total,
        )
        for line_no, line in cart.numbered()
    )

    payments = tuple(
        ReceiptPayment(
            method=attempt.method,
            amount=attempt.amount,
            verified=attempt.method == "cash",
            reference=attempt.txn_ref,
        )
        for attempt in attempts
        if attempt.state is AttemptState.APPROVED
    )

    receipt = Receipt(
        receipt_no=posted.receipt_no,
        sale_id=posted.sale_id,
        issued_at=posted.posted_at,
        store_name=settings.store_name,
        store_gstin=settings.store_gstin or None,
        terminal_code=settings.terminal_code,
        cashier_name=cashier_name,
        lines=lines,
        tax_components=tuple(cart.tax_breakdown),
        subtotal=cart.subtotal,
        discount_total=cart.discount_total,
        total_before_rounding=cart.total_before_rounding,
        rounding_adjustment=posted.rounding_adjustment,
        grand_total=posted.grand_total,
        payments=payments,
        change_due=posted.change_due,
        is_reprint=is_reprint,
        requires_review=posted.status == "requires_review",
    )

    # A receipt that does not add up is never handed to a customer.
    if not receipt.reconciles():
        raise ReceiptError(
            f"receipt {receipt.receipt_no} does not reconcile: "
            f"subtotal {receipt.subtotal}, discounts {receipt.discount_total}, "
            f"rounding {receipt.rounding_adjustment}, total {receipt.grand_total}"
        )
    return receipt

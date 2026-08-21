"""Server-side cart state — architecture §4.

**The cart lives here, not in React.** Adding a line returns fully recalculated
totals from Python, so the UI never sums anything and a webview crash mid-sale
loses nothing. That single decision is what keeps price and tax bugs to one
place.

Carts are held in memory until they post. They are not written to SQLite
line-by-line: a sale becomes durable in exactly one transaction at
`post_sale`, and a cart that never got there was never a sale. Parking a
transaction (architecture §4) will serialise this same value; it is not needed
for phase 3's exit criteria.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from app.data.repositories.catalog import CatalogProduct, CatalogRepository
from app.domain.barcode import ScanResult, parse
from app.domain.cart import Cart
from app.domain.identity import Session, utcnow
from app.domain.ids import new_id
from app.domain.money import QUANTITY_SCALE, Money
from app.domain.payments import (
    BalanceState,
    PaymentAttempt,
    approved_total,
    balance_of,
    expire_stale,
    pending,
)
from app.domain.pricing import Discount, LineInput
from app.domain.tender import CashRounding, TenderMethod, rounding_for


class CartNotFound(KeyError):
    """No open cart with that id."""


class UnknownBarcode(LookupError):
    """The code parsed but resolved to no product."""

    def __init__(self, barcode: str, reason: str | None = None) -> None:
        super().__init__(barcode)
        self.barcode = barcode
        self.reason = reason


class UnreadableBarcode(ValueError):
    """The code did not parse at all — a mistyped digit, usually."""

    def __init__(self, barcode: str, reason: str) -> None:
        super().__init__(reason)
        self.barcode = barcode
        self.reason = reason


@dataclass
class OpenCart:
    """A cart in progress, with everything the register needs about it."""

    id: str
    cart: Cart
    cashier_id: str
    store_id: str
    opened_at: datetime
    attempts: list[PaymentAttempt] = field(default_factory=list)
    #: Settled when the first tender is chosen, because rounding is a property
    #: of the tender rather than of the basket (architecture §13.4).
    rounding: CashRounding | None = None

    @property
    def rounding_adjustment(self) -> Money:
        return self.rounding.adjustment if self.rounding else Money.zero()

    def balance(self) -> BalanceState:
        return balance_of(
            self.cart.total_before_rounding, self.attempts, self.rounding_adjustment
        )


class CartService:
    def __init__(self, catalog: CatalogRepository) -> None:
        self.catalog = catalog
        self._carts: dict[str, OpenCart] = {}
        self._lock = threading.Lock()

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def open(self, session: Session) -> OpenCart:
        cart = OpenCart(
            id=new_id(),
            cart=Cart(),
            cashier_id=session.user_id,
            store_id=session.store_id,
            opened_at=utcnow(),
        )
        with self._lock:
            self._carts[cart.id] = cart
        return cart

    def get(self, cart_id: str) -> OpenCart:
        with self._lock:
            found = self._carts.get(cart_id)
        if found is None:
            raise CartNotFound(cart_id)
        return self._expire_attempts(found)

    def discard(self, cart_id: str) -> None:
        with self._lock:
            self._carts.pop(cart_id, None)

    def cart_for_attempt(self, attempt_id: str) -> OpenCart:
        """Find the cart an attempt belongs to.

        The attempt endpoints are addressed by attempt id alone (architecture
        §4) because that is the id the cashier's screen is holding. A till has
        one open cart in practice, so this scan is over a single entry.
        """
        with self._lock:
            carts = list(self._carts.values())
        for open_cart in carts:
            if any(attempt.id == attempt_id for attempt in open_cart.attempts):
                return self._expire_attempts(open_cart)
        raise AttemptNotFound(attempt_id)

    def replace_attempt(self, open_cart: OpenCart, attempt: PaymentAttempt) -> None:
        """Swap in a resolved attempt, in place of the one it came from."""
        open_cart.attempts = [
            attempt if existing.id == attempt.id else existing
            for existing in open_cart.attempts
        ]

    def _expire_attempts(self, open_cart: OpenCart) -> OpenCart:
        open_cart.attempts = expire_stale(open_cart.attempts, now=utcnow())
        return open_cart

    # ── Building the basket ─────────────────────────────────────────────────

    def resolve(self, raw: str) -> tuple[CatalogProduct, ScanResult]:
        """Turn a typed or scanned code into something sellable.

        Parsing is pure and lives in the domain; resolution needs the
        catalogue and lives here. The split is what lets the barcode rules be
        unit-tested against 308 real codes with no database attached.
        """
        scan = parse(raw)
        if not scan.is_usable or scan.lookup_key is None:
            raise UnreadableBarcode(raw, scan.error or "unreadable code")

        product = self.catalog.by_lookup_key(scan.lookup_key)
        if product is None:
            raise UnknownBarcode(raw)
        return product, scan

    def add_scanned(self, cart_id: str, raw: str) -> OpenCart:
        """Add a line from a code, honouring pack size and embedded weight."""
        open_cart = self.get(cart_id)
        product, scan = self.resolve(raw)

        if scan.carries_quantity and scan.qty_milli is not None:
            # A weighed code carries its own quantity, so the cashier is not
            # asked for one they already keyed into the scale prompt.
            qty_milli = scan.qty_milli
        else:
            # Scanning a case of 24 receives 24 units (architecture §10.2).
            qty_milli = product.pack_size * QUANTITY_SCALE

        return self._append(open_cart, product, qty_milli, barcode=raw)

    def add_product(
        self, cart_id: str, product_id: str, qty_milli: int = QUANTITY_SCALE
    ) -> OpenCart:
        """Add a line chosen from search — the only route for the 38.6% of the
        catalogue that has no barcode at all."""
        open_cart = self.get(cart_id)
        product = self.catalog.by_id(product_id)
        if product is None:
            raise UnknownBarcode(product_id, "no such product")
        return self._append(open_cart, product, qty_milli, barcode=None)

    def _append(
        self,
        open_cart: OpenCart,
        product: CatalogProduct,
        qty_milli: int,
        barcode: str | None,
    ) -> OpenCart:
        self._require_open_basket(open_cart)
        line = LineInput(
            product_id=product.product_id,
            description=product.display_name,
            unit_price=product.unit_price,
            qty_milli=qty_milli,
            tax_code=product.tax_code,
            barcode_scanned=barcode,
        )
        open_cart.cart = open_cart.cart.add(line)
        return open_cart

    def change_quantity(self, cart_id: str, line_no: int, qty_milli: int) -> OpenCart:
        open_cart = self.get(cart_id)
        self._require_open_basket(open_cart)
        open_cart.cart = open_cart.cart.change_quantity(line_no, qty_milli)
        return open_cart

    def void_line(self, cart_id: str, line_no: int) -> OpenCart:
        open_cart = self.get(cart_id)
        self._require_open_basket(open_cart)
        open_cart.cart = open_cart.cart.void_line(line_no)
        return open_cart

    def apply_discount(
        self, cart_id: str, line_no: int, discount: Discount
    ) -> OpenCart:
        open_cart = self.get(cart_id)
        self._require_open_basket(open_cart)
        open_cart.cart = open_cart.cart.apply_discount(line_no, discount)
        return open_cart

    def _require_open_basket(self, open_cart: OpenCart) -> None:
        """The basket freezes once money has been taken.

        Changing a line after an approved payment would move the total under a
        payment that has already been collected, which is how a till ends a
        day short.
        """
        if approved_total(open_cart.attempts).is_positive:
            raise CartLocked(open_cart.id)
        # A pending UPI attempt froze the basket the moment the cashier read a
        # figure out loud: the customer is typing that amount into their own
        # app right now (architecture §13.3). Adding a line underneath it would
        # collect the wrong money. Cancelling the attempt is the way out.
        if pending(open_cart.attempts):
            raise CartLocked(
                open_cart.id, reason="a payment is in progress; cancel it first"
            )

    # ── Tender ──────────────────────────────────────────────────────────────

    def quote_tender(self, cart_id: str, method: TenderMethod) -> CashRounding:
        """What this method would collect for the outstanding balance.

        Does not commit anything: the register shows the cashier the rounded
        figure before they take the money.
        """
        open_cart = self.get(cart_id)
        outstanding = open_cart.cart.balance(approved_total(open_cart.attempts))
        return rounding_for(outstanding, method)


class CartLocked(RuntimeError):
    """The basket cannot change while money is being collected against it."""

    def __init__(self, cart_id: str, reason: str | None = None) -> None:
        super().__init__(reason or "the basket is locked; a payment was taken")
        self.cart_id = cart_id
        self.reason = reason


class AttemptNotFound(KeyError):
    """No open cart holds a payment attempt with that id."""

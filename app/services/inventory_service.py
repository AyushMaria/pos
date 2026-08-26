"""Receiving, counting and adjusting — architecture §9.4.

Orchestration only. The arithmetic is in `app/domain/receiving.py` and the
writes are in `InventoryRepository`; what lives here is resolving a scanned
code to a product and its pack size, and deciding what a document means.

**Nothing here waits on the cloud.** A delivery arrives when it arrives, and
the shop that cannot receive it with the line down is the shop offline-first
was for. Movements go to the ledger and the outbox, and reach Supabase when
they reach it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.data.repositories.catalog import CatalogProduct, CatalogRepository
from app.data.repositories.inventory import InventoryRepository
from app.domain import receiving
from app.domain.barcode import parse
from app.domain.identity import Session, utcnow
from app.domain.receiving import Movement, ReceivingError
from app.services.cart_service import UnknownBarcode, UnreadableBarcode


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    """One line of a delivery, as the person entering it sees it."""

    product: CatalogProduct
    packs: int
    movement: Movement

    @property
    def units(self) -> int:
        """Whole units received, for the screen. The ledger keeps thousandths."""
        return self.movement.delta_milli // receiving.QUANTITY_SCALE


class InventoryService:
    def __init__(
        self,
        catalog: CatalogRepository,
        inventory: InventoryRepository,
        *,
        terminal_code: str,
    ) -> None:
        self.catalog = catalog
        self.inventory = inventory
        self.terminal_code = terminal_code

    # ── Receiving ───────────────────────────────────────────────────────────

    def preview_receipt_line(self, raw: str, packs: int) -> ReceiptLine:
        """What scanning this code that many times would receive.

        Separate from committing so the screen can show a running list and the
        person can correct it before anything is written. A delivery entered
        wrong and committed is a correction with a reason attached; caught
        before commit it is a keystroke.
        """
        product = self._resolve(raw)
        movement = receiving.receipt(
            product.product_id, packs=packs, pack_size=product.pack_size
        )
        return ReceiptLine(product=product, packs=packs, movement=movement)

    def receive(self, session: Session, lines: list[ReceiptLine]) -> list[str]:
        """Commit a delivery. One transaction, all lines or none."""
        if not lines:
            raise ReceivingError("a receipt with no lines is not a receipt")
        return self.inventory.record(
            [line.movement for line in lines],
            store_id=session.store_id,
            terminal_id=self.terminal_code,
            user_id=session.user_id,
            ref_type="receipt",
            occurred_at=utcnow(),
        )

    # ── Counting and adjusting ──────────────────────────────────────────────

    def count(self, session: Session, counts: dict[str, int]) -> list[str]:
        """A stock count, as `{product_id: counted_milli}`.

        `expected` is read here and the correction written in the same
        transaction below, so a sale landing between the two cannot be
        silently reversed. A product whose count matches expected produces no
        row: nothing changed, and a ledger of no-ops is a ledger nobody reads.
        """
        movements: list[Movement] = []
        for product_id, counted_milli in counts.items():
            try:
                movements.append(
                    receiving.count(
                        product_id,
                        counted_milli=counted_milli,
                        expected_milli=self.inventory.on_hand(product_id),
                    )
                )
            except ReceivingError as exc:
                # "matches expected" is the ordinary case in a count, not an
                # error worth failing the whole section over.
                if "nothing to correct" not in str(exc):
                    raise
        return self.inventory.record(
            movements,
            store_id=session.store_id,
            terminal_id=self.terminal_code,
            user_id=session.user_id,
            ref_type="count",
            occurred_at=utcnow(),
        )

    def adjust(
        self, session: Session, product_id: str, delta_milli: int, note: str
    ) -> list[str]:
        """A manual correction, which always needs a reason."""
        movement = receiving.adjustment(
            product_id, delta_milli=delta_milli, note=note
        )
        return self.inventory.record(
            [movement],
            store_id=session.store_id,
            terminal_id=self.terminal_code,
            user_id=session.user_id,
            ref_type="adjustment",
            occurred_at=utcnow(),
        )

    # ── Resolution ──────────────────────────────────────────────────────────

    def _resolve(self, raw: str) -> CatalogProduct:
        """A code to a product, honouring pack size.

        Same rules as the register, deliberately: parsing is the domain's and
        is shared, so a case code means the same thing at the back door as it
        does at the counter. A weighed code is refused — a delivery is counted
        in packs, and a `22…` code carries one weighing of one item, which is
        not a quantity anybody received.
        """
        scan = parse(raw)
        if not scan.is_usable or scan.lookup_key is None:
            raise UnreadableBarcode(raw, scan.error or "unreadable code")
        if scan.carries_quantity:
            raise UnreadableBarcode(
                raw, "a weighed code is one weighing, not a delivery quantity"
            )

        product = self.catalog.by_lookup_key(scan.lookup_key)
        if product is None:
            raise UnknownBarcode(raw)
        return product

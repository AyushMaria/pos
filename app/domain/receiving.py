"""Stock movements — architecture §9.4.

Three things move stock, and they differ only in how the delta is arrived at:

    sale      delta = -qty
    receipt   delta = +qty x pack_size
    count     delta = counted - expected

Everything else about them is the same, which is why they share a ledger
rather than three tables. `stock_levels` is derived from that ledger by a
trigger and is never written directly — a level written by hand is a level
that disagrees with its own history within a day.

**Pure, like the rest of `domain/`.** No catalogue, no database, no clock.
Given a pack size and a quantity, what is the delta? That is the whole
question, and it is worth isolating because getting it wrong is not visible
until a shelf is empty and the system says it is not.

**The multiplication is the part that goes wrong.** Quantities are integer
thousandths throughout (§6), and a receipt has two multiplications stacked:
packs to units, then units to thousandths. One case of 24 is

    1 pack x 24 per pack = 24 units = 24000 thousandths

Get the order wrong and it is 1000 (the pack size ignored) or 24 (the scale
ignored) — both plausible-looking numbers that are wrong by a factor of
twenty-four or a thousand, and neither visible until a shelf disagrees with
the screen. `units_to_milli` exists so the second multiplication happens in
one place with a name on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: One whole unit, in thousandths. 1.250 kg is 1250; one tin is 1000.
QUANTITY_SCALE = 1000

MovementReason = Literal["receipt", "count", "adjustment"]


class ReceivingError(ValueError):
    """A movement that should not be written."""


@dataclass(frozen=True, slots=True)
class Movement:
    """One ledger row, before it has an id or a timestamp."""

    product_id: str
    delta_milli: int
    reason: MovementReason
    #: Free text for an adjustment, which is the only movement with no
    #: document behind it. Empty for a receipt or a count.
    note: str = ""

    def __post_init__(self) -> None:
        if type(self.delta_milli) is not int:
            raise ReceivingError(
                f"delta must be integer thousandths, got {self.delta_milli!r}"
            )
        if self.delta_milli == 0:
            # A count that matches expected is not a correction, and a receipt
            # of nothing is a typo. Either would leave a ledger row saying
            # something happened when nothing did.
            raise ReceivingError("a movement of zero is not a movement")


def units_to_milli(units: int) -> int:
    """Whole units to thousandths. The one place that multiplication lives."""
    if type(units) is not int:
        raise ReceivingError(f"units must be a whole number, got {units!r}")
    return units * QUANTITY_SCALE


def receipt(product_id: str, *, packs: int, pack_size: int) -> Movement:
    """A delivery arriving.

    `packs` is what the person counted off the pallet — the number of things
    they scanned. `pack_size` comes from the barcode row: 1 for a retail unit,
    24 for a case of 24 (§10.2). Scanning one case receives 24 units.

    Both are whole numbers because you cannot receive a third of a case; the
    thousandths only appear on the way into the ledger.
    """
    if packs < 1:
        raise ReceivingError(f"cannot receive {packs} packs")
    if pack_size < 1:
        raise ReceivingError(f"pack size must be at least 1, got {pack_size}")
    return Movement(
        product_id=product_id,
        delta_milli=units_to_milli(packs * pack_size),
        reason="receipt",
    )


def count(product_id: str, *, counted_milli: int, expected_milli: int) -> Movement:
    """A stock count: the correction, not the count itself.

    The ledger records what *changed*, so a count writes the difference. That
    is what keeps it reconcilable: sum the deltas and you get the level, with
    no row that overrides history rather than adding to it.

    `expected_milli` must be read in the same transaction that writes this, or
    a sale between the reading and the writing is silently reversed — the
    count would put back stock that had just been sold.
    """
    if counted_milli < 0:
        raise ReceivingError(f"cannot count {counted_milli} of something")

    delta = counted_milli - expected_milli
    if delta == 0:
        raise ReceivingError(
            "the count matches what was expected; there is nothing to correct"
        )
    return Movement(product_id=product_id, delta_milli=delta, reason="count")


def adjustment(product_id: str, *, delta_milli: int, note: str) -> Movement:
    """A manual correction: breakage, theft, a gift, a mistake being undone.

    The only movement with no document behind it, which is exactly why it
    requires a reason. An adjustment column full of blanks is where shrinkage
    goes to be forgotten about.
    """
    if not note.strip():
        raise ReceivingError("an adjustment needs a reason")
    return Movement(
        product_id=product_id,
        delta_milli=delta_milli,
        reason="adjustment",
        note=note.strip(),
    )

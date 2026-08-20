"""Money — architecture §6.

Integer minor units (paise), always. No binary floating point ever touches a
money path; `scripts/check_no_float.py` enforces that mechanically across this
package, and this type exists so there is never a reason to reach for one.

Three rules the rest of the domain relies on:

  * Arithmetic on `Money` is exact. Addition, subtraction and multiplication
    by a whole number cannot lose a paisa, so they are always available.
  * Anything that *can* lose a paisa — scaling by a quantity, extracting tax,
    splitting a discount — is a named function that rounds explicitly, once,
    with ROUND_HALF_UP. Rounding is never implicit.
  * Every rounding operation that distributes a total across parts reconciles:
    the parts sum to the whole, exactly, with no residue.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

#: Quantities are integer thousandths (architecture §6), so 1.250 kg is 1250.
#: For anything sold by the kilo, thousandths are grams.
QUANTITY_SCALE = 1000

#: One rupee, in paise. Cash rounding works to this unit (§13.4).
RUPEE = 100


class MoneyError(ValueError):
    """A money value was constructed or combined incorrectly."""


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An exact amount in paise.

    Negative values are legal and meaningful: a refund line, a discount, a
    change-due figure, a rounding adjustment that went down rather than up.
    """

    paise: int

    def __post_init__(self) -> None:
        # bool is a subclass of int, and `Money(True)` is always a mistake.
        if type(self.paise) is not int:
            raise MoneyError(
                f"Money takes whole paise as an int, got {type(self.paise).__name__}"
                f" ({self.paise!r}). Fractional money is never representable."
            )

    # ── Construction ────────────────────────────────────────────────────────

    @staticmethod
    def zero() -> Money:
        return Money(0)

    @staticmethod
    def from_paise(paise: int) -> Money:
        return Money(paise)

    @staticmethod
    def from_rupees(rupees: Decimal | int | str) -> Money:
        """Build from a rupee figure. Accepts a string or Decimal, never a float.

        A float argument is refused rather than silently accepted, because
        `0.1 + 0.2` is where this whole class of bug begins.
        """
        if isinstance(rupees, Decimal | int | str):
            value = Decimal(rupees)
        else:
            raise MoneyError(
                f"from_rupees needs a Decimal, int or str, got "
                f"{type(rupees).__name__}. Pass '19.99', not 19.99."
            )

        scaled = value * RUPEE
        if scaled != scaled.to_integral_value():
            raise MoneyError(f"{value} rupees is not a whole number of paise")
        return Money(int(scaled))

    # ── Exact arithmetic ────────────────────────────────────────────────────

    def __add__(self, other: Money) -> Money:
        return Money(self.paise + other.paise)

    def __sub__(self, other: Money) -> Money:
        return Money(self.paise - other.paise)

    def __neg__(self) -> Money:
        return Money(-self.paise)

    def __abs__(self) -> Money:
        return Money(abs(self.paise))

    def __mul__(self, count: int) -> Money:
        """Scale by a whole number. Exact, so always safe."""
        if type(count) is not int:
            raise MoneyError(
                "Money can only be multiplied by a whole number. To scale by a "
                "quantity use multiply_by_quantity(); to take a percentage use "
                "percentage_of()."
            )
        return Money(self.paise * count)

    __rmul__ = __mul__

    # ── Inspection ──────────────────────────────────────────────────────────

    @property
    def is_zero(self) -> bool:
        return self.paise == 0

    @property
    def is_negative(self) -> bool:
        return self.paise < 0

    @property
    def is_positive(self) -> bool:
        return self.paise > 0

    @property
    def rupees(self) -> Decimal:
        """Exact rupee value, for display and for the tax boundary."""
        return Decimal(self.paise) / RUPEE

    def __str__(self) -> str:
        sign = "-" if self.paise < 0 else ""
        whole, part = divmod(abs(self.paise), RUPEE)
        return f"{sign}₹{whole:,}.{part:02d}"

    def __format__(self, spec: str) -> str:
        """Allow `f"{amount:>10}"` so receipts can align a column of figures.

        Without this, a frozen dataclass inherits `object.__format__`, which
        rejects any format spec at all — and the first place that bites is the
        receipt renderer, where every amount is right-aligned.
        """
        return format(str(self), spec)

    def __repr__(self) -> str:
        return f"Money({self.paise})"


#: A shared zero. Money is frozen, so this is safe to use as a default
#: argument, which `Money.zero()` is not (a call in a default is evaluated
#: once at import and looks like a mutable-default bug even when it is not).
ZERO = Money(0)


def total(amounts: Sequence[Money] | Iterator[Money]) -> Money:
    """Sum, exactly. Empty sums to zero rather than raising."""
    return Money(sum(amount.paise for amount in amounts))


# ── Operations that round ───────────────────────────────────────────────────
# Each of these can lose a paisa, so each is explicit, named, and rounds once.


def quantize_to_paise(value: Decimal) -> int:
    """Decimal to whole paise, ROUND_HALF_UP.

    HALF_UP, not Python's default banker's rounding: it is what Indian tax
    practice and every auditor expects, and it is what the customer expects
    when they check the arithmetic on a receipt.
    """
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def multiply_by_quantity(unit_price: Money, qty_milli: int) -> Money:
    """Unit price times a quantity in thousandths, rounded once.

    This is the one place a line's gross amount comes from. 1.250 kg at
    ₹40.00/kg is 1250 thousandths times 4000 paise, which is exactly 5000
    paise; but 0.333 kg is not exact, and rounding it here — once, at the
    line — is what architecture §6 requires.
    """
    if type(qty_milli) is not int:
        raise MoneyError(f"quantity must be integer thousandths, got {qty_milli!r}")
    return Money(quantize_to_paise(Decimal(unit_price.paise) * qty_milli / QUANTITY_SCALE))


def percentage_of(amount: Money, basis_points: int) -> Money:
    """A percentage of an amount, in basis points. 10% is 1000 bp.

    Basis points rather than a percentage keeps the input an integer, so a
    "12.5% off" promotion never needs a float to express.
    """
    if type(basis_points) is not int:
        raise MoneyError(f"basis points must be an int, got {basis_points!r}")
    return Money(quantize_to_paise(Decimal(amount.paise) * basis_points / 10_000))


def allocate(amount: Money, weights: Sequence[int]) -> list[Money]:
    """Split an amount across weights so the parts sum to the whole exactly.

    Largest-remainder: floor every share, then hand the leftover paise out one
    at a time to whichever share was cut hardest. Naive rounding of each share
    independently loses or invents paise, and on a GST breakdown that shows up
    as a receipt whose tax lines do not add to its total.

    Ties go to the earlier line, so the result is deterministic — the same
    basket always produces the same receipt.
    """
    if not weights:
        raise MoneyError("cannot allocate across no weights")
    if any(weight < 0 for weight in weights):
        raise MoneyError("allocation weights must not be negative")

    total_weight = sum(weights)
    if total_weight == 0:
        # Nothing to weight by: give it all to the first share rather than
        # dividing by zero or silently dropping the amount.
        return [amount] + [Money.zero()] * (len(weights) - 1)

    shares: list[int] = []
    remainders: list[tuple[int, int]] = []  # (remainder, index)
    for index, weight in enumerate(weights):
        exact = Decimal(amount.paise) * weight
        floor_share = int(exact // total_weight)
        shares.append(floor_share)
        remainders.append((int(exact - floor_share * total_weight), index))

    leftover = amount.paise - sum(shares)

    # A negative amount floors away from zero, so the leftover is handed out
    # in the same direction the amount points.
    step = 1 if leftover >= 0 else -1
    remainders.sort(key=lambda pair: (-pair[0], pair[1]))
    for offset in range(abs(leftover)):
        shares[remainders[offset % len(remainders)][1]] += step

    return [Money(share) for share in shares]

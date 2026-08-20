"""Barcodes — architecture §10.2.

Two jobs, and they are a matched pair:

  **Parse** whatever arrives as text. With no scanner in v1 the codes are
  typed, but they still have to be understood correctly — a mistyped digit
  must be refused rather than resolved to the wrong product.

  **Generate** the code for a weighed item. The target shops are too small to
  own a label-printing scale, so nothing external ever produces one: the
  shopkeeper weighs by hand, keys the quantity, and the POS emits the code
  itself. That makes the GS1 in-store range ours to author, and turns what
  would be the riskiest branch here — reverse-engineering an unknown scale's
  digit layout — into a round trip against a format we define.

The in-store range (EAN-13 prefixes 20-29) is partitioned deliberately, because
two different things need to live in it and they must never be confused:

    21…   internal product code   identity, permanent, one per product
    22…   weighed item            one weighing, carries its own quantity

`21…` is reserved by architecture §10.2 for products with no manufacturer
barcode — 38.6% of the pilot store's catalogue, so it will be heavily used.
`22…` is this module's. 20 and 23-29 stay free.

**A code that fails its check digit is never interpreted.** Not as a product,
not as a weight, not as anything. Reading a quantity out of a corrupt number is
how a till charges the wrong amount with complete confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Symbology = Literal[
    "EAN13", "EAN8", "UPC_A", "ITF14", "GS1_128", "INTERNAL", "WEIGHED", "UNKNOWN"
]

#: The GS1 range reserved for in-store use. No manufacturer ships these.
IN_STORE_PREFIXES = frozenset(f"{n:02d}" for n in range(20, 30))

INTERNAL_PREFIX = "21"
WEIGHED_PREFIX = "22"

#: 22 | 5-digit item code | 5-digit grams | check digit
WEIGHED_ITEM_DIGITS = 5
WEIGHED_QTY_DIGITS = 5
MAX_WEIGHED_ITEM_CODE = 10**WEIGHED_ITEM_DIGITS - 1  # 99999
MAX_WEIGHED_QTY_MILLI = 10**WEIGHED_QTY_DIGITS - 1  # 99.999 kg

GTIN_LENGTHS = frozenset({8, 12, 13, 14})


class BarcodeError(ValueError):
    """A barcode could not be parsed or generated."""


# ── Check digits ────────────────────────────────────────────────────────────


def check_digit(digits: str) -> int:
    """GS1 mod-10 over a payload (everything but the check digit itself).

    Weights alternate 3,1 from the right. The same rule serves EAN-8, UPC-A,
    EAN-13 and ITF-14; only the length differs.
    """
    if not digits.isdigit():
        raise BarcodeError(f"not numeric: {digits!r}")

    total = 0
    for index, char in enumerate(reversed(digits)):
        total += int(char) * (3 if index % 2 == 0 else 1)
    return (10 - total % 10) % 10


def with_check_digit(payload: str) -> str:
    """Append the check digit to a payload."""
    return payload + str(check_digit(payload))


def is_valid_gtin(code: str) -> bool:
    if len(code) not in GTIN_LENGTHS or not code.isdigit():
        return False
    return check_digit(code[:-1]) == int(code[-1])


# ── Parsing ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What a scanned or typed code turned out to mean.

    Deliberately does *not* resolve a product. The domain has no catalogue and
    no database; it reports what the digits say and hands `lookup_key` to the
    service layer, which owns resolution. That split is what keeps this module
    pure and testable in milliseconds.
    """

    raw: str
    symbology: Symbology
    #: The value to look up in `product_barcodes`, or None if unusable.
    lookup_key: str | None
    #: Quantity in thousandths when the code carries one (a weighed item).
    qty_milli: int | None = None
    #: True when the code is a case/outer whose pack size multiplies the line.
    is_case: bool = False
    #: GS1-128 application identifiers, when present.
    batch: str | None = None
    expiry: str | None = None
    #: Why the code could not be used, when it could not be.
    error: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.lookup_key is not None

    @property
    def carries_quantity(self) -> bool:
        return self.qty_milli is not None


@dataclass(frozen=True, slots=True)
class StoreRules:
    """Per-store parsing configuration.

    Small today because the shop's own format is the only variable one. It
    exists so that a second store with a different internal scheme does not
    require editing this module.
    """

    internal_prefix: str = INTERNAL_PREFIX
    weighed_prefix: str = WEIGHED_PREFIX
    #: Accept codes whose check digit fails. Off, and should stay off: it
    #: exists to be turned on temporarily against a supplier with known-bad
    #: labels, never as a default.
    allow_invalid_check_digit: bool = False


DEFAULT_RULES = StoreRules()


def _unusable(raw: str, symbology: Symbology, reason: str) -> ScanResult:
    return ScanResult(raw=raw, symbology=symbology, lookup_key=None, error=reason)


def parse(raw: str, rules: StoreRules = DEFAULT_RULES) -> ScanResult:
    """Interpret a scanned or typed code.

    Never raises for bad input: an unreadable code is a `ScanResult` with an
    error on it, because the till must show the cashier something useful
    rather than crashing mid-basket. It raises only for programmer error.
    """
    code = raw.strip()

    if not code:
        return _unusable(raw, "UNKNOWN", "no code entered")

    # GS1-128 arrives with application identifiers and is variable length, so
    # it is recognised before the plain-length rules.
    if _looks_like_gs1_128(code):
        return _parse_gs1_128(raw, code)

    if not code.isdigit():
        # Code128 / Code39 internal SKUs are alphanumeric and carry no
        # structure we can verify — they are simply looked up as typed.
        return ScanResult(raw=raw, symbology="UNKNOWN", lookup_key=code)

    if len(code) not in GTIN_LENGTHS:
        # A supplier or legacy internal code. 0.5% of the pilot catalogue is
        # this shape, so it is a normal case, not an error.
        return ScanResult(raw=raw, symbology="UNKNOWN", lookup_key=code)

    if not is_valid_gtin(code) and not rules.allow_invalid_check_digit:
        expected = check_digit(code[:-1])
        return _unusable(
            raw,
            _symbology_for_length(len(code)),
            f"check digit is {code[-1]}, expected {expected}",
        )

    if len(code) == 14:
        return ScanResult(raw=raw, symbology="ITF14", lookup_key=code, is_case=True)

    if len(code) == 13:
        prefix = code[:2]
        if prefix == rules.weighed_prefix:
            return _parse_weighed(raw, code)
        if prefix == rules.internal_prefix:
            return ScanResult(raw=raw, symbology="INTERNAL", lookup_key=code)
        if prefix in IN_STORE_PREFIXES:
            # Reserved but unallocated. Refusing beats guessing: an unhandled
            # in-store prefix means the store's scheme has grown and this
            # module has not been told about it.
            return _unusable(
                raw, "EAN13", f"in-store prefix {prefix} is not configured for this store"
            )
        return ScanResult(raw=raw, symbology="EAN13", lookup_key=code)

    return ScanResult(
        raw=raw, symbology=_symbology_for_length(len(code)), lookup_key=code
    )


def _symbology_for_length(length: int) -> Symbology:
    return {8: "EAN8", 12: "UPC_A", 13: "EAN13", 14: "ITF14"}.get(length, "UNKNOWN")  # type: ignore[return-value]


def _parse_weighed(raw: str, code: str) -> ScanResult:
    """Take apart a `22…` code this POS generated."""
    item_code = code[2 : 2 + WEIGHED_ITEM_DIGITS]
    quantity = code[2 + WEIGHED_ITEM_DIGITS : -1]

    qty_milli = int(quantity)
    if qty_milli == 0:
        return _unusable(raw, "WEIGHED", "weighed code carries no quantity")

    return ScanResult(
        raw=raw,
        symbology="WEIGHED",
        # The item code is the lookup key, not the whole barcode: the same
        # product weighs differently every time, so the full code is unique
        # per weighing and would never match a catalogue row.
        lookup_key=item_code,
        qty_milli=qty_milli,
    )


# ── GS1-128 ─────────────────────────────────────────────────────────────────
# Only the identifiers a grocery till actually meets. An unknown AI stops
# parsing rather than being skipped, because skipping one of unknown length
# would silently misread everything after it.

_FIXED_LENGTH_AIS = {
    "01": 14,  # GTIN
    "17": 6,   # expiry, YYMMDD
    "11": 6,   # production date
}
_VARIABLE_LENGTH_AIS = {"10", "21"}  # batch/lot, serial
#: AI 310n — net weight in kilograms, n implied decimal places.
_NET_WEIGHT_AI = "310"
_MAX_VARIABLE = 20
_GROUP_SEPARATOR = "\x1d"


def _looks_like_gs1_128(code: str) -> bool:
    if _GROUP_SEPARATOR in code:
        return True
    # A bare "01" + 14 digits is a GTIN in AI form; shorter strings that merely
    # start with 01 are ordinary codes.
    return code.startswith("01") and len(code) > 14 and code.isdigit()


def _parse_gs1_128(raw: str, code: str) -> ScanResult:
    gtin: str | None = None
    batch: str | None = None
    expiry: str | None = None
    qty_milli: int | None = None

    position = 0
    while position < len(code):
        if code[position] == _GROUP_SEPARATOR:
            position += 1
            continue

        # Net weight is a *four*-character AI: "310" identifies it and the
        # fourth digit says how many decimal places the six-digit value has.
        # Framing it as two characters silently misreads the rest of the code,
        # so it is matched before the two-character table.
        if code[position : position + 3] == _NET_WEIGHT_AI:
            decimals = int(code[position + 3])
            value = code[position + 4 : position + 10]
            position += 10
            qty_milli = _weight_to_milli(value, decimals)
            continue

        ai = code[position : position + 2]
        position += 2

        if ai in _FIXED_LENGTH_AIS:
            length = _FIXED_LENGTH_AIS[ai]
            value = code[position : position + length]
            position += length
            if ai == "01":
                gtin = value
            elif ai == "17":
                expiry = value
        elif ai in _VARIABLE_LENGTH_AIS:
            end = code.find(_GROUP_SEPARATOR, position)
            if end == -1:
                end = min(len(code), position + _MAX_VARIABLE)
            value = code[position:end]
            position = end
            if ai == "10":
                batch = value
        else:
            return _unusable(raw, "GS1_128", f"unsupported application identifier {ai}")

    if gtin is None:
        return _unusable(raw, "GS1_128", "no GTIN (AI 01) in the code")

    # A GS1-128 GTIN is zero-padded to 14; the catalogue holds the EAN-13.
    lookup = gtin.lstrip("0") if len(gtin) == 14 and gtin.startswith("0") else gtin

    return ScanResult(
        raw=raw,
        symbology="GS1_128",
        lookup_key=lookup,
        qty_milli=qty_milli,
        batch=batch,
        expiry=expiry,
        is_case=len(gtin) == 14 and not gtin.startswith("0"),
    )


def _weight_to_milli(value: str, decimals: int) -> int:
    """AI 310n net weight, in kilograms with n implied decimals, to thousandths.

    310**3**|001250 is 1.250 kg, which is 1250 thousandths. Integer arithmetic
    throughout — shifting the implied decimal point, never dividing into a
    float.
    """
    if not value.isdigit():
        raise BarcodeError(f"non-numeric weight in GS1-128: {value!r}")

    if decimals <= 3:
        # The exponent is non-negative here; the int() makes that explicit,
        # since `int ** int` is otherwise indistinguishable from a float power.
        return int(value) * int(10 ** (3 - decimals))
    # More precision than thousandths: truncate, since the system cannot
    # represent it and rounding a weight up would overcharge.
    return int(value) // int(10 ** (decimals - 3))


# ── Generating a weighed-item code ──────────────────────────────────────────


def weighed_barcode(item_code: int | str, qty_milli: int) -> str:
    """Build the EAN-13 for a hand-weighed item.

        22 | 12345 | 01250 | 3
             item    grams   check digit

    Quantities are already integer thousandths throughout this system
    (architecture §6), and for anything sold by the kilo thousandths *are*
    grams — so the quantity is embedded verbatim, with no scaling and no unit
    conversion anywhere on the path.

    Weight is embedded rather than price on purpose: weight is a fact about
    the item, whereas price is derived from it and changes at every repricing.
    A code carrying a stale price would pin the GST base to a number that is
    no longer true.
    """
    code = int(item_code)
    if not 0 <= code <= MAX_WEIGHED_ITEM_CODE:
        raise BarcodeError(
            f"item code {code} does not fit in {WEIGHED_ITEM_DIGITS} digits "
            f"(max {MAX_WEIGHED_ITEM_CODE})"
        )
    if type(qty_milli) is not int:
        raise BarcodeError(f"quantity must be integer thousandths, got {qty_milli!r}")
    if not 1 <= qty_milli <= MAX_WEIGHED_QTY_MILLI:
        raise BarcodeError(
            f"quantity {qty_milli} thousandths is outside the range a weighed "
            f"code can carry (1 to {MAX_WEIGHED_QTY_MILLI} thousandths, which "
            f"is just under 100 kg)"
        )

    payload = (
        f"{WEIGHED_PREFIX}"
        f"{code:0{WEIGHED_ITEM_DIGITS}d}"
        f"{qty_milli:0{WEIGHED_QTY_DIGITS}d}"
    )
    return with_check_digit(payload)


def internal_barcode(item_code: int | str) -> str:
    """Build the EAN-13 identity code for a product with no manufacturer barcode.

    Phase 6 assigns these in bulk; the format is fixed here so that phase 2's
    parser and phase 6's assignment cannot disagree about it.
    """
    code = int(item_code)
    if not 0 <= code <= 9_999_999_999:
        raise BarcodeError(f"internal code {code} does not fit in 10 digits")
    return with_check_digit(f"{INTERNAL_PREFIX}{code:010d}")

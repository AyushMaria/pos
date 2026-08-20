"""Barcodes — architecture §10.2, phase 2 exit criterion.

The corpus tests at the bottom run the parser over real codes pulled from the
pilot store's own catalogue, which is what the plan's §10 item 6 exists for. A
parser validated only on hand-written examples is a parser validated against
the author's assumptions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.barcode import (
    MAX_WEIGHED_ITEM_CODE,
    MAX_WEIGHED_QTY_MILLI,
    BarcodeError,
    StoreRules,
    check_digit,
    internal_barcode,
    is_valid_gtin,
    parse,
    weighed_barcode,
    with_check_digit,
)

CORPUS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "barcode_corpus.json"


# ── Check digits ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code",
    [
        "5901234123457",   # EAN-13, published reference
        "4006381333931",   # EAN-13
        "036000291452",    # UPC-A, published reference
        "96385074",        # EAN-8, published reference
        "8906088880911",   # real, from the pilot store
        "89060447116938",  # real ITF-14, from the pilot store
    ],
)
def test_valid_gtins(code: str) -> None:
    assert is_valid_gtin(code)
    assert with_check_digit(code[:-1]) == code


@pytest.mark.parametrize("code", ["5901234123458", "036000291451", "96385075"])
def test_corrupted_check_digits(code: str) -> None:
    assert not is_valid_gtin(code)


def test_check_digit_refuses_letters() -> None:
    with pytest.raises(BarcodeError, match="not numeric"):
        check_digit("ABC")


# ── Ordinary retail codes ───────────────────────────────────────────────────


def test_an_ean13_resolves_to_itself() -> None:
    result = parse("5901234123457")

    assert result.symbology == "EAN13"
    assert result.lookup_key == "5901234123457"
    assert result.qty_milli is None
    assert not result.is_case


def test_a_upc_a() -> None:
    result = parse("036000291452")

    assert result.symbology == "UPC_A"
    assert result.is_usable


def test_an_itf14_is_a_case() -> None:
    """pack_size on the resolved row multiplies the line (architecture §10.2)."""
    result = parse("89060447116938")

    assert result.symbology == "ITF14"
    assert result.is_case


def test_whitespace_is_trimmed() -> None:
    assert parse("  5901234123457  ").lookup_key == "5901234123457"


def test_an_empty_code() -> None:
    result = parse("")

    assert not result.is_usable
    assert result.error is not None


def test_an_alphanumeric_code_is_looked_up_as_typed() -> None:
    """Code128/Code39 internal SKUs carry no structure to verify."""
    result = parse("ABC-123")

    assert result.lookup_key == "ABC-123"


def test_a_short_supplier_code_is_looked_up_as_typed() -> None:
    """0.5% of the pilot catalogue is this shape. Normal, not an error."""
    result = parse("51116004014")

    assert result.is_usable
    assert result.lookup_key == "51116004014"


# ── Corrupt codes are never interpreted ─────────────────────────────────────


def test_a_bad_check_digit_is_refused() -> None:
    result = parse("5901234123458")

    assert not result.is_usable
    assert result.error is not None
    assert "check digit" in result.error


def test_a_bad_check_digit_is_refused_even_in_the_in_store_range() -> None:
    """The exact trap the pilot catalogue laid: 2211198810777 is not a weight."""
    result = parse("2211198810777")

    assert not result.is_usable
    assert result.qty_milli is None


def test_the_override_exists_but_is_off_by_default() -> None:
    lenient = StoreRules(allow_invalid_check_digit=True)

    assert parse("5901234123458", lenient).is_usable
    assert not parse("5901234123458").is_usable


def test_parse_never_raises_on_junk() -> None:
    """A till must show the cashier something useful, not crash mid-basket."""
    for junk in ("", "   ", "N/A", "0", "-", "!!!", "999999999999999999999"):
        parse(junk)


# ── The in-store range ──────────────────────────────────────────────────────


def test_an_internal_code_is_an_identity() -> None:
    code = internal_barcode(12345)
    result = parse(code)

    assert result.symbology == "INTERNAL"
    assert result.lookup_key == code
    assert result.qty_milli is None


def test_an_unallocated_in_store_prefix_is_refused() -> None:
    """Guessing at an unhandled prefix is how a till invents a quantity."""
    code = with_check_digit("250000000000")
    result = parse(code)

    assert not result.is_usable
    assert result.error is not None
    assert "not configured" in result.error


# ── Weighed items: the round trip ───────────────────────────────────────────


def test_generating_a_weighed_code() -> None:
    """1.250 kg of item 12345."""
    code = weighed_barcode(12345, 1250)

    assert code.startswith("22")
    assert len(code) == 13
    assert is_valid_gtin(code)


def test_a_weighed_code_round_trips() -> None:
    result = parse(weighed_barcode(12345, 1250))

    assert result.symbology == "WEIGHED"
    assert result.lookup_key == "12345"
    assert result.qty_milli == 1250


def test_the_lookup_key_is_the_item_not_the_whole_code() -> None:
    """The same product weighs differently every time.

    If the full code were the key, no weighing would ever match a catalogue
    row — every sale would be an unknown barcode.
    """
    first = parse(weighed_barcode(12345, 1250))
    second = parse(weighed_barcode(12345, 875))

    assert first.lookup_key == second.lookup_key == "12345"
    assert first.raw != second.raw


@given(
    st.integers(min_value=0, max_value=MAX_WEIGHED_ITEM_CODE),
    st.integers(min_value=1, max_value=MAX_WEIGHED_QTY_MILLI),
)
def test_every_weighed_code_round_trips(item_code: int, qty_milli: int) -> None:
    """Generate then parse returns the same item and the same quantity.

    The property that matters most in this module: we author the format, so
    the only way to get it wrong is to disagree with ourselves.
    """
    result = parse(weighed_barcode(item_code, qty_milli))

    assert result.symbology == "WEIGHED"
    assert result.lookup_key is not None
    assert int(result.lookup_key) == item_code
    assert result.qty_milli == qty_milli


@given(
    st.integers(min_value=0, max_value=MAX_WEIGHED_ITEM_CODE),
    st.integers(min_value=1, max_value=MAX_WEIGHED_QTY_MILLI),
)
def test_every_weighed_code_is_a_valid_ean13(item_code: int, qty_milli: int) -> None:
    code = weighed_barcode(item_code, qty_milli)

    assert len(code) == 13
    assert is_valid_gtin(code)


def test_a_weighed_code_never_collides_with_an_internal_one() -> None:
    """The two things sharing the in-store range must stay distinguishable."""
    weighed = parse(weighed_barcode(12345, 1000))
    internal = parse(internal_barcode(12345))

    assert weighed.symbology == "WEIGHED"
    assert internal.symbology == "INTERNAL"


@pytest.mark.parametrize("qty", [0, -1, MAX_WEIGHED_QTY_MILLI + 1, 1_000_000])
def test_an_unrepresentable_quantity_is_refused(qty: int) -> None:
    with pytest.raises(BarcodeError, match="quantity"):
        weighed_barcode(1, qty)


def test_a_float_quantity_is_refused() -> None:
    with pytest.raises(BarcodeError, match="thousandths"):
        weighed_barcode(1, 1.25)  # type: ignore[arg-type]


def test_an_oversized_item_code_is_refused() -> None:
    with pytest.raises(BarcodeError, match="does not fit"):
        weighed_barcode(MAX_WEIGHED_ITEM_CODE + 1, 1000)


def test_a_zero_quantity_code_is_not_interpreted() -> None:
    """Hand-built rather than generated — the generator refuses to make one."""
    code = with_check_digit("221234500000")
    result = parse(code)

    assert not result.is_usable


# ── GS1-128 ─────────────────────────────────────────────────────────────────


def test_gs1_128_gtin_and_batch() -> None:
    result = parse("0108906088880911" + "\x1d" + "10LOT42")

    assert result.symbology == "GS1_128"
    assert result.lookup_key == "8906088880911"
    assert result.batch == "LOT42"


def test_gs1_128_expiry() -> None:
    result = parse("0108906088880911" + "17261231")

    assert result.expiry == "261231"


def test_gs1_128_net_weight() -> None:
    """AI 3103: kilograms to three decimals, so 001250 is 1.250 kg."""
    result = parse("0108906088880911" + "3103001250")

    assert result.qty_milli == 1250


def test_gs1_128_without_a_gtin_is_refused() -> None:
    result = parse("10LOT42\x1d17261231")

    assert not result.is_usable


def test_an_unsupported_ai_stops_parsing() -> None:
    """Skipping an AI of unknown length would misread everything after it."""
    result = parse("0108906088880911" + "9912345")

    assert not result.is_usable
    assert result.error is not None
    assert "application identifier" in result.error


# ── The real catalogue ──────────────────────────────────────────────────────

corpus_required = pytest.mark.skipif(
    not CORPUS_PATH.exists(), reason="run scripts/import_barcode_corpus.py"
)


def _corpus() -> list[dict[str, object]]:
    data = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    items: list[dict[str, object]] = data["barcodes"]
    return items


@corpus_required
def test_the_parser_survives_the_whole_real_corpus() -> None:
    """Phase 2 exit criterion: the parser handles real stock codes.

    Not "parses them all successfully" — a third of this catalogue is junk.
    The criterion is that every one produces a usable answer or an explained
    refusal, and never an exception.
    """
    for entry in _corpus():
        result = parse(str(entry["barcode"]))
        assert result.is_usable or result.error is not None


@corpus_required
def test_valid_catalogue_gtins_resolve() -> None:
    for entry in _corpus():
        if entry["valid"] and str(entry["symbology"]).startswith(("EAN", "UPC", "ITF")):
            result = parse(str(entry["barcode"]))
            assert result.is_usable, f"{entry['barcode']} should resolve"


@corpus_required
def test_catalogue_codes_with_bad_check_digits_are_all_refused() -> None:
    """106 real corrupt codes. Not one may be resolved to a product."""
    refused = 0
    for entry in _corpus():
        if entry.get("kind") == "malformed":
            assert not parse(str(entry["barcode"])).is_usable
            refused += 1

    assert refused > 0, "corpus carries no malformed codes to check against"


@corpus_required
def test_no_catalogue_code_is_mistaken_for_a_weighed_item() -> None:
    """Nothing in the shop's existing data may parse as carrying a quantity."""
    for entry in _corpus():
        result = parse(str(entry["barcode"]))
        assert result.qty_milli is None, f"{entry['barcode']} read as a quantity"

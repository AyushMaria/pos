"""The barcode corpus and the profiler that builds it.

Phase 2 cannot exit without a corpus of real barcodes from actual stock
(execution plan §10 item 6). The parser itself is phase 2 work; what is tested
here is the tool that collects the corpus, and the shape of the corpus once
collected — so that the phase 2 parser tests have something trustworthy to run
against.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from import_barcode_corpus import classify, gtin_check_digit  # noqa: E402

CORPUS_PATH = REPO_ROOT / "tests" / "fixtures" / "barcode_corpus.json"


# ── The mod-10 check digit, against published reference codes ───────────────


@pytest.mark.parametrize(
    "code",
    [
        "5901234123457",  # EAN-13, canonical example
        "4006381333931",  # EAN-13
        "036000291452",   # UPC-A, canonical example
        "96385074",       # EAN-8, canonical example
    ],
)
def test_valid_gtins_verify(code: str) -> None:
    assert gtin_check_digit(code) == int(code[-1])
    assert classify(code)["valid"] is True


@pytest.mark.parametrize("code", ["5901234123458", "4006381333930", "036000291451"])
def test_corrupted_check_digits_are_caught(code: str) -> None:
    """The commonest source of junk in a catalogue export (plan §6 step 2)."""
    assert classify(code)["valid"] is False


@pytest.mark.parametrize(
    "code,symbology",
    [
        ("5901234123457", "EAN13"),
        ("036000291452", "UPC_A"),
        ("96385074", "EAN8"),
        ("18901719101257", "ITF14"),
        ("ABC-123", "NON_NUMERIC"),
        ("", "EMPTY"),
        ("12345", "LEN_5"),
    ],
)
def test_symbology_is_identified_by_length(code: str, symbology: str) -> None:
    assert classify(code)["symbology"] == symbology


@pytest.mark.parametrize(
    "code,kind",
    [
        ("2012345678903", "scale_label"),   # prefix 20-29: embedded weight/price
        ("2112345678900", "scale_label"),   # 21 is inside the 20-29 range
        ("8901030865275", "india_gs1"),
        ("8906088880911", "india_gs1"),     # real, from the shop's catalogue
        ("5901234123457", "manufacturer"),
        ("18901719101257", "case_code"),
        ("89060447116938", "case_code"),    # real, from the shop's catalogue
    ],
)
def test_prefixes_that_change_interpretation(code: str, kind: str) -> None:
    """Architecture §10.2 — a scale label is not an ordinary retail unit."""
    assert classify(code)["kind"] == kind


@pytest.mark.parametrize(
    "code",
    [
        "2164985487216",  # looks like a scale label, fails its check digit
        "2211198810777",  # ditto — actually a supplier's date-based numbering
        "89080087630167",  # 14 digits, but not a real case code
    ],
)
def test_a_corrupt_code_is_never_given_a_meaning(code: str) -> None:
    """A failed check digit means the code is unreadable, not interesting.

    All three of these are real rows from the shop's catalogue that the
    prefix rules would happily have mislabelled. Reading a scale label's
    embedded price off a corrupt number is how a till charges the wrong
    amount with complete confidence.
    """
    result = classify(code)
    assert result["valid"] is False
    assert result["kind"] == "malformed"


def test_blank_and_junk_do_not_raise() -> None:
    """A catalogue export contains blanks; profiling must survive them."""
    for junk in ("", "   ", "N/A", "0", "-"):
        assert classify(junk)["valid"] is False


# ── The corpus itself ───────────────────────────────────────────────────────

corpus_required = pytest.mark.skipif(
    not CORPUS_PATH.exists(),
    reason=(
        "no barcode corpus yet — run scripts/import_barcode_corpus.py. "
        "Phase 2 cannot exit without it (execution plan §10 item 6)."
    ),
)


@corpus_required
def test_corpus_is_well_formed() -> None:
    data = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

    assert data["count"] == len(data["barcodes"])
    for entry in data["barcodes"]:
        assert "barcode" in entry
        assert "symbology" in entry
        assert isinstance(entry["valid"], bool)


def _corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


@corpus_required
def test_corpus_is_large_enough() -> None:
    """Plan §10 item 6 asks for 50+ real codes from actual stock."""
    assert _corpus()["count"] >= 50


@corpus_required
def test_corpus_covers_the_ordinary_symbologies() -> None:
    symbologies = {entry["symbology"] for entry in _corpus()["barcodes"]}

    assert {"EAN13", "UPC_A", "EAN8"} <= symbologies


@corpus_required
def test_corpus_contains_a_case_code() -> None:
    kinds = [entry.get("kind") for entry in _corpus()["barcodes"]]

    assert "case_code" in kinds, "no verified ITF-14 case code in the corpus"


@corpus_required
def test_corpus_carries_real_malformed_codes() -> None:
    """The parser must survive the catalogue as it is, not as it should be.

    0.3% of this catalogue has a bad check digit and 0.5% is not a GTIN length
    at all. A parser only ever tested on clean input will meet these at the
    till instead.
    """
    kinds = [entry.get("kind") for entry in _corpus()["barcodes"]]

    assert kinds.count("malformed") > 0
    assert any(e["symbology"].startswith("LEN_") for e in _corpus()["barcodes"])


@corpus_required
def test_the_catalogue_has_no_scale_labels() -> None:
    """And that is correct for this market — recorded so it stays deliberate.

    The target shops are too small to own a label-printing scale. Nothing
    external ever produces a scale label, so there is no third-party format to
    reverse-engineer: the POS itself generates the code for a weighed item
    after the shopkeeper enters a hand-weighed quantity.

    That makes the 20-29 range *ours to author*, and turns what would have been
    the riskiest parser branch into a round-trip against a format we control.

    Three codes here look like scale labels (prefix 21-24) but all fail their
    check digit — they are a supplier's date-based numbering on a bamboo
    container, a camphor cone and a broom. If this assertion ever fails, a real
    in-store-range code has appeared in the catalogue and the generator's
    prefix allocation needs re-checking before it collides with one.
    """
    kinds = [entry.get("kind") for entry in _corpus()["barcodes"]]

    assert "scale_label" not in kinds

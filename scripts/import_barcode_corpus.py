"""Pull real barcodes out of the legacy billing Supabase into a test corpus.

Execution plan §10 item 6: collect real barcode numbers from actual stock as
the phase 2 test corpus. Phase 2 cannot exit without one — a parser validated
only against hand-written examples is a parser validated against the author's
assumptions, and real catalogues are full of short codes, bad check digits and
the same number on two products.

Plan §6 applies even at this scale: a repeatable script, never a one-off. The
catalogue will be re-exported more than once between now and go-live.

    # credentials come from .env (the legacy billing app's project)
    python scripts/import_barcode_corpus.py
    python scripts/import_barcode_corpus.py --include-names   # aids debugging
    python scripts/import_barcode_corpus.py --profile-only    # look, don't write

Writes tests/fixtures/barcode_corpus.json.

NOTE ON THE CLASSIFIER BELOW: it is a *profiling* heuristic, not the parser.
The real parser is phase 2 work and lives in app/domain/barcode.py. This one
exists to tell you what is in the catalogue before you write that parser —
if it and the real parser ever disagree, the real parser is right.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUT_PATH = REPO_ROOT / "tests" / "fixtures" / "barcode_corpus.json"

LEGACY_TABLE = "products"
LEGACY_BARCODE_COLUMN = "upc_ean_code"
LEGACY_NAME_COLUMN = "item_name"

PAGE_SIZE = 1000


def load_credentials() -> tuple[str, str]:
    """Legacy billing-app credentials.

    Deliberately *not* the POS_ prefixed settings: this reads the old system,
    which is a different project with a different schema. Nothing here writes.
    """
    env: dict[str, str] = {}
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")

    url = os.environ.get("LEGACY_SUPABASE_URL") or env.get("SUPABASE_URL", "")
    key = os.environ.get("LEGACY_SUPABASE_KEY") or env.get("SUPABASE_KEY", "")

    if not url or not key:
        raise SystemExit(
            "No legacy Supabase credentials found.\n"
            "Expected SUPABASE_URL and SUPABASE_KEY in .env, or "
            "LEGACY_SUPABASE_URL / LEGACY_SUPABASE_KEY in the environment."
        )
    return url.rstrip("/"), key


def fetch_rows(url: str, key: str) -> list[dict[str, Any]]:
    """Every product row, paginated. Read-only."""
    import httpx

    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    columns = f"{LEGACY_BARCODE_COLUMN},{LEGACY_NAME_COLUMN}"
    rows: list[dict[str, Any]] = []

    with httpx.Client(timeout=30.0) as client:
        offset = 0
        while True:
            response = client.get(
                f"{url}/rest/v1/{LEGACY_TABLE}",
                params={"select": columns, "limit": PAGE_SIZE, "offset": offset},
                headers=headers,
            )
            if response.status_code != 200:
                raise SystemExit(
                    f"Supabase returned {response.status_code}: {response.text[:300]}"
                )
            page = response.json()
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            offset += PAGE_SIZE


# ── Profiling heuristic (NOT the parser — see module docstring) ─────────────


def gtin_check_digit(digits: str) -> int:
    """Standard GS1 mod-10 over all but the last digit.

    Weights alternate 3,1 from the right of the payload, which is the same
    rule for EAN-8, UPC-A, EAN-13 and ITF-14 — only the length differs.
    """
    payload = digits[:-1]
    total = 0
    for index, char in enumerate(reversed(payload)):
        weight = 3 if index % 2 == 0 else 1
        total += int(char) * weight
    return (10 - total % 10) % 10


def classify(raw: str) -> dict[str, Any]:
    code = raw.strip()
    result: dict[str, Any] = {"barcode": code}

    if not code:
        return {**result, "symbology": "EMPTY", "valid": False, "note": "blank"}
    if not code.isdigit():
        return {
            **result,
            "symbology": "NON_NUMERIC",
            "valid": False,
            "note": "letters or punctuation — Code128/Code39 internal SKU?",
        }

    length = len(code)
    symbology = {
        8: "EAN8",
        12: "UPC_A",
        13: "EAN13",
        14: "ITF14",
    }.get(length, f"LEN_{length}")

    result["symbology"] = symbology

    if length not in (8, 12, 13, 14):
        # Not corrupt — just not a GTIN. Supplier and legacy internal codes of
        # 1 to 19 digits are 0.5% of this catalogue and are looked up as typed.
        # Calling them "malformed" would blame the shop for its own numbering.
        return {
            **result,
            "valid": False,
            "kind": "other",
            "note": "not a GTIN length — internal or supplier code",
        }

    expected = gtin_check_digit(code)
    actual = int(code[-1])
    result["valid"] = expected == actual

    # A GTIN-shaped code that fails its own check digit means nothing. Reading
    # a prefix rule off it would invent facts: this catalogue contains 13-digit
    # codes beginning 21-24 that look like scale labels but are a supplier's
    # date-based internal numbering, attached to a bamboo container and a
    # broom. Corrupt is corrupt; do not guess at intent.
    if not result["valid"]:
        result["note"] = f"check digit is {actual}, should be {expected}"
        result["kind"] = "malformed"
        return result

    # Prefixes that change how a scan is interpreted (architecture §10.2).
    if length == 13:
        prefix2 = code[:2]
        if prefix2 in {f"{n:02d}" for n in range(20, 30)}:
            result["kind"] = "scale_label"
            # Append rather than replace: a scale label can also have a bad
            # check digit, and losing that would flatter the catalogue.
            note = "EAN-13 prefix 20-29: embedded weight or price"
            result["note"] = f"{result['note']}; {note}" if result.get("note") else note
        elif code.startswith("890"):
            result["kind"] = "india_gs1"
        else:
            result["kind"] = "manufacturer"
    elif length == 14:
        result["kind"] = "case_code"
    else:
        result["kind"] = "other"

    return result


def _failure_class(entry: dict[str, Any]) -> str:
    """Why a code is unusable, split into causes with different remedies.

    Lumping these together flatters nobody: "12,703 failures" reads like a data
    disaster, when almost all of it is products that never had a barcode at
    all. That is a labelling decision (plan §6 step 5), not corrupt data, and
    it is fixed by assigning internal codes rather than by cleaning.
    """
    if entry["symbology"] == "EMPTY":
        return "no barcode at all"
    if entry["symbology"] == "NON_NUMERIC":
        return "non-numeric"
    if entry["symbology"].startswith("LEN_"):
        return "not a GTIN length"
    return "bad check digit"


def profile(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    total = len(entries)
    by_symbology = Counter(e["symbology"] for e in entries)
    by_kind = Counter(e.get("kind", "other") for e in entries)

    scannable = [e for e in entries if e["symbology"] != "EMPTY"]
    failures = Counter(_failure_class(e) for e in entries if not e["valid"])

    lines.append(f"{total} products, {len(scannable)} with a barcode\n")

    lines.append("by symbology:")
    for name, count in by_symbology.most_common():
        lines.append(f"  {name:<14} {count:>6}")

    lines.append("\nby kind:")
    for name, count in by_kind.most_common():
        lines.append(f"  {name:<14} {count:>6}")

    lines.append("\nunusable, by cause:")
    for cause, count in failures.most_common():
        share = f"{100 * count / total:.1f}%"
        lines.append(f"  {cause:<20} {count:>6}  ({share} of catalogue)")

    bad_digit = [
        e for e in entries if not e["valid"] and _failure_class(e) == "bad check digit"
    ]
    if bad_digit:
        lines.append("\nsample of bad check digits:")
        for entry in bad_digit[:8]:
            lines.append(f"  {entry['barcode']:<16} {entry.get('note', '')}")

    # The defect that makes a till ring up the wrong product (plan §6 step 2).
    # Blanks are excluded: 12k products sharing "no barcode" is not a clash.
    seen: dict[str, int] = defaultdict(int)
    for entry in scannable:
        seen[entry["barcode"]] += 1
    duplicates = {code: n for code, n in seen.items() if n > 1}

    lines.append(f"\nduplicate barcodes (excluding blanks): {len(duplicates)}")
    for code, count in sorted(duplicates.items(), key=lambda kv: -kv[1])[:10]:
        lines.append(f"  {code:<16} on {count} products")

    coverage = {
        "a case code (ITF-14)": by_kind.get("case_code", 0),
        "a scale label (20-29)": by_kind.get("scale_label", 0),
        "an imported code (non-890)": by_kind.get("manufacturer", 0),
        "50+ scannable codes": len(scannable),
    }
    lines.append("\nphase 2 corpus coverage (plan section 10 item 6):")
    for label, count in coverage.items():
        lines.append(f"  [{'x' if count > 0 else ' '}] {label:<28} {count}")

    thin = [label for label, count in coverage.items() if 0 < count < 5]
    if thin:
        lines.append(
            "\n  Thin coverage in: "
            + ", ".join(thin)
            + "\n  These branches carry the most parser risk and the fewest real"
            "\n  examples. Collect more from the shelf before phase 2 exits."
        )

    return "\n".join(lines)


def build_corpus(entries: list[dict[str, Any]], per_bucket: int) -> list[dict[str, Any]]:
    """A representative sample, not the whole catalogue.

    32k barcodes in a test fixture would be a multi-megabyte file in which
    18,000 near-identical EAN-13s drown the handful of interesting cases. What
    the parser tests need is every *branch* represented: each symbology, each
    prefix rule, each way a code can be malformed.

    Buckets smaller than the cap are kept whole, so the rare cases — the case
    codes, the scale labels, the 19-digit oddities — all survive. Selection is
    evenly spaced over a sorted bucket rather than random, so re-running the
    import produces a stable diff instead of churn.
    """
    buckets: dict[tuple[str, str, bool], list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        if entry["symbology"] == "EMPTY":
            continue  # nothing to parse
        key = (entry["symbology"], entry.get("kind", "other"), entry["valid"])
        buckets[key].append(entry)

    sampled: list[dict[str, Any]] = []
    for key in sorted(buckets):
        bucket = sorted(buckets[key], key=lambda e: e["barcode"])
        if len(bucket) <= per_bucket:
            sampled.extend(bucket)
            continue
        step = len(bucket) / per_bucket
        sampled.extend(bucket[int(i * step)] for i in range(per_bucket))

    return sorted(sampled, key=lambda e: (e["symbology"], e["barcode"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-names", action="store_true",
                        help="keep product names in the fixture (aids debugging)")
    parser.add_argument("--profile-only", action="store_true",
                        help="print the profile without writing the fixture")
    parser.add_argument("--per-bucket", type=int, default=25,
                        help="max samples per (symbology, kind, validity) bucket")
    args = parser.parse_args()

    url, key = load_credentials()
    print(f"reading {LEGACY_TABLE} from the legacy project ...")
    rows = fetch_rows(url, key)
    print(f"fetched {len(rows)} rows\n")

    entries: list[dict[str, Any]] = []
    for row in rows:
        entry = classify(str(row.get(LEGACY_BARCODE_COLUMN) or ""))
        if args.include_names:
            entry["name"] = row.get(LEGACY_NAME_COLUMN) or ""
        entries.append(entry)

    print(profile(entries))

    if args.profile_only:
        return 0

    corpus = build_corpus(entries, args.per_bucket)
    no_barcode = sum(1 for e in entries if e["symbology"] == "EMPTY")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {
                "source": "legacy billing Supabase, table 'products'",
                "note": (
                    "Real barcodes from actual stock, sampled to cover every "
                    "branch rather than every row. Classification here is a "
                    "profiling heuristic; app/domain/barcode.py is authoritative."
                ),
                "catalogue_rows": len(entries),
                "catalogue_without_barcode": no_barcode,
                "count": len(corpus),
                "barcodes": corpus,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nsampled {len(corpus)} of {len(entries) - no_barcode} scannable codes")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

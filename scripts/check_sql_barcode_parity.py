"""Prove `legacy.scan` agrees with `app/domain/barcode.py`.

`scripts/migrate_legacy_catalog.sql` contains a plpgsql translation of
`parse()`. A fork of domain logic is a place for the two copies to drift, and
this repository's answer to that elsewhere — the permission matrix — is to
generate rather than copy. A one-time import script cannot be generated, so it
is checked instead: both implementations run over the 308 real codes in
`tests/fixtures/barcode_corpus.json` and every verdict must match.

    pip install -e ".[pg]"
    POS_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres \
        python scripts/check_sql_barcode_parity.py

Skips (exit 0) when POS_TEST_PG_DSN is unset, matching tests/test_rls.py.

The one deliberate disagreement is GS1-128. The Python parser reads
application identifiers; the SQL declines and routes those codes to
`legacy.import_exceptions` for a person to look at. The check asserts that
this is the *only* shape they differ on, so the exemption cannot quietly widen
to cover a real bug.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.domain.barcode import parse  # noqa: E402

CORPUS = REPO_ROOT / "tests" / "fixtures" / "barcode_corpus.json"
MIGRATION = REPO_ROOT / "scripts" / "migrate_legacy_catalog.sql"

#: Extra shapes worth checking that the sampled corpus happens not to contain.
#
# Check digits are real. An invented code with a wrong one is refused before
# the parser reaches the branch you meant to test, so a list of plausible-
# looking digits would pass this check while exercising nothing — which is
# exactly what the first draft of this file did.
SYNTHETIC = [
    "",
    "   ",
    "2200010012507",   # weighed, 1.250 kg — the branch with the fiddly offsets
    "2200010000009",   # weighed, carrying no quantity — must be refused
    "2100000000012",   # in-store product identity
    "2300000000016",   # reserved but unallocated — must be refused
    "2000000000015",   # likewise, the bottom of the in-store range
    "96385074",        # EAN-8
    "036000291452",    # UPC-A
    "18901030865272",  # ITF-14 case code
    "ABC-123",         # alphanumeric internal SKU, looked up as typed
    "8901030865278",   # a real India GS1 code
]


def sql_function_source() -> str:
    """The two function definitions, lifted out of the migration verbatim.

    Reading them from the file that will actually be run is the point: a copy
    pasted in here could pass while the migration ships something else.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    start = text.index("create or replace function legacy.gs1_check_digit")
    end = text.index("-- ── Tax codes")
    return text[start:end]


def main() -> int:
    dsn = os.environ.get("POS_TEST_PG_DSN")
    if not dsn:
        print("POS_TEST_PG_DSN unset — skipping SQL/Python barcode parity check")
        return 0

    import psycopg

    codes = [b["barcode"] for b in json.loads(CORPUS.read_text())["barcodes"]]
    codes += SYNTHETIC

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("create schema if not exists legacy")
        conn.execute(sql_function_source())

        mismatches: list[str] = []
        gs1_exempt = 0

        for raw in codes:
            expected = parse(raw)
            row = conn.execute(
                "select lookup_key, symbology, carries_qty from legacy.scan(%s)",
                (raw,),
            ).fetchone()
            assert row is not None
            got_key, got_symbology, got_qty = row

            if got_symbology == "GS1_128" and expected.symbology == "GS1_128":
                # The documented exemption: SQL refuses what Python parses.
                gs1_exempt += 1
                continue

            if (got_key, got_symbology, bool(got_qty)) != (
                expected.lookup_key,
                expected.symbology,
                expected.carries_quantity,
            ):
                mismatches.append(
                    f"  {raw!r}\n"
                    f"    python: key={expected.lookup_key!r} "
                    f"symbology={expected.symbology} qty={expected.carries_quantity}\n"
                    f"    sql:    key={got_key!r} symbology={got_symbology} qty={bool(got_qty)}"
                )

    print(f"checked {len(codes)} codes ({gs1_exempt} GS1-128 exempt)")
    if mismatches:
        print(f"\n{len(mismatches)} disagreement(s) between legacy.scan and parse():\n")
        print("\n".join(mismatches))
        return 1

    print("legacy.scan agrees with app/domain/barcode.py on every code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

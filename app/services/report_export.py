"""Write a report to a file the owner can open without the app.

The phase 8 exit criterion, in its own words. CSV rather than a spreadsheet
format because Excel, LibreOffice and a phone all open it, and nothing here
needs a formula. PDF is the Z-report's job (`zreport_render`), not this one's.

Written beside the receipts and Z-reports under the data directory, and the
path handed back — the same shape as `POST /shifts/{id}/z.pdf`. The page in a
webview cannot download a file, and a report the owner has to find again
should be somewhere a person would look.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

#: A byte-order mark first, so Excel on Windows reads the file as UTF-8. Left
#: out, "Aashirvaad Atta" is fine and anything with a ₹ or a Hindi name in it
#: opens as mojibake — which the owner reads as the till mangling the data.
ENCODING = "utf-8-sig"


def export_path(data_dir: Path, name: str, *, now: datetime) -> Path:
    """`reports/sales-2026-09-24-to-2026-09-24-143207.csv` under the data dir.

    Stamped with the time it was written, so exporting the same range
    twice — once before a late sale pushed, once after — keeps both rather
    than silently replacing the one somebody already emailed.
    """
    return data_dir / "reports" / f"{name}-{now.strftime('%H%M%S')}.csv"


def write_csv(table: list[list[str]], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding=ENCODING, newline="") as handle:
        csv.writer(handle).writerows(table)
    return destination

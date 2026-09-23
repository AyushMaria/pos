"""Z-report rendering — screen and PDF, from one `ZReport`.

The same discipline as receipts: neither renderer computes anything, and the
PDF is drawn from the text rendering, so the paper in the drawer bag and the
screen the supervisor read agree to the paisa.

Two separations the plan insists on are kept visible on the page, not merged
into a friendlier single number:

* **variance and rounding** are separate lines — rounding is already inside
  cash sales and is never the reason the drawer is short;
* **UPI attested and UPI verified** are separate lines — one is what the
  cashier saw on the customer's phone, the other what the bank confirmed.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from app.domain.zreport import ZReport
from app.services.receipt_render import RECEIPT_WIDTH, write_text_pdf


def _when(moment: datetime) -> str:
    return moment.astimezone().strftime("%d-%m-%Y %H:%M")


def rows(report: ZReport) -> list[tuple[str, str]]:
    """The report as (label, value) pairs; a blank label is a section break.

    Shared by text and HTML so the order and wording live in one place.
    """
    f = report.figures
    out: list[tuple[str, str]] = [
        ("Opened", f"{_when(report.opened_at)} {report.opened_by}"),
        ("Closed", f"{_when(report.closed_at)} {report.closed_by}"),
        ("Sales", str(f.sales_count)),
        ("", ""),
        ("Opening float", str(f.opening_float)),
        ("Cash sales", str(f.cash_sales)),
        ("Cash in", str(f.cash_in)),
        ("Cash out", str(f.cash_out)),
        ("Expected in drawer", str(report.expected_cash)),
        ("Counted", str(report.counted_cash)),
        (f"Variance ({report.variance_word})", str(report.variance)),
        ("", ""),
        ("Rounding (inside cash sales)", str(f.rounding)),
        ("UPI attested", str(f.upi_attested)),
        ("UPI verified", str(f.upi_verified)),
        ("Takings", str(f.takings)),
    ]
    if f.under_review_count:
        out += [
            ("", ""),
            (f"Under review ({f.under_review_count})", str(f.under_review_total)),
        ]
        out += [(f"  {receipt_no}", "") for receipt_no in report.under_review_receipts]
    return out


def render_text(report: ZReport) -> str:
    width = RECEIPT_WIDTH
    out: list[str] = [report.store_name.center(width).rstrip()]
    if report.store_gstin:
        out.append(f"GSTIN {report.store_gstin}".center(width).rstrip())
    out.append(f"Z-REPORT  Till {report.terminal_code}".center(width).rstrip())
    out.append("-" * width)
    for label, value in rows(report):
        if not label and not value:
            out.append("-" * width)
            continue
        space = width - len(value)
        out.append(f"{label[:space - 1]:<{space}}{value}".rstrip())
    if report.note:
        out.append("-" * width)
        out.append(f"Note: {report.note}"[: width * 3])
    out.append("-" * width)
    out.append("Not in any total: sales under review.".center(width).rstrip())
    return "\n".join(out) + "\n"


def render_html(report: ZReport) -> str:
    def esc(value: object) -> str:
        return html.escape(str(value))

    body = "".join(
        '<tr class="gap"><td colspan="2"></td></tr>'
        if not label and not value
        else f'<tr><td class="label">{esc(label)}</td><td class="amt">{esc(value)}</td></tr>'
        for label, value in rows(report)
    )
    gstin = f"<p>GSTIN {esc(report.store_gstin)}</p>" if report.store_gstin else ""
    note = f'<p class="note">Note: {esc(report.note)}</p>' if report.note else ""
    return (
        f'<div class="zreport"><h2>{esc(report.store_name)}</h2>{gstin}'
        f"<h3>Z-report · Till {esc(report.terminal_code)}</h3>"
        f"<table>{body}</table>{note}</div>"
    )


def render_pdf(report: ZReport, destination: Path) -> Path:
    return write_text_pdf(
        render_text(report).splitlines(),
        title=f"Z-report {report.close_id}",
        destination=destination,
    )


def zreport_path(data_dir: Path, report: ZReport) -> Path:
    """Beside the receipts, under the data directory (architecture §14)."""
    stamp = report.closed_at.astimezone().strftime("%Y%m%d-%H%M")
    return data_dir / "zreports" / f"Z-{report.terminal_code}-{stamp}.pdf"

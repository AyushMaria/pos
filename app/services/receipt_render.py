"""Receipt rendering — screen and PDF.

Both renderers consume the same `Receipt` and neither computes anything. A
figure that appears on the screen and on the PDF is literally the same object,
so the two can never disagree — which matters because a customer may be
looking at one while an auditor later reads the other.

Delivery in v1 is on screen always, PDF on demand (execution plan §2). The PDF
is written to disk next to the terminal database, outside the install
directory, so an application update never touches it.

The **rounding line is printed whenever it is non-zero**, and the GST block
always shows one row per rate with CGST and SGST split out — that is what
makes an Indian retail invoice checkable by the person holding it.
"""

from __future__ import annotations

import html
from pathlib import Path

from app.domain.receipt import Receipt

RECEIPT_WIDTH = 42  # characters, sized for a 80mm thermal roll later


# ── Plain text (the shape of a till roll; used in logs and tests) ───────────


def render_text(receipt: Receipt) -> str:
    """A monospace rendering. Also what the tests read, because it is the
    least forgiving format — a misaligned total is visible immediately."""
    width = RECEIPT_WIDTH
    out: list[str] = []

    def rule(char: str = "-") -> None:
        out.append(char * width)

    def row(left: str, right: str) -> None:
        space = width - len(right)
        out.append(f"{left[:space - 1]:<{space}}{right}")

    out.append(receipt.store_name.center(width).rstrip())
    if receipt.store_gstin:
        out.append(f"GSTIN {receipt.store_gstin}".center(width).rstrip())
    out.append("")
    row(receipt.receipt_no, receipt.issued_at.strftime("%d-%m-%Y %H:%M"))
    row(f"Till {receipt.terminal_code}", receipt.cashier_name)
    if receipt.is_reprint:
        out.append("*** REPRINT ***".center(width).rstrip())
    if receipt.requires_review:
        out.append("*** HELD FOR REVIEW ***".center(width).rstrip())
    rule()

    for line in receipt.lines:
        out.append(line.description[:width])
        detail = f"  {line.quantity_text} x {line.unit_price}"
        row(detail, str(line.line_total))
        if line.has_discount:
            row("  discount", f"-{line.discount}")

    rule()
    row("Subtotal", str(receipt.subtotal))
    if not receipt.discount_total.is_zero:
        row("Discounts", f"-{receipt.discount_total}")

    for component in receipt.tax_components:
        if component.tax_amount.is_zero and component.rate_bp == 0:
            continue
        percent = f"{component.rate_bp / 100:g}%"
        row(f"  GST {percent} on {component.taxable_base}", str(component.tax_amount))
        row("    CGST / SGST", f"{component.cgst} / {component.sgst}")

    if receipt.shows_rounding:
        # Never folded into the total: an unexplained variance at shift close
        # eventually gets read as cashier theft (architecture §13.4).
        sign = "" if receipt.rounding_adjustment.is_negative else "+"
        row("Rounding", f"{sign}{receipt.rounding_adjustment}")

    rule("=")
    row("TOTAL", str(receipt.grand_total))

    for payment in receipt.payments:
        label = payment.method.upper()
        if not payment.verified:
            label += " (unverified)"
        row(label, str(payment.amount))
    if not receipt.change_due.is_zero:
        row("Change", str(receipt.change_due))

    rule()
    out.append(f"{receipt.item_count} item(s)".center(width).rstrip())
    out.append("Thank you".center(width).rstrip())

    return "\n".join(out)


# ── HTML (what the register screen shows) ──────────────────────────────────


def render_html(receipt: Receipt) -> str:
    def esc(value: object) -> str:
        return html.escape(str(value))

    lines = "".join(
        f"""<tr class="line">
              <td class="desc">{esc(line.description)}</td>
              <td class="qty">{esc(line.quantity_text)}</td>
              <td class="rate">{esc(line.unit_price)}</td>
              <td class="amt">{esc(line.line_total)}</td>
            </tr>"""
        + (
            f"""<tr class="disc"><td colspan="3">Discount</td>
                 <td class="amt">-{esc(line.discount)}</td></tr>"""
            if line.has_discount
            else ""
        )
        for line in receipt.lines
    )

    tax_rows = "".join(
        f"""<tr><td colspan="2">GST {component.rate_bp / 100:g}% on
                 {esc(component.taxable_base)}</td>
             <td class="split">CGST {esc(component.cgst)} &middot;
                 SGST {esc(component.sgst)}</td>
             <td class="amt">{esc(component.tax_amount)}</td></tr>"""
        for component in receipt.tax_components
        if not (component.tax_amount.is_zero and component.rate_bp == 0)
    )

    rounding_row = (
        f"""<tr class="rounding"><td colspan="3">Rounding</td>
             <td class="amt">{esc(receipt.rounding_adjustment)}</td></tr>"""
        if receipt.shows_rounding
        else ""
    )

    payments = "".join(
        f"""<tr><td colspan="3">{esc(payment.method.upper())}
             {"<em>(unverified)</em>" if not payment.verified else ""}</td>
             <td class="amt">{esc(payment.amount)}</td></tr>"""
        for payment in receipt.payments
    )

    change = (
        f"""<tr class="change"><td colspan="3">Change due</td>
             <td class="amt">{esc(receipt.change_due)}</td></tr>"""
        if not receipt.change_due.is_zero
        else ""
    )

    banner = ""
    if receipt.is_reprint:
        banner += '<p class="banner">REPRINT</p>'
    if receipt.requires_review:
        banner += '<p class="banner review">HELD FOR REVIEW</p>'

    return f"""<section class="receipt">
  <header>
    <h2>{esc(receipt.store_name)}</h2>
    {f"<p class='gstin'>GSTIN {esc(receipt.store_gstin)}</p>" if receipt.store_gstin else ""}
    <p class="meta">{esc(receipt.receipt_no)} &middot;
       {esc(receipt.issued_at.strftime("%d-%m-%Y %H:%M"))}</p>
    <p class="meta">Till {esc(receipt.terminal_code)} &middot;
       {esc(receipt.cashier_name)}</p>
    {banner}
  </header>
  <table>
    <tbody>{lines}</tbody>
    <tfoot>
      <tr class="sub"><td colspan="3">Subtotal</td>
          <td class="amt">{esc(receipt.subtotal)}</td></tr>
      {tax_rows}
      {rounding_row}
      <tr class="total"><td colspan="3">TOTAL</td>
          <td class="amt">{esc(receipt.grand_total)}</td></tr>
      {payments}
      {change}
    </tfoot>
  </table>
  <footer><p>{receipt.item_count} item(s) &middot; Thank you</p></footer>
</section>"""


# ── PDF ─────────────────────────────────────────────────────────────────────


class PdfUnavailable(RuntimeError):
    """reportlab is not installed in this build."""


def render_pdf(receipt: Receipt, destination: Path) -> Path:
    """Write the receipt as a PDF and return the path.

    Drawn from the same document model as the screen rendering rather than by
    converting HTML, so there is no headless browser in the bundle and no
    second layout engine to disagree with the first.
    """
    try:
        from reportlab.lib.pagesizes import mm
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - depends on the build
        raise PdfUnavailable(
            "reportlab is not installed; PDF receipts are unavailable"
        ) from exc

    text = render_text(receipt)
    rows = text.splitlines()

    # A tall, narrow page: an 80mm roll, sized to the content so nothing is
    # cut off and there is no page break mid-receipt.
    width = 80 * mm
    line_height = 3.6 * mm
    height = (len(rows) + 6) * line_height

    destination.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(destination), pagesize=(width, height))
    pdf.setTitle(f"Receipt {receipt.receipt_no}")

    pdf.setFont("Courier", 8)
    y = height - (3 * line_height)
    for row in rows:
        pdf.drawString(4 * mm, y, row)
        y -= line_height

    pdf.showPage()
    pdf.save()
    return destination


def receipt_path(data_dir: Path, receipt: Receipt) -> Path:
    """Where a receipt PDF lives.

    Under the data directory, not the install directory, so an update never
    deletes a customer's invoice (architecture §14).
    """
    return data_dir / "receipts" / f"{receipt.receipt_no}.pdf"


def totals_line(receipt: Receipt) -> str:
    """One-line summary for the log."""
    return (
        f"{receipt.receipt_no} {receipt.grand_total} "
        f"({receipt.item_count} lines, rounding {receipt.rounding_adjustment})"
    )

import { useEffect, useState } from "react";
import { ApiError } from "../../core/api/client";
import { shifts } from "../../core/api/register";
import type {
  CloseShiftResponse,
  SessionResponse,
  XReportResponse,
} from "../../core/api/contract";
import { useScanShield } from "../../core/barcode-capture/useScanShield";

/**
 * Closing the day — phase 8 slice 2b.
 *
 * **Count first; expected only after the commit.** Before the count this
 * screen shows how many sales there were and which are under review, and no
 * cash figure at all: a supervisor who can see float plus cash sales can add
 * them up, and then types the sum instead of counting. The expected figure,
 * the variance and the rounding arrive with the committed close, together
 * with the Z-report that was stored — there is no way back from here to a
 * recount, which is the point.
 */
export function CloseShiftScreen({
  session,
  onClose,
}: {
  session: SessionResponse;
  onClose: () => void;
}) {
  const [before, setBefore] = useState<XReportResponse | null>(null);
  const [closed, setClosed] = useState<CloseShiftResponse | null>(null);
  const [rupees, setRupees] = useState("");
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pdfPath, setPdfPath] = useState<string | null>(null);

  const shield = useScanShield({
    onScanBlocked: () => {
      setRupees("");
      setProblem("That was a scan. Type what you counted.");
    },
  });

  useEffect(() => {
    const failed = (error: unknown) =>
      setProblem(error instanceof ApiError ? error.message : "Could not read the shift");
    shifts.x().then(setBefore).catch(failed);
  }, []);

  const paise = Math.round(Number(rupees) * 100);
  const ready = rupees.trim().length > 0 && Number.isFinite(paise) && paise >= 0;

  async function commit(event: React.FormEvent) {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      setClosed(await shifts.close(paise, note.trim() || null));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : "Could not close the shift");
    } finally {
      setBusy(false);
    }
  }

  async function writePdf() {
    if (!closed) return;
    try {
      setPdfPath((await shifts.writeZPdf(closed.shift.id)).path);
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : "Could not write the PDF");
    }
  }

  if (closed) {
    const f = closed.figures;
    return (
      <main className="card close-shift">
        <h2>Shift closed</h2>
        {/* Variance and rounding on separate lines: rounding is already inside
            cash sales and is never why a drawer is short. */}
        <dl className="figures">
          <dt>Expected in drawer</dt>
          <dd>{f.expected_cash?.text}</dd>
          <dt>Counted</dt>
          <dd>{f.counted_cash?.text}</dd>
          <dt>Variance</dt>
          <dd className="variance">{f.variance?.text}</dd>
          <dt>Rounding (inside cash sales)</dt>
          <dd>{f.rounding.text}</dd>
          <dt>UPI attested</dt>
          <dd>{f.upi_attested.text}</dd>
          <dt>UPI verified</dt>
          <dd>{f.upi_verified.text}</dd>
        </dl>
        <div
          className="receipt-view"
          // Rendered by the service from the stored close, escaped there.
          dangerouslySetInnerHTML={{ __html: closed.zreport_html }}
        />
        {problem && <p className="msg error" role="alert">{problem}</p>}
        {pdfPath && <p className="msg">Saved {pdfPath}</p>}
        <div className="row">
          <button type="button" className="secondary" onClick={() => void writePdf()}>
            Save PDF
          </button>
          <button type="button" onClick={onClose}>
            Done
          </button>
        </div>
      </main>
    );
  }

  const receipts = before?.figures.under_review_receipts ?? [];
  return (
    <main className="card close-shift">
      <h2>Close the day</h2>
      <p className="muted">
        {session.full_name}, count the drawer before anything else.{" "}
        {before ? `${before.figures.sales_count} sales on this shift.` : ""}
      </p>
      {receipts.length > 0 && (
        <div className="under-review">
          <p>Still under review — in no total, chase each one:</p>
          <ul>
            {receipts.map((receipt) => (
              <li key={receipt}>{receipt}</li>
            ))}
          </ul>
        </div>
      )}
      <form onSubmit={commit}>
        <label htmlFor="counted-cash">Cash counted in the drawer</label>
        <input
          id="counted-cash"
          inputMode="decimal"
          value={rupees}
          onChange={(event) => setRupees(event.target.value)}
          onKeyDown={shield.onKeyDown}
          placeholder="in rupees"
          autoFocus
          autoComplete="off"
        />
        <label htmlFor="close-note">Note (optional)</label>
        <input
          id="close-note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          maxLength={500}
          autoComplete="off"
        />
        {problem && <p className="msg error" role="alert">{problem}</p>}
        <div className="row">
          <button type="button" className="secondary" onClick={onClose}>
            Back to the register
          </button>
          <button type="submit" disabled={!ready || busy}>
            {busy ? "Closing…" : "Close shift"}
          </button>
        </div>
      </form>
    </main>
  );
}

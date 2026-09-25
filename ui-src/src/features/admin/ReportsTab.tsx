import { useCallback, useEffect, useState } from "react";
import type {
  ProductReportResponse,
  ProductSalesOut,
  SalesDayOut,
  SalesReportResponse,
  SessionResponse,
  StockPositionOut,
  StockReportResponse,
} from "../../core/api/contract";
import { reports } from "../../core/api/reports";
import { useHasPermission } from "../../core/rbac/PermissionGate";
import { useCloudCall } from "./useCloudCall";

/**
 * The owner's reports — phase 8 slice 4.
 *
 * What the shop did, as opposed to what this till did: the Z-report is the
 * till's document and prints offline; these are read from the cloud, dated
 * by when the cloud received each sale, and need the internet. The tab says
 * so rather than showing an empty table that looks like a quiet week.
 *
 * **The cost columns are `report.margin`'s.** A supervisor holds
 * `report.sales.store` and sees takings; a manager sees what they cost. The
 * database is what withholds cost (0026) and the till withholds it again
 * (`app/api/reports.py`); hiding the columns here is so a supervisor is not
 * shown a Margin heading over a column of blanks.
 *
 * **Export writes a file on the till** — CSV, which Excel opens — and says
 * where. The file is fetched again by the till rather than built from this
 * page, so what it contains is decided by the session and not by the screen.
 */

type View = "sales" | "products" | "stock";

const VIEWS: { id: View; label: string }[] = [
  { id: "sales", label: "Sales by day" },
  { id: "products", label: "Products" },
  { id: "stock", label: "Stock" },
];

const rupees = (paise: number) => (paise / 100).toFixed(2);
const units = (milli: number) => String(milli / 1000);

/** `YYYY-MM-DD` for this machine's today — the day the owner means. */
export function localToday(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** The zone the till is in, so the cloud draws the day where the shop does. */
function localZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Kolkata";
}

/** A figure, or the honest reason there is none — never a blank cell. */
function Money({ paise, missing = "—" }: { paise?: number | null; missing?: string }) {
  if (paise === null || paise === undefined) return <span className="muted">{missing}</span>;
  return <>{rupees(paise)}</>;
}

export function ReportsTab({ session }: { session: SessionResponse }) {
  const has = useHasPermission(session);
  const margin = has("report.margin");
  const today = localToday();

  const [view, setView] = useState<View>("sales");
  const [since, setSince] = useState(today);
  const [until, setUntil] = useState(today);
  const [sales, setSales] = useState<SalesReportResponse | null>(null);
  const [products, setProducts] = useState<ProductReportResponse | null>(null);
  const [stock, setStock] = useState<StockReportResponse | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const { busy, error, offline, run } = useCloudCall();

  const load = useCallback(async () => {
    setSaved(null);
    const range = { since, until, tz: localZone() };
    if (view === "sales") {
      const body = await run(() => reports.sales(range));
      if (body) setSales(body);
    } else if (view === "products") {
      const body = await run(() => reports.products(range));
      if (body) setProducts(body);
    } else {
      const body = await run(() => reports.stock());
      if (body) setStock(body);
    }
  }, [run, view, since, until]);

  useEffect(() => {
    if (since && until) void load();
  }, [load, since, until]);

  const exportCsv = async () => {
    const written = await run(() =>
      reports.exportCsv({ report: view, since, until, tz: localZone() }),
    );
    if (written) setSaved(written.path);
  };

  return (
    <section className="pane reports">
      <nav className="tabs" aria-label="Report">
        {VIEWS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className={entry.id === view ? "tab on" : "tab"}
            onClick={() => setView(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      <div className="filters">
        {view !== "stock" ? (
          <>
            <label htmlFor="report-from">From</label>
            <input
              id="report-from"
              type="date"
              value={since}
              max={until}
              onChange={(event) => setSince(event.target.value)}
            />
            <label htmlFor="report-to">To</label>
            <input
              id="report-to"
              type="date"
              value={until}
              min={since}
              onChange={(event) => setUntil(event.target.value)}
            />
          </>
        ) : (
          <span className="muted">What is on the shelves now.</span>
        )}
        <button type="button" disabled={busy} onClick={() => void exportCsv()}>
          Export to CSV
        </button>
      </div>

      {offline && (
        <p className="msg error">
          Reports are read from the cloud and need the internet. The till keeps
          selling without it, and the Z-report still prints at the close.
        </p>
      )}
      {error && <p className="msg error">{error}</p>}
      {saved && (
        <p className="msg">
          Saved {saved} — it opens in Excel without the till.
        </p>
      )}

      {view === "sales" && sales && <SalesTable report={sales} margin={margin} />}
      {view === "products" && products && (
        <ProductsTable report={products} margin={margin} />
      )}
      {view === "stock" && stock && <StockTable report={stock} margin={margin} />}
    </section>
  );
}

// ── Sales by day ──────────────────────────────────────────────────────────

function SalesTable({ report, margin }: { report: SalesReportResponse; margin: boolean }) {
  const row = (day: SalesDayOut, total = false) => (
    <tr key={day.day ?? "total"} className={total ? "total" : undefined}>
      <td>{day.day ?? "Total"}</td>
      <td className="num">{day.sales_count}</td>
      <td className="num">{rupees(day.takings)}</td>
      <td className="num">{rupees(day.cash)}</td>
      <td className="num">{rupees(day.upi_attested)}</td>
      <td className="num">{rupees(day.upi_verified)}</td>
      <td className="num">{rupees(day.tax)}</td>
      <td className="num">{rupees(day.rounding)}</td>
      <td className="num">
        {day.under_review_count > 0 ? (
          <>
            {day.under_review_count} ({rupees(day.under_review_total)})
          </>
        ) : (
          <span className="muted">none</span>
        )}
      </td>
      {margin && (
        <>
          <td className="num">
            <Money paise={day.cost} />
          </td>
          <td className="num">
            <Money paise={day.margin} />
          </td>
          <td className="num">
            <Money paise={day.uncosted_sales} />
          </td>
        </>
      )}
    </tr>
  );

  return (
    <>
      <p className="note muted">
        Takings are what was paid for the sales that count — the same figure
        as the Z-reports' Takings line for these days. Rounding is already
        inside cash. Sales under review are beside the takings, never in them.
      </p>
      <table className="grid">
        <thead>
          <tr>
            <th>Day</th>
            <th className="num">Sales</th>
            <th className="num">Takings</th>
            <th className="num">Cash</th>
            <th className="num">UPI attested</th>
            <th className="num">UPI verified</th>
            <th className="num">GST</th>
            <th className="num">Rounding</th>
            <th className="num">Under review</th>
            {margin && (
              <>
                <th className="num">Cost</th>
                <th className="num">Margin</th>
                <th className="num">Sold with no cost</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>{report.days.map((day) => row(day))}</tbody>
        <tfoot>{row(report.total, true)}</tfoot>
      </table>
      {margin && <MarginNote uncosted={report.total.uncosted_sales} />}
    </>
  );
}

/**
 * Most of the catalogue came across with no cost, and the unlisted item
 * never has one. A margin over part of the takings is still worth reading
 * as long as it says which part.
 */
function MarginNote({ uncosted }: { uncosted?: number | null }) {
  if (!uncosted) return null;
  return (
    <p className="note muted">
      Margin covers only what has a cost recorded. {rupees(uncosted)} was sold
      with no cost on file and is not in the margin.
    </p>
  );
}

// ── Products ──────────────────────────────────────────────────────────────

function ProductsTable({
  report,
  margin,
}: {
  report: ProductReportResponse;
  margin: boolean;
}) {
  if (report.rows.length === 0) {
    return <p className="muted">Nothing sold in these days.</p>;
  }
  // A product sold with no cost at all has no margin, not a margin of zero.
  const costed = (row: ProductSalesOut) => row.uncosted_sales !== row.sales;
  const percent = (row: ProductSalesOut) =>
    row.margin_bp === null || row.margin_bp === undefined
      ? null
      : (row.margin_bp / 100).toFixed(2);

  return (
    <>
      <table className="grid">
        <thead>
          <tr>
            <th>SKU</th>
            <th>Product</th>
            <th className="num">Quantity</th>
            <th className="num">Sold for</th>
            <th className="num">GST</th>
            {margin && (
              <>
                <th className="num">Cost</th>
                <th className="num">Margin</th>
                <th className="num">Margin %</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {report.rows.map((row) => (
            <tr key={row.product_id ?? row.sku}>
              <td>{row.sku}</td>
              <td>{row.name}</td>
              <td className="num">
                {row.qty_milli !== null && row.qty_milli !== undefined
                  ? `${units(row.qty_milli)} ${row.uom ?? ""}`
                  : ""}
              </td>
              <td className="num">{rupees(row.sales)}</td>
              <td className="num">{rupees(row.tax)}</td>
              {margin && (
                <>
                  <td className="num">
                    <Money paise={costed(row) ? row.cost : null} missing="no cost" />
                  </td>
                  <td className="num">
                    <Money paise={costed(row) ? row.margin : null} />
                  </td>
                  <td className="num">{percent(row) ?? <span className="muted">—</span>}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="total">
            <td>Total</td>
            <td />
            <td />
            <td className="num">{rupees(report.total.sales)}</td>
            <td className="num">{rupees(report.total.tax)}</td>
            {margin && (
              <>
                <td className="num">
                  <Money paise={report.total.cost} />
                </td>
                <td className="num">
                  <Money paise={report.total.margin} />
                </td>
                <td className="num">{percent(report.total) ?? ""}</td>
              </>
            )}
          </tr>
        </tfoot>
      </table>
      {margin && <MarginNote uncosted={report.total.uncosted_sales} />}
    </>
  );
}

// ── Stock ─────────────────────────────────────────────────────────────────

function StockTable({ report, margin }: { report: StockReportResponse; margin: boolean }) {
  if (report.rows.length === 0) {
    return (
      <p className="muted">
        No stock recorded yet. A product appears here once it has been
        received or counted.
      </p>
    );
  }
  const row = (item: StockPositionOut) => (
    <tr key={item.product_id ?? item.sku}>
      <td>{item.sku}</td>
      <td>{item.name}</td>
      <td className="num">
        {item.on_hand !== null && item.on_hand !== undefined
          ? `${units(item.on_hand)} ${item.uom ?? ""}`
          : ""}
      </td>
      <td className="num">
        <Money paise={item.price} missing="no price" />
      </td>
      <td className="num">
        <Money paise={item.value_at_price} />
      </td>
      {margin && (
        <>
          <td className="num">
            <Money paise={item.unit_cost} missing="no cost" />
          </td>
          <td className="num">
            <Money paise={item.value_at_cost} />
          </td>
        </>
      )}
    </tr>
  );

  return (
    <table className="grid">
      <thead>
        <tr>
          <th>SKU</th>
          <th>Product</th>
          <th className="num">On hand</th>
          <th className="num">Price</th>
          <th className="num">Value at price</th>
          {margin && (
            <>
              <th className="num">Unit cost</th>
              <th className="num">Value at cost</th>
            </>
          )}
        </tr>
      </thead>
      <tbody>{report.rows.map(row)}</tbody>
      <tfoot>
        <tr className="total">
          <td>Total</td>
          <td />
          <td />
          <td />
          <td className="num">
            <Money paise={report.total.value_at_price} />
          </td>
          {margin && (
            <>
              <td />
              <td className="num">
                <Money paise={report.total.value_at_cost} />
              </td>
            </>
          )}
        </tr>
      </tfoot>
    </table>
  );
}

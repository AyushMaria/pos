import { useCallback, useEffect, useState } from "react";
import { admin } from "../../core/api/admin";
import { ApiError } from "../../core/api/client";
import { catalog } from "../../core/api/register";
import type {
  AdminBarcodeOut,
  AdminProductOut,
  LowStockOut,
  Permission,
  SessionResponse,
  TaxCodeOut,
  UnknownScanOut,
} from "../../core/api/contract";

/**
 * Catalogue admin — phase 6 slice 6.
 *
 * The only screen in this application that stops working without the
 * internet, and it says so rather than looking broken. Products are pull-only
 * on a terminal; editing them is a cloud write by decision 2 of the phase 6
 * plan, and the line that decision draws is: selling something unrecognised
 * works offline, cataloguing it properly does not.
 *
 * **Money is paise below this file and rupees on it.** The conversion happens
 * at this boundary and nowhere else, the same way the stockroom converts
 * thousandths to things you can count.
 */

const TABS: { id: Tab; label: string; permission: Permission }[] = [
  { id: "catalogue", label: "Catalogue", permission: "product.read" },
  { id: "queue", label: "Unknown scans", permission: "product.edit" },
  { id: "low", label: "Low stock", permission: "product.read" },
];

type Tab = "catalogue" | "queue" | "low";

const rupees = (paise: number) => (paise / 100).toFixed(2);
const paise = (typed: string) => Math.round(Number(typed) * 100);

/** Offline is a different sentence from refused, and a different next step. */
function useCloudCall() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);

  const run = useCallback(async <T,>(work: () => Promise<T>): Promise<T | null> => {
    setBusy(true);
    setError(null);
    setOffline(false);
    try {
      return await work();
    } catch (cause) {
      if (cause instanceof ApiError && cause.isUnavailable) {
        setOffline(true);
      } else {
        setError(cause instanceof ApiError ? cause.message : "That did not work.");
      }
      return null;
    } finally {
      setBusy(false);
    }
  }, []);

  return { busy, error, offline, run, clearError: () => setError(null) };
}

function CloudNotice({ offline, error }: { offline: boolean; error: string | null }) {
  if (offline) {
    return (
      <p className="msg error">
        Editing the catalogue needs the internet. The till keeps selling
        without it — this screen does not.
      </p>
    );
  }
  return error ? <p className="msg error">{error}</p> : null;
}

export function AdminScreen({
  session,
  onClose,
}: {
  session: SessionResponse;
  onClose: () => void;
}) {
  const allowed = TABS.filter((entry) =>
    session.permissions.includes(entry.permission),
  );
  const [tab, setTab] = useState<Tab>(allowed[0]?.id ?? "catalogue");

  if (allowed.length === 0) {
    return (
      <div className="register admin">
        <header className="bar">
          <button type="button" className="link" onClick={onClose}>
            ← Register
          </button>
          <span className="till">{session.employee_code}</span>
        </header>
        <p className="empty">
          Editing the catalogue needs a permission this account does not have.
        </p>
      </div>
    );
  }

  return (
    <div className="register admin">
      <header className="bar">
        <button type="button" className="link" onClick={onClose}>
          ← Register
        </button>
        <nav className="tabs">
          {allowed.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className={entry.id === tab ? "tab on" : "tab"}
              onClick={() => setTab(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </nav>
        <span className="till">{session.employee_code}</span>
      </header>

      {tab === "catalogue" && <CatalogueTab session={session} />}
      {tab === "queue" && <QueueTab />}
      {tab === "low" && <LowStockTab />}
    </div>
  );
}

// ── Catalogue ─────────────────────────────────────────────────────────────

function CatalogueTab({ session }: { session: SessionResponse }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<AdminProductOut[]>([]);
  const [chosen, setChosen] = useState<AdminProductOut | null>(null);
  const { busy, error, offline, run } = useCloudCall();

  const search = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    const found = await run(() => admin.searchProducts(query.trim()));
    if (found) {
      setResults(found.products);
      setChosen(null);
    }
  };

  return (
    <section className="pane">
      <form onSubmit={search} className="row">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Name or SKU"
          aria-label="Search the catalogue"
        />
        <button type="submit" disabled={busy || !query.trim()}>
          Search
        </button>
      </form>

      <CloudNotice offline={offline} error={error} />

      {!chosen && (
        <ul className="list">
          {results.map((product) => (
            <li key={product.product_id}>
              <button type="button" className="link" onClick={() => setChosen(product)}>
                <strong>{product.sku}</strong> {product.name}
              </button>
            </li>
          ))}
          {results.length === 0 && !busy && (
            <li className="muted">
              Nothing yet. Search by name or SKU — 38.6% of this catalogue has
              no barcode, so the name is often the only way in.
            </li>
          )}
        </ul>
      )}

      {chosen && (
        <ProductEditor
          product={chosen}
          session={session}
          onDone={(updated) => {
            setChosen(updated);
            setResults((rows) =>
              rows.map((row) =>
                row.product_id === updated.product_id ? updated : row,
              ),
            );
          }}
          onBack={() => setChosen(null)}
        />
      )}
    </section>
  );
}

function ProductEditor({
  product,
  session,
  onDone,
  onBack,
}: {
  product: AdminProductOut;
  session: SessionResponse;
  onDone: (product: AdminProductOut) => void;
  onBack: () => void;
}) {
  const [name, setName] = useState(product.name);
  const [shortName, setShortName] = useState(product.short_name ?? "");
  const [taxCode, setTaxCode] = useState(product.tax_code);
  const [isWeighed, setIsWeighed] = useState(product.is_weighed);
  const [tracksStock, setTracksStock] = useState(product.track_stock);
  const [rates, setRates] = useState<TaxCodeOut[]>([]);
  const { busy, error, offline, run } = useCloudCall();
  const mayEdit = session.permissions.includes("product.edit");

  useEffect(() => {
    catalog.taxCodes().then((body) => setRates(body.tax_codes)).catch(() => undefined);
  }, []);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const saved = await run(() =>
      admin.updateProduct(product.product_id, {
        name,
        short_name: shortName || null,
        tax_code: taxCode,
        is_weighed: isWeighed,
        track_stock: tracksStock,
      }),
    );
    if (saved) onDone(saved);
  };

  return (
    <div className="editor">
      <button type="button" className="link" onClick={onBack}>
        ← All results
      </button>
      <h2>
        {product.sku} <span className="muted">{product.uom}</span>
      </h2>

      <form onSubmit={save} className="form">
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} disabled={!mayEdit} />
        </label>
        <label>
          Short name <span className="muted">(what fits on a receipt)</span>
          <input
            value={shortName}
            onChange={(e) => setShortName(e.target.value)}
            disabled={!mayEdit}
          />
        </label>
        <label>
          Tax code
          <select
            value={taxCode}
            onChange={(e) => setTaxCode(e.target.value)}
            disabled={!mayEdit}
          >
            {rates.map((rate) => (
              <option key={rate.code} value={rate.code}>
                {rate.code} — {rate.name}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={isWeighed}
            onChange={(e) => setIsWeighed(e.target.checked)}
            disabled={!mayEdit}
          />
          Sold by weight
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={tracksStock}
            onChange={(e) => setTracksStock(e.target.checked)}
            disabled={!mayEdit}
          />
          Track stock
          <span className="note muted">
            Off means sales write no ledger row — correct for anything loose.
          </span>
        </label>
        <button type="submit" disabled={busy || !mayEdit}>
          Save
        </button>
      </form>

      <CloudNotice offline={offline} error={error} />

      <BarcodeEditor productId={product.product_id} mayEdit={mayEdit} />
      <PriceEditor productId={product.product_id} mayEdit={mayEdit} />
    </div>
  );
}

function BarcodeEditor({ productId, mayEdit }: { productId: string; mayEdit: boolean }) {
  const [codes, setCodes] = useState<AdminBarcodeOut[]>([]);
  const [typed, setTyped] = useState("");
  const { busy, error, offline, run } = useCloudCall();

  const load = useCallback(async () => {
    const body = await run(() => admin.barcodes(productId));
    if (body) setCodes(body.barcodes);
  }, [productId, run]);

  useEffect(() => {
    void load();
  }, [load]);

  const add = async (event: React.FormEvent) => {
    event.preventDefault();
    const created = await run(() => admin.addBarcode(productId, { barcode: typed.trim() }));
    if (created) {
      setTyped("");
      void load();
    }
  };

  return (
    <section className="sub">
      <h3>Barcodes</h3>
      <ul className="list">
        {codes.map((code) => (
          <li key={code.barcode_id}>
            <code>{code.barcode}</code>{" "}
            <span className="muted">
              {code.symbology}
              {code.pack_size > 1 ? ` · pack of ${code.pack_size}` : ""}
            </span>
            {mayEdit && (
              <button
                type="button"
                className="link danger"
                onClick={async () => {
                  await run(() => admin.withdrawBarcode(code.barcode_id));
                  void load();
                }}
              >
                Remove
              </button>
            )}
          </li>
        ))}
        {codes.length === 0 && !busy && (
          <li className="muted">No codes. This product is found by name.</li>
        )}
      </ul>

      {mayEdit && (
        <form onSubmit={add} className="row">
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            placeholder="Scan or type a code"
            aria-label="Add a barcode"
          />
          <button type="submit" disabled={busy || !typed.trim()}>
            Add
          </button>
        </form>
      )}

      {/* A duplicate arrives here already phrased as a sentence naming the
          product that holds the code — the router does that, because
          "duplicate key value violates unique constraint" is not something
          anyone can act on. */}
      <CloudNotice offline={offline} error={error} />
    </section>
  );
}

function PriceEditor({ productId, mayEdit }: { productId: string; mayEdit: boolean }) {
  const [current, setCurrent] = useState<number | null>(null);
  const [typed, setTyped] = useState("");
  const { busy, error, offline, run } = useCloudCall();

  const load = useCallback(async () => {
    const body = await run(() => admin.prices(productId));
    if (body) {
      const open = body.prices.find((price) => price.valid_to === null);
      setCurrent(open ? open.price : null);
    }
  }, [productId, run]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const value = paise(typed);
    if (!Number.isFinite(value) || value < 0) return;
    const saved = await run(() => admin.setPrice(productId, { price: value }));
    if (saved) {
      setTyped("");
      setCurrent(saved.price);
    }
  };

  return (
    <section className="sub">
      <h3>Price</h3>
      <p>
        {current === null ? (
          <span className="muted">
            No price. A product with no price cannot be sold.
          </span>
        ) : (
          <strong>₹{rupees(current)}</strong>
        )}
      </p>

      {mayEdit && (
        <form onSubmit={save} className="row">
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            inputMode="decimal"
            placeholder="New price in ₹"
            aria-label="New price in rupees"
          />
          <button type="submit" disabled={busy || Number(typed) <= 0}>
            Set
          </button>
        </form>
      )}

      <CloudNotice offline={offline} error={error} />
    </section>
  );
}

// ── The unknown-scan queue ────────────────────────────────────────────────

function QueueTab() {
  const [scans, setScans] = useState<UnknownScanOut[]>([]);
  const { busy, error, offline, run } = useCloudCall();

  const load = useCallback(async () => {
    const body = await run(() => admin.unknownScans(false));
    if (body) setScans(body.scans);
  }, [run]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="pane">
      <p className="note muted">
        Codes the till could not resolve. Catalogue the item, then close the
        entry. Closing one changes nothing about what was already sold — a sold
        line records what was charged.
      </p>

      <CloudNotice offline={offline} error={error} />

      <ul className="list">
        {scans.map((scan) => (
          <li key={scan.scan_id}>
            <code>{scan.barcode}</code>{" "}
            <span className="muted">{new Date(scan.scanned_at).toLocaleString()}</span>
            <button
              type="button"
              className="link"
              disabled={busy}
              onClick={async () => {
                await run(() => admin.resolveScan(scan.scan_id));
                void load();
              }}
            >
              Done
            </button>
          </li>
        ))}
        {scans.length === 0 && !busy && !offline && (
          <li className="muted">Nothing waiting. Every scan found a product.</li>
        )}
      </ul>
    </section>
  );
}

// ── Low stock ─────────────────────────────────────────────────────────────

function LowStockTab() {
  const [rows, setRows] = useState<LowStockOut[]>([]);
  const { busy, error, offline, run } = useCloudCall();

  useEffect(() => {
    void (async () => {
      const body = await run(() => admin.lowStock());
      if (body) setRows(body.rows);
    })();
  }, [run]);

  return (
    <section className="pane">
      <CloudNotice offline={offline} error={error} />
      <table className="grid">
        <thead>
          <tr>
            <th>SKU</th>
            <th>Product</th>
            <th className="num">On hand</th>
            <th className="num">Reorder at</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.product_id}>
              <td>{row.sku}</td>
              <td>{row.name}</td>
              <td className="num">
                {row.on_hand} {row.uom}
              </td>
              <td className="num">{row.reorder_point}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && !busy && !offline && (
        <p className="muted">
          Nothing at its reorder point. Only products with a reorder point set
          appear here.
        </p>
      )}
    </section>
  );
}

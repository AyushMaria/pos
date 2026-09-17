import { useCallback, useEffect, useState } from "react";
import { admin } from "../../core/api/admin";
import { ApiError } from "../../core/api/client";
import { catalog } from "../../core/api/register";
import type {
  AdminBarcodeOut,
  AdminProductOut,
  LowStockOut,
  Permission,
  ProductCreateRequest,
  SessionResponse,
  StockLevelOut,
  TaxCodeOut,
  UnknownScanOut,
} from "../../core/api/contract";
import { PermissionGate, useHasPermission } from "../../core/rbac/PermissionGate";

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

/**
 * Quantities are thousandths below this screen and things you can count on
 * it — the same boundary the stockroom owns, for the same reason: nobody
 * counts in millipackets.
 */
const UNIT = 1000;
const units = (milli: number) => String(milli / UNIT);
const milli = (typed: string) => Math.round(Number(typed) * UNIT);

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

/**
 * The product form saves on a button; the barcode, price and reorder panels
 * below it save on theirs. That difference is invisible, and the first person
 * to use this screen ticked "sold by weight", walked away, and lost it — the
 * edit had never left the browser. Leaving is now a question rather than a
 * silent discard.
 */
function UnsavedChanges({
  onDiscard,
  onStay,
}: {
  onDiscard: () => void;
  onStay: () => void;
}) {
  return (
    <p className="msg warn" role="alert">
      You have unsaved changes to this product.{" "}
      <button type="button" className="link" onClick={onStay}>
        Keep editing
      </button>{" "}
      <button type="button" className="link danger" onClick={onDiscard}>
        Discard them
      </button>
    </p>
  );
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
  const has = useHasPermission(session);
  const allowed = TABS.filter((entry) => has(entry.permission));
  const [tab, setTab] = useState<Tab>(allowed[0]?.id ?? "catalogue");
  // Raised by the product editor. A tab is a navigation like any other, and
  // it discarded edits as quietly as the back link did.
  const [unsaved, setUnsaved] = useState(false);
  const [pending, setPending] = useState<Tab | null>(null);

  const goTo = (next: Tab) => {
    if (unsaved && next !== tab) setPending(next);
    else setTab(next);
  };

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
              onClick={() => goTo(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </nav>
        <span className="till">{session.employee_code}</span>
      </header>

      {pending && (
        <UnsavedChanges
          onDiscard={() => {
            setUnsaved(false);
            setTab(pending);
            setPending(null);
          }}
          onStay={() => setPending(null)}
        />
      )}

      {tab === "catalogue" && (
        <CatalogueTab session={session} onDirtyChange={setUnsaved} />
      )}
      {tab === "queue" && <QueueTab session={session} />}
      {tab === "low" && <LowStockTab />}
    </div>
  );
}

// ── Catalogue ─────────────────────────────────────────────────────────────

/**
 * Search and pick a product.
 *
 * Extracted because the unknown-scan queue needs exactly this to answer the
 * other half of its question: a code the till did not recognise is either a
 * product you have not catalogued yet, or one you have and forgot to give
 * this code to. Requirement 3 of the slice says *either*, so both paths have
 * to exist and the search is common to one of them.
 */
function ProductSearch({
  onPick,
  label = "Search the catalogue",
}: {
  onPick: (product: AdminProductOut) => void;
  label?: string;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<AdminProductOut[]>([]);
  const { busy, error, offline, run } = useCloudCall();

  const search = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    const found = await run(() => admin.searchProducts(query.trim()));
    if (found) setResults(found.products);
  };

  return (
    <>
      <form onSubmit={search} className="row">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Name or SKU"
          aria-label={label}
        />
        <button type="submit" disabled={busy || !query.trim()}>
          Search
        </button>
      </form>

      <CloudNotice offline={offline} error={error} />

      <ul className="list">
        {results.map((product) => (
          <li key={product.product_id}>
            <button type="button" className="link" onClick={() => onPick(product)}>
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
    </>
  );
}

function CatalogueTab({
  session,
  onDirtyChange,
}: {
  session: SessionResponse;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const [chosen, setChosen] = useState<AdminProductOut | null>(null);
  const [creating, setCreating] = useState(false);
  const mayCreate = useHasPermission(session)("product.create");

  if (creating) {
    return (
      <section className="pane">
        <ProductCreator
          onCreated={(product) => {
            setCreating(false);
            // Straight into the editor rather than back to a list: a product
            // with no barcode and no price cannot be sold, and those are the
            // next two things this person has to do.
            setChosen(product);
          }}
          onCancel={() => setCreating(false)}
        />
      </section>
    );
  }

  if (chosen) {
    return (
      <section className="pane">
        <ProductEditor
          product={chosen}
          session={session}
          onDirtyChange={onDirtyChange}
          onDone={setChosen}
          onBack={() => setChosen(null)}
        />
      </section>
    );
  }

  return (
    <section className="pane">
      {mayCreate && (
        <p className="row">
          <button type="button" onClick={() => setCreating(true)}>
            New product
          </button>
        </p>
      )}
      <ProductSearch onPick={setChosen} />
    </section>
  );
}

/**
 * Create a product.
 *
 * `product.create` is a separate permission from `product.edit`, and this is
 * the only thing that needs it — so the button is gated on it rather than on
 * the tab.
 *
 * SKU is the one field with no sensible default. Everything else has one, and
 * the defaults are the common case for a kirana shop: sold each, tracked,
 * not weighed.
 */
function ProductCreator({
  seedName = "",
  onCreated,
  onCancel,
}: {
  seedName?: string;
  onCreated: (product: AdminProductOut) => void;
  onCancel: () => void;
}) {
  const [sku, setSku] = useState("");
  const [name, setName] = useState(seedName);
  const [shortName, setShortName] = useState("");
  const [uom, setUom] = useState("each");
  const [taxCode, setTaxCode] = useState("");
  const [isWeighed, setIsWeighed] = useState(false);
  const [tracksStock, setTracksStock] = useState(true);
  const [rates, setRates] = useState<TaxCodeOut[]>([]);
  const { busy, error, offline, run } = useCloudCall();

  useEffect(() => {
    catalog
      .taxCodes()
      .then((body) => {
        setRates(body.tax_codes);
        setTaxCode((current) => current || body.tax_codes[0]?.code || "");
      })
      .catch(() => undefined);
  }, []);

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    const body: ProductCreateRequest = {
      sku: sku.trim(),
      name: name.trim(),
      short_name: shortName.trim() || null,
      uom,
      tax_code: taxCode,
      is_weighed: isWeighed,
      track_stock: tracksStock,
    };
    const created = await run(() => admin.createProduct(body));
    if (created) onCreated(created);
  };

  const ready = sku.trim() !== "" && name.trim() !== "" && taxCode !== "";

  return (
    <div className="editor">
      <button type="button" className="link" onClick={onCancel}>
        ← Cancel
      </button>
      <h2>New product</h2>

      <form onSubmit={create} className="form">
        <label>
          SKU
          <input
            value={sku}
            onChange={(e) => setSku(e.target.value)}
            aria-label="SKU"
          />
        </label>
        <label>
          Name
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-label="Product name"
          />
        </label>
        <label>
          Short name <span className="muted">(what fits on a receipt)</span>
          <input
            value={shortName}
            onChange={(e) => setShortName(e.target.value)}
            aria-label="Short name"
          />
        </label>
        <label>
          Unit
          <input value={uom} onChange={(e) => setUom(e.target.value)} aria-label="Unit" />
        </label>
        <label>
          Tax code
          <select
            value={taxCode}
            onChange={(e) => setTaxCode(e.target.value)}
            aria-label="Tax code"
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
          />
          Sold by weight
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={tracksStock}
            onChange={(e) => setTracksStock(e.target.checked)}
          />
          Track stock
          <span className="note muted">
            Off means sales write no ledger row — correct for anything loose.
          </span>
        </label>
        <button type="submit" disabled={busy || !ready}>
          Create
        </button>
      </form>

      <p className="note muted">
        A new product has no barcode and no price yet, so it cannot be sold
        until you give it both. That is the next screen.
      </p>

      <CloudNotice offline={offline} error={error} />
    </div>
  );
}

function ProductEditor({
  product,
  session,
  onDone,
  onBack,
  onDirtyChange,
}: {
  product: AdminProductOut;
  session: SessionResponse;
  onDone: (product: AdminProductOut) => void;
  onBack: () => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const [name, setName] = useState(product.name);
  const [shortName, setShortName] = useState(product.short_name ?? "");
  const [taxCode, setTaxCode] = useState(product.tax_code);
  const [isWeighed, setIsWeighed] = useState(product.is_weighed);
  const [tracksStock, setTracksStock] = useState(product.track_stock);
  const [rates, setRates] = useState<TaxCodeOut[]>([]);
  const [leaving, setLeaving] = useState(false);
  const { busy, error, offline, run } = useCloudCall();
  const mayEdit = useHasPermission(session)("product.edit");

  // Compared against the product as the server last returned it, so saving
  // clears this without a second round trip: `onDone` replaces the prop.
  const dirty =
    name !== product.name ||
    shortName !== (product.short_name ?? "") ||
    taxCode !== product.tax_code ||
    isWeighed !== product.is_weighed ||
    tracksStock !== product.track_stock;

  useEffect(() => {
    onDirtyChange(dirty);
    return () => onDirtyChange(false);
  }, [dirty, onDirtyChange]);

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

  const back = () => {
    if (dirty) setLeaving(true);
    else onBack();
  };

  return (
    <div className="editor">
      <button type="button" className="link" onClick={back}>
        ← All results
      </button>

      {leaving && (
        <UnsavedChanges onDiscard={onBack} onStay={() => setLeaving(false)} />
      )}
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
        {/* Disabled until something differs. The button is the only thing on
            this form that says whether the edits are real yet. */}
        <button type="submit" disabled={busy || !mayEdit || !dirty}>
          Save changes
        </button>
      </form>

      <CloudNotice offline={offline} error={error} />

      <BarcodeEditor productId={product.product_id} mayEdit={mayEdit} />
      <PriceEditor productId={product.product_id} mayEdit={mayEdit} />
      <ReorderPointEditor
        productId={product.product_id}
        uom={product.uom}
        tracksStock={tracksStock}
        mayEdit={mayEdit}
      />
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

function ReorderPointEditor({
  productId,
  uom,
  tracksStock,
  mayEdit,
}: {
  productId: string;
  uom: string;
  tracksStock: boolean;
  mayEdit: boolean;
}) {
  const [level, setLevel] = useState<StockLevelOut | null>(null);
  const [untracked, setUntracked] = useState(false);
  const [typed, setTyped] = useState("");
  const { busy, error, offline, run } = useCloudCall();

  const load = useCallback(async () => {
    setUntracked(false);
    try {
      setLevel(await admin.stockLevel(productId));
    } catch (cause) {
      // 404 here is the ordinary state of a product nobody has counted yet,
      // not a failure worth a red message. Anything else is.
      if (cause instanceof ApiError && cause.status === 404) {
        setLevel(null);
        setUntracked(true);
        return;
      }
      await run(() => Promise.reject(cause));
    }
  }, [productId, run]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const saved = await run(() =>
      admin.setReorderPoint(productId, { reorder_point: milli(typed) }),
    );
    if (saved) {
      setTyped("");
      setLevel(saved);
    }
  };

  if (!tracksStock) {
    return (
      <section className="sub">
        <h3>Reorder point</h3>
        <p className="muted">
          This product does not track stock, so there is no level to fall
          below. Loose and weighed items are counted on the shelf, not here.
        </p>
      </section>
    );
  }

  return (
    <section className="sub">
      <h3>Reorder point</h3>
      <p>
        {untracked ? (
          <span className="muted">
            No stock record yet. Count this product in or receive a delivery,
            then set the point it should be reordered at.
          </span>
        ) : level === null ? (
          <span className="muted">Loading…</span>
        ) : level.reorder_point === 0 ? (
          <span className="muted">
            Not set — {units(level.on_hand)} {uom} on hand. Without a point,
            this product never reaches the reorder list.
          </span>
        ) : (
          <>
            <strong>
              {units(level.reorder_point)} {uom}
            </strong>{" "}
            <span className="muted">
              · {units(level.on_hand)} {uom} on hand
            </span>
          </>
        )}
      </p>

      {mayEdit && !untracked && (
        <form onSubmit={save} className="row">
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            inputMode="decimal"
            placeholder={`Reorder at, in ${uom}`}
            aria-label="Reorder point"
          />
          <button type="submit" disabled={busy || typed.trim() === ""}>
            Set reorder point
          </button>
        </form>
      )}

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

/**
 * The unknown-scan queue.
 *
 * Requirement 3 of the slice: a manager takes an entry and *either* matches
 * it to an existing product *or* creates a real one. Both attach the scanned
 * code to a product and then close the entry, which is the only thing that
 * makes the next scan of that packet find something.
 *
 * The third way out is kept deliberately, for the code that was a torn label
 * or somebody's loyalty card — but it used to be a button called "Done",
 * sitting third in a row of three, that closed the entry and catalogued
 * nothing. It was the one people pressed. One code went through it five
 * times and is still not in the catalogue.
 *
 * So it is called Dismiss, it is styled as the lesser option, it asks before
 * it acts, and it reaches a different endpoint that writes a different audit
 * row (0021). None of that is what makes it safe — the database refusing a
 * resolve when the barcode is on nothing is what makes it safe. This is what
 * stops it being pressed by accident in the first place.
 *
 * Closing an entry either way changes nothing about what was already sold —
 * a sold line records what was charged.
 */
function QueueTab({ session }: { session: SessionResponse }) {
  const [scans, setScans] = useState<UnknownScanOut[]>([]);
  const [working, setWorking] = useState<UnknownScanOut | null>(null);
  const [mode, setMode] = useState<"create" | "match" | null>(null);
  // The one entry whose Dismiss has been pressed but not yet confirmed. A
  // scan id rather than a boolean, so the question is asked in the row it is
  // about and a second row cannot inherit the answer.
  const [dismissing, setDismissing] = useState<string | null>(null);
  const { busy, error, offline, run } = useCloudCall();
  const mayCreate = useHasPermission(session)("product.create");

  const load = useCallback(async () => {
    const body = await run(() => admin.unknownScans(false));
    if (body) setScans(body.scans);
  }, [run]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * Attach the scanned code to a product, then close the entry.
   *
   * Order matters and is not reversible: the barcode needs a product to hang
   * on, so a create has to land first. If the code turns out to already be on
   * something else the entry stays open and the new product stays too —
   * recoverable, and the honest signal that "match" was the right answer.
   */
  const attachAndClose = async (scan: UnknownScanOut, productId: string) => {
    const added = await run(() =>
      admin.addBarcode(productId, { barcode: scan.barcode }),
    );
    if (!added) return;
    await run(() => admin.resolveScan(scan.scan_id));
    setWorking(null);
    setMode(null);
    void load();
  };

  if (working && mode === "create") {
    return (
      <section className="pane">
        <p className="note muted">
          Cataloguing <code>{working.barcode}</code>. The code is attached to
          the new product and the entry closed, both at the end.
        </p>
        <ProductCreator
          onCreated={(product) => void attachAndClose(working, product.product_id)}
          onCancel={() => {
            setWorking(null);
            setMode(null);
          }}
        />
        <CloudNotice offline={offline} error={error} />
      </section>
    );
  }

  if (working && mode === "match") {
    return (
      <section className="pane">
        <button
          type="button"
          className="link"
          onClick={() => {
            setWorking(null);
            setMode(null);
          }}
        >
          ← Back to the queue
        </button>
        <p className="note muted">
          Which product does <code>{working.barcode}</code> belong to?
        </p>
        <ProductSearch
          label="Search for the product this code belongs to"
          onPick={(product) => void attachAndClose(working, product.product_id)}
        />
        <CloudNotice offline={offline} error={error} />
      </section>
    );
  }

  return (
    <section className="pane">
      <p className="note muted">
        Codes the till could not resolve. Catalogue the item or point the code
        at a product you already have, and the entry closes itself. Dismiss is
        for a code that is never going to be a product.
      </p>

      <CloudNotice offline={offline} error={error} />

      <ul className="list">
        {scans.map((scan) => (
          <li key={scan.scan_id}>
            <code>{scan.barcode}</code>{" "}
            <span className="muted">{new Date(scan.scanned_at).toLocaleString()}</span>
            {mayCreate && (
              <button
                type="button"
                className="link"
                disabled={busy}
                onClick={() => {
                  setWorking(scan);
                  setMode("create");
                }}
              >
                New product
              </button>
            )}
            {/*
              Both of these close a queue entry, and both reach
              `POST /admin/unknown-scans/{id}/...`, which requires
              `product.edit`. Today that is the same key the Unknown scans tab
              itself needs, so the gate changes nothing — which is the point of
              writing it down rather than relying on it. The tab's permission
              is about navigation and these are about the act; if the tab is
              ever opened up so a cashier can see what is pending, the buttons
              that close an entry must not come with it.
            */}
            <PermissionGate session={session} permission="product.edit">
              <button
                type="button"
                className="link"
                disabled={busy}
                onClick={() => {
                  setWorking(scan);
                  setMode("match");
                }}
              >
                Existing product
              </button>
            </PermissionGate>
            <PermissionGate session={session} permission="product.edit">
              {dismissing === scan.scan_id ? (
              <>
                <span className="muted">
                  Not a product? The code stays on record and what was sold
                  against it does not change — but nothing gets catalogued.
                </span>
                <button
                  type="button"
                  className="link secondary"
                  disabled={busy}
                  onClick={async () => {
                    // Reloaded either way, like the resolve path above it: a
                    // dismissal that was refused left the row open, and the
                    // list should show that rather than the optimistic view.
                    await run(() => admin.dismissScan(scan.scan_id));
                    setDismissing(null);
                    void load();
                  }}
                >
                  Yes, dismiss it
                </button>
                <button
                  type="button"
                  className="link"
                  disabled={busy}
                  onClick={() => setDismissing(null)}
                >
                  Keep it open
                </button>
              </>
              ) : (
                <button
                  type="button"
                  className="link secondary"
                  disabled={busy}
                  onClick={() => setDismissing(scan.scan_id)}
                >
                  Dismiss
                </button>
              )}
            </PermissionGate>
          </li>
        ))}
        {scans.length === 0 && !busy && !offline && (
          // It used to say "Every scan found a product", which the screen had
          // no way of knowing and which was false for every entry closed with
          // the old Done button. An empty queue means nobody is waiting on
          // you; it does not mean the catalogue is complete.
          <li className="muted">
            Nothing waiting. New codes appear here as they are scanned.
          </li>
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
                {units(row.on_hand)} {row.uom}
              </td>
              <td className="num">
                {units(row.reorder_point)} {row.uom}
              </td>
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

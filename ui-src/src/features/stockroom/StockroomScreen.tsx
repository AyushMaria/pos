import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../core/api/client";
import { catalog, inventory } from "../../core/api/register";
import type {
  Permission,
  ProductOut,
  ReceiptLineOut,
  SessionResponse,
} from "../../core/api/contract";

/**
 * The back door - architecture 9.4.
 *
 * Three jobs that all end as rows in the same ledger, and differ only in how
 * the number is arrived at: a delivery arriving, a shelf being counted, and a
 * correction with a reason attached.
 *
 * Deliberately separate from the register. A cashier at a queue and a person
 * with a pallet are doing different work, and one screen that tried to be
 * both would be worse at each. It is also a different act of trust: a cashier
 * holds none of these permissions.
 *
 * **Quantities are thousandths everywhere below the screen** - one unit is
 * 1000 - and the screen never shows that. A person counting a shelf counts
 * things, so the conversion happens at this boundary and nowhere else.
 */

const UNIT = 1000;

type Tab = "receive" | "count" | "adjust";

/** The three jobs, and the permission each needs. One list so the tabs and
 *  the landing tab cannot disagree about what someone may do. */
const TABS: { id: Tab; label: string; permission: Permission }[] = [
  { id: "receive", label: "Receive", permission: "stock.receive" },
  { id: "count", label: "Count", permission: "stock.count" },
  { id: "adjust", label: "Adjust", permission: "stock.adjust" },
];

export function StockroomScreen({
  session,
  onClose,
}: {
  session: SessionResponse;
  onClose: () => void;
}) {
  // The first tab this person may actually use. Defaulting to "receive"
  // regardless would drop someone holding only `stock.count` onto a receiving
  // form that refuses them - a screen that looks broken rather than one that
  // is not for them.
  const allowed = TABS.filter((entry) =>
    session.permissions.includes(entry.permission),
  );
  const [tab, setTab] = useState<Tab>(allowed[0]?.id ?? "receive");

  if (allowed.length === 0) {
    return (
      <div className="register stockroom">
        <header className="bar">
          <button type="button" className="link" onClick={onClose}>
            ← Register
          </button>
          <span className="till">{session.employee_code}</span>
        </header>
        <p className="empty">
          Moving stock needs a permission this account does not have. A manager
          can receive a delivery, count a shelf, or record a correction.
        </p>
      </div>
    );
  }

  return (
    <div className="register stockroom">
      <header className="bar">
        <button type="button" className="link" onClick={onClose}>
          ← Register
        </button>
        <nav className="tabs">
          {allowed.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className={tab === entry.id ? "on" : ""}
              onClick={() => setTab(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </nav>
        <span className="till">{session.employee_code}</span>
      </header>

      {tab === "receive" && <Receiving />}
      {tab === "count" && <Counting />}
      {tab === "adjust" && <Adjusting />}
    </div>
  );
}


/**
 * A typed or scanned string to the product it means.
 *
 * All digits is a code; anything else is a search. The person at the back
 * door should not have to tell the till which one they meant, and the
 * register's entry field has worked this way since phase 3 - this screen
 * only searched by name, so scanning a barcode into it reported "nothing
 * found" for a product sitting right there in the catalogue.
 *
 * Returns every match, because a name search legitimately finds several and
 * silently taking the first is how the wrong product gets counted.
 */
async function resolve(typed: string): Promise<ProductOut[]> {
  if (/^[0-9]+$/.test(typed)) {
    const found = await catalog.lookup(typed);
    return found.resolved && found.product ? [found.product] : [];
  }
  return (await catalog.search(typed)).results;
}

// ── Receiving ───────────────────────────────────────────────────────────────

function Receiving() {
  const [lines, setLines] = useState<ReceiptLineOut[]>([]);
  const [code, setCode] = useState("");
  const [packs, setPacks] = useState(1);
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const codeRef = useRef<HTMLInputElement>(null);

  const focus = useCallback(() => codeRef.current?.focus(), []);
  useEffect(focus, [focus]);

  async function addLine(event: React.FormEvent) {
    event.preventDefault();
    const raw = code.trim();
    if (!raw) return;

    setBusy(true);
    try {
      // Previewed rather than committed, so a delivery entered wrong is a
      // keystroke rather than a correction with a reason attached.
      const line = await inventory.previewLine(raw, packs);
      setLines((current) => [...current, line]);
      setMessage(null);
      setCode("");
      setPacks(1);
    } catch (error) {
      setMessage({
        text: error instanceof ApiError ? error.message : "That did not work.",
        bad: true,
      });
    } finally {
      setBusy(false);
      focus();
    }
  }

  async function commit() {
    setBusy(true);
    try {
      const result = await inventory.receive(
        lines.map((line) => ({ barcode: line.barcode, packs: line.packs })),
      );
      setLines([]);
      setMessage({
        text: `${result.movement_ids.length} line(s) received.`,
        bad: false,
      });
    } catch (error) {
      // The whole delivery failed, so the list stays exactly as it was and
      // the person can fix the offending line. Half a delivery is worse than
      // none.
      setMessage({
        text:
          error instanceof ApiError
            ? `Nothing was received: ${error.message}`
            : "Nothing was received.",
        bad: true,
      });
    } finally {
      setBusy(false);
      focus();
    }
  }

  const totalUnits = lines.reduce((sum, line) => sum + line.units, 0);

  return (
    <>
      <form className="entry" onSubmit={addLine}>
        <input
          ref={codeRef}
          value={code}
          onChange={(event) => setCode(event.target.value)}
          placeholder="Scan the case or the item"
          autoFocus
          autoComplete="off"
        />
        <input
          className="qty"
          type="number"
          min={1}
          value={packs}
          onChange={(event) => setPacks(Number(event.target.value) || 1)}
          aria-label="How many"
        />
        <button type="submit" disabled={busy}>
          Add
        </button>
      </form>

      {message && (
        <p className={message.bad ? "msg error" : "msg"} role="alert">
          {message.text}
        </p>
      )}

      <ul className="lines">
        {lines.map((line, index) => (
          <li key={`${line.barcode}-${index}`}>
            <span className="what">{line.description}</span>
            <span className="sub">
              {/* Both numbers, because a receipt three orders of magnitude
                  out looks plausible in either one alone. */}
              {line.packs} x {line.pack_size} = <strong>{line.units}</strong>
            </span>
            <button
              type="button"
              className="link"
              onClick={() =>
                setLines((current) => current.filter((_, i) => i !== index))
              }
            >
              Remove
            </button>
          </li>
        ))}
      </ul>

      {lines.length > 0 ? (
        <div className="totals">
          <div className="row">
            <span>{lines.length} line(s)</span>
            <strong>{totalUnits} units</strong>
          </div>
          <button type="button" disabled={busy} onClick={() => void commit()}>
            {busy ? "Receiving…" : "Receive all"}
          </button>
        </div>
      ) : (
        <p className="empty">
          Scan what arrived. Nothing is recorded until you receive the whole
          delivery.
        </p>
      )}
    </>
  );
}

// ── Counting ────────────────────────────────────────────────────────────────

type CountRow = { product_id: string; description: string; counted: string };

function Counting() {
  const [rows, setRows] = useState<CountRow[]>([]);
  const [query, setQuery] = useState("");
  const [choices, setChoices] = useState<ProductOut[]>([]);
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  async function find(event: React.FormEvent) {
    event.preventDefault();
    const raw = query.trim();
    if (!raw) return;

    setBusy(true);
    try {
      const [only, ...rest] = await resolve(raw);
      if (!only) {
        setMessage({ text: `Nothing found for "${raw}".`, bad: true });
        setChoices([]);
        return;
      }
      if (rest.length > 0) {
        // A name matched several. Taking the first would count the wrong
        // product and look like it worked.
        setChoices([only, ...rest]);
        setMessage(null);
        return;
      }
      addRow(only);
    } finally {
      setBusy(false);
    }
  }

  function addRow(product: ProductOut) {
    setChoices([]);
    setQuery("");
    if (rows.some((row) => row.product_id === product.product_id)) {
      setMessage({ text: `${product.short_name} is already on the list.`, bad: true });
      return;
    }
    setMessage(null);
    setRows((current) => [
      ...current,
      { product_id: product.product_id, description: product.short_name, counted: "" },
    ]);
  }

  async function commit() {
    const counted = rows.filter((row) => row.counted.trim() !== "");
    if (counted.length === 0) return;

    setBusy(true);
    try {
      const result = await inventory.count(
        counted.map((row) => ({
          product_id: row.product_id,
          // The screen counts things; the ledger counts thousandths.
          counted_milli: Math.round(Number(row.counted) * UNIT),
        })),
      );
      setRows([]);
      setMessage({
        text:
          result.movement_ids.length === 0
            ? "Counted. Everything matched, so nothing was corrected."
            : `Counted. ${result.movement_ids.length} correction(s) recorded.`,
        bad: false,
      });
    } catch (error) {
      setMessage({
        text: error instanceof ApiError ? error.message : "That did not work.",
        bad: true,
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form className="entry" onSubmit={find}>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Scan it, or search by name"
          autoFocus
          autoComplete="off"
        />
        <button type="submit" disabled={busy}>
          Add
        </button>
      </form>

      {choices.length > 0 && (
        <ul className="results">
          {choices.map((product) => (
            <li key={product.product_id}>
              <button type="button" onClick={() => addRow(product)}>
                <span>{product.short_name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {message && (
        <p className={message.bad ? "msg error" : "msg"} role="alert">
          {message.text}
        </p>
      )}

      <ul className="lines">
        {rows.map((row, index) => (
          <li key={row.product_id}>
            <span className="what">{row.description}</span>
            <input
              className="qty"
              type="number"
              min={0}
              value={row.counted}
              placeholder="On the shelf"
              aria-label={`Counted ${row.description}`}
              onChange={(event) =>
                setRows((current) =>
                  current.map((r, i) =>
                    i === index ? { ...r, counted: event.target.value } : r,
                  ),
                )
              }
            />
            <button
              type="button"
              className="link"
              onClick={() => setRows((current) => current.filter((_, i) => i !== index))}
            >
              Remove
            </button>
          </li>
        ))}
      </ul>

      {rows.length > 0 ? (
        <div className="totals">
          <p className="note muted">
            What you type is what is on the shelf. The till works out the
            difference and records only that, so a count that matches records
            nothing at all.
          </p>
          <button type="button" disabled={busy} onClick={() => void commit()}>
            {busy ? "Recording…" : "Record the count"}
          </button>
        </div>
      ) : (
        <p className="empty">
          Add the products in the section you are counting.
        </p>
      )}
    </>
  );
}

// ── Adjusting ───────────────────────────────────────────────────────────────

function Adjusting() {
  const [query, setQuery] = useState("");
  const [choices, setChoices] = useState<ProductOut[]>([]);
  const [picked, setPicked] = useState<{ id: string; name: string } | null>(null);
  const [units, setUnits] = useState("");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  async function find(event: React.FormEvent) {
    event.preventDefault();
    const raw = query.trim();
    if (!raw) return;

    const [only, ...rest] = await resolve(raw);
    if (!only) {
      setMessage({ text: `Nothing found for "${raw}".`, bad: true });
      setChoices([]);
      return;
    }
    if (rest.length > 0) {
      setChoices([only, ...rest]);
      setMessage(null);
      return;
    }
    choose(only);
  }

  function choose(product: ProductOut) {
    setPicked({ id: product.product_id, name: product.short_name });
    setChoices([]);
    setMessage(null);
    setQuery("");
  }

  async function commit() {
    if (!picked) return;
    setBusy(true);
    try {
      await inventory.adjust(picked.id, Math.round(Number(units) * UNIT), note);
      setMessage({ text: `${picked.name} adjusted.`, bad: false });
      setPicked(null);
      setUnits("");
      setNote("");
    } catch (error) {
      setMessage({
        text: error instanceof ApiError ? error.message : "That did not work.",
        bad: true,
      });
    } finally {
      setBusy(false);
    }
  }

  const amount = Number(units);
  const ready = picked !== null && units.trim() !== "" && amount !== 0 && note.trim() !== "";

  return (
    <>
      <form className="entry" onSubmit={find}>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Scan it, or search by name"
          autoFocus
          autoComplete="off"
        />
        <button type="submit">Find</button>
      </form>

      {choices.length > 0 && (
        <ul className="results">
          {choices.map((product) => (
            <li key={product.product_id}>
              <button type="button" onClick={() => choose(product)}>
                <span>{product.short_name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {message && (
        <p className={message.bad ? "msg error" : "msg"} role="alert">
          {message.text}
        </p>
      )}

      {picked ? (
        <div className="card">
          <h2>{picked.name}</h2>
          <label>
            How many, and which way
            <input
              className="qty"
              type="number"
              value={units}
              placeholder="-2"
              onChange={(event) => setUnits(event.target.value)}
            />
          </label>
          <p className="note muted">
            Negative for breakage, theft or waste. Positive for stock found.
          </p>
          <label>
            Why
            <input
              value={note}
              placeholder="Two tins damaged in transit"
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          <p className="note muted">
            {/* The one movement with no document behind it. An adjustments
                column full of blanks is where losses go to be forgotten. */}
            Required. This is the only stock change with nothing else to
            explain it.
          </p>
          <button type="button" disabled={!ready || busy} onClick={() => void commit()}>
            {busy ? "Recording…" : "Record the adjustment"}
          </button>
        </div>
      ) : (
        <p className="empty">Find the product you need to correct.</p>
      )}
    </>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../core/api/client";
import { catalog, register } from "../../core/api/register";
import type {
  CartOut,
  PostSaleResponse,
  ProductOut,
  SessionResponse,
  TenderQuote,
} from "../../core/api/contract";
import { useBarcodeCapture } from "../../core/barcode-capture/useBarcodeCapture";

/**
 * The register — architecture §4.
 *
 * Designed for one hand on a keyboard and a queue waiting. The barcode field
 * holds focus at all times and returns to it after every action, because the
 * commonest way to lose seconds at a till is discovering that focus went
 * somewhere else. Nothing here computes a total: every figure on screen came
 * from the recalculated basket the server returned.
 */
export function RegisterScreen({ session }: { session: SessionResponse }) {
  const [cart, setCart] = useState<CartOut | null>(null);
  const [entry, setEntry] = useState("");
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null);
  const [results, setResults] = useState<ProductOut[]>([]);
  const [quote, setQuote] = useState<TenderQuote | null>(null);
  const [sale, setSale] = useState<PostSaleResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const entryRef = useRef<HTMLInputElement>(null);

  const focusEntry = useCallback(() => entryRef.current?.focus(), []);

  const startCart = useCallback(async () => {
    setSale(null);
    setResults([]);
    setQuote(null);
    setMessage(null);
    setCart(await register.openCart());
    focusEntry();
  }, [focusEntry]);

  useEffect(() => {
    void startCart();
  }, [startCart]);

  const say = (text: string, bad = false) => setMessage({ text, bad });

  const addCode = useCallback(
    async (code: string) => {
      if (!cart || busy) return;
      setBusy(true);
      try {
        setCart(await register.addBarcode(cart.cart_id, code));
        setResults([]);
        setMessage(null);
      } catch (error) {
        // A mistyped code and an unstocked one are different problems, and the
        // server has already phrased each one for a cashier. Pass it through
        // rather than replacing it with something generic.
        say(error instanceof ApiError ? error.message : "Could not add that item", true);
      } finally {
        setBusy(false);
        setEntry("");
        focusEntry();
      }
    },
    [cart, busy, focusEntry],
  );

  // A scan anywhere on the page lands in the basket, even with a dialog open.
  useBarcodeCapture({ onScan: addCode });

  async function onEntrySubmit(event: React.FormEvent) {
    event.preventDefault();
    const typed = entry.trim();
    if (!typed) return;

    // All digits is a code; anything else is a search. The cashier should not
    // have to tell the till which one they meant.
    if (/^[0-9]+$/.test(typed)) {
      await addCode(typed);
      return;
    }
    const found = await catalog.search(typed);
    setResults(found.results);
    if (found.results.length === 0) say(`Nothing found for ${typed}`, true);
  }

  async function pick(product: ProductOut) {
    if (!cart) return;
    setCart(await register.addProduct(cart.cart_id, product.product_id));
    setResults([]);
    setEntry("");
    focusEntry();
  }

  async function voidLine(lineNo: number) {
    if (!cart) return;
    setCart(await register.voidLine(cart.cart_id, lineNo));
    focusEntry();
  }

  async function askToTender(method: string) {
    if (!cart || cart.item_count === 0) return;
    setQuote(await register.tenderQuote(cart.cart_id, method));
  }

  async function takeCash(tenderedPaise?: number) {
    if (!cart) return;
    setBusy(true);
    try {
      const result = await register.takePayment(cart.cart_id, "cash", tenderedPaise);
      if (!result.cart.settled) {
        setCart(result.cart);
        say("Balance still outstanding", true);
        return;
      }
      setSale(await register.post(cart.cart_id));
    } catch (error) {
      say(error instanceof ApiError ? error.message : "Payment failed", true);
    } finally {
      setBusy(false);
      setQuote(null);
    }
  }

  if (sale) return <CompletedSale sale={sale} onNext={startCart} />;

  return (
    <div className="register">
      <header className="bar">
        <span className="who">{session.full_name}</span>
        <span className="till">{session.employee_code}</span>
      </header>

      <form className="entry" onSubmit={onEntrySubmit}>
        <input
          ref={entryRef}
          data-barcode-field="true"
          value={entry}
          onChange={(event) => setEntry(event.target.value)}
          placeholder="Scan, type a barcode, or search by name"
          autoFocus
          autoComplete="off"
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

      {results.length > 0 && (
        <ul className="results">
          {results.map((product) => (
            <li key={product.product_id}>
              <button type="button" onClick={() => void pick(product)}>
                <span>{product.short_name}</span>
                <span className="price">{product.unit_price.text}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <LineList cart={cart} onVoid={(lineNo) => void voidLine(lineNo)} />
      <Totals cart={cart} />

      <div className="tender">
        <button
          type="button"
          disabled={!cart || cart.item_count === 0 || busy}
          onClick={() => void askToTender("cash")}
        >
          Cash
        </button>
        <button type="button" className="secondary" onClick={() => void startCart()}>
          New sale
        </button>
      </div>

      {quote && (
        <TenderDialog
          quote={quote}
          onCancel={() => setQuote(null)}
          onConfirm={(tendered) => void takeCash(tendered)}
        />
      )}
    </div>
  );
}

function LineList({
  cart,
  onVoid,
}: {
  cart: CartOut | null;
  onVoid: (lineNo: number) => void;
}) {
  if (!cart || cart.lines.length === 0) {
    return <p className="empty">No items yet.</p>;
  }
  return (
    <table className="lines">
      <tbody>
        {cart.lines.map((line) => (
          <tr key={line.line_no}>
            <td className="no">{line.line_no}</td>
            <td className="desc">{line.description}</td>
            <td className="qty">
              {line.quantity_text} &times; {line.unit_price.text}
            </td>
            <td className="amt">{line.line_total.text}</td>
            <td>
              <button
                type="button"
                className="void"
                aria-label={`Void line ${line.line_no}`}
                disabled={cart.locked}
                onClick={() => onVoid(line.line_no)}
              >
                &times;
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Totals({ cart }: { cart: CartOut | null }) {
  if (!cart) return null;
  return (
    <div className="totals">
      {cart.tax_breakdown.map((component) => (
        <p key={component.tax_code} className="tax">
          <span>
            GST {component.rate_bp / 100}% on {component.taxable_base.text}
          </span>
          <span>{component.tax_amount.text}</span>
        </p>
      ))}
      <p className="grand">
        <span>Total</span>
        <span>{cart.total_before_rounding.text}</span>
      </p>
    </div>
  );
}

function TenderDialog({
  quote,
  onCancel,
  onConfirm,
}: {
  quote: TenderQuote;
  onCancel: () => void;
  onConfirm: (tenderedPaise?: number) => void;
}) {
  const [handed, setHanded] = useState("");

  return (
    <div className="dialog" role="dialog" aria-label="Take cash">
      <h3>Cash</h3>
      <p className="due">
        <span>Due</span>
        <span>{quote.collected.text}</span>
      </p>
      {quote.rounding_adjustment.paise !== 0 && (
        // Shown before the money is taken, not discovered afterwards on the
        // receipt (architecture §13.4).
        <p className="rounding">
          Rounded from {quote.outstanding.text} ({quote.rounding_adjustment.text})
        </p>
      )}
      <label htmlFor="handed">Cash received (optional)</label>
      <input
        id="handed"
        inputMode="numeric"
        value={handed}
        onChange={(event) => setHanded(event.target.value)}
        placeholder="for change"
        autoFocus
      />
      <div className="row">
        <button
          type="button"
          onClick={() =>
            onConfirm(handed ? Math.round(Number(handed) * 100) : undefined)
          }
        >
          Take payment
        </button>
        <button type="button" className="secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function CompletedSale({
  sale,
  onNext,
}: {
  sale: PostSaleResponse;
  onNext: () => void;
}) {
  const [saved, setSaved] = useState<string | null>(null);

  // Enter starts the next sale. The commonest action gets the biggest key.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Enter") onNext();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onNext]);

  return (
    <div className="done">
      <h2>{sale.grand_total.text}</h2>
      {sale.change_due.paise > 0 && (
        <p className="change">Change {sale.change_due.text}</p>
      )}
      <div
        className="receipt-view"
        dangerouslySetInnerHTML={{ __html: sale.receipt_html }}
      />
      <div className="row">
        <button type="button" onClick={onNext}>
          New sale (Enter)
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() =>
            void register.writePdf(sale.sale_id).then((result) => setSaved(result.path))
          }
        >
          Save PDF
        </button>
      </div>
      {saved && <p className="msg">Saved to {saved}</p>}
    </div>
  );
}

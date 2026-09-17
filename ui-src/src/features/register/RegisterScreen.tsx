import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../core/api/client";
import { catalog, register } from "../../core/api/register";
import type {
  CartOut,
  PostSaleResponse,
  ProductOut,
  SessionResponse,
  TaxCodeOut,
  TenderQuote,
  TenderResponse,
} from "../../core/api/contract";
import { useBarcodeCapture } from "../../core/barcode-capture/useBarcodeCapture";
import { PermissionGate } from "../../core/rbac/PermissionGate";
import { SyncIndicator } from "../sync/SyncIndicator";

/**
 * The register — architecture §4.
 *
 * Designed for one hand on a keyboard and a queue waiting. The barcode field
 * holds focus at all times and returns to it after every action, because the
 * commonest way to lose seconds at a till is discovering that focus went
 * somewhere else. Nothing here computes a total: every figure on screen came
 * from the recalculated basket the server returned.
 */
export function RegisterScreen({
  session,
  onOpenStockroom,
  onOpenAdmin,
}: {
  session: SessionResponse;
  onOpenStockroom?: () => void;
  onOpenAdmin?: () => void;
}) {
  const [cart, setCart] = useState<CartOut | null>(null);
  const [entry, setEntry] = useState("");
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null);
  const [results, setResults] = useState<ProductOut[]>([]);
  const [quote, setQuote] = useState<TenderQuote | null>(null);
  const [upi, setUpi] = useState<TenderResponse | null>(null);
  const [sale, setSale] = useState<PostSaleResponse | null>(null);
  const [busy, setBusy] = useState(false);
  //: The code the catalogue could not answer, while the cashier decides
  //: whether to sell it anyway. `""` means they opened the form by hand.
  const [unlisted, setUnlisted] = useState<string | null>(null);
  const [rates, setRates] = useState<TaxCodeOut[]>([]);
  const entryRef = useRef<HTMLInputElement>(null);

  const focusEntry = useCallback(() => entryRef.current?.focus(), []);

  const startCart = useCallback(async () => {
    setSale(null);
    setResults([]);
    setQuote(null);
    setUpi(null);
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
        // 404 is "a readable code for a product this shop does not have" — the
        // one case where selling it anyway is the right answer. A 422 is a
        // mistyped code and offering the form there would train cashiers to
        // hand-key items they could simply rescan.
        if (error instanceof ApiError && error.status === 404) setUnlisted(code);
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

  // Fetched once and kept. The form must open instantly with a queue waiting,
  // and GST slabs do not change during a shift.
  useEffect(() => {
    catalog
      .taxCodes()
      .then((response) => setRates(response.tax_codes))
      .catch(() => setRates([]));
  }, []);

  async function sellUnlisted(item: {
    description: string;
    rupees: string;
    tax_code: string;
  }) {
    if (!cart) return;
    setBusy(true);
    try {
      setCart(
        await register.addUnlisted(cart.cart_id, {
          description: item.description,
          // Rupees on screen, paise on the wire. The one conversion this
          // screen owns, and the only place a decimal is allowed to exist.
          unit_price_paise: Math.round(Number(item.rupees) * 100),
          tax_code: item.tax_code,
          barcode: unlisted || null,
        }),
      );
      setUnlisted(null);
      setMessage(null);
    } catch (error) {
      say(error instanceof ApiError ? error.message : "Could not add that item", true);
    } finally {
      setBusy(false);
      focusEntry();
    }
  }

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

  /**
   * UPI opens its attempt immediately — architecture §13.3.
   *
   * There is no QR to show: the shop's is printed and standing on the
   * counter. What the till does is freeze the basket at a figure, so that the
   * amount the cashier reads out is the amount still owed when the customer
   * finishes typing it into their own app.
   */
  async function startUpi() {
    if (!cart || cart.item_count === 0 || busy) return;
    setBusy(true);
    try {
      setUpi(await register.takePayment(cart.cart_id, "upi"));
    } catch (error) {
      say(error instanceof ApiError ? error.message : "Could not start UPI", true);
    } finally {
      setBusy(false);
    }
  }

  /** Whatever the customer actually paid, settle or continue accordingly. */
  async function settleWith(result: TenderResponse) {
    setCart(result.cart);
    if (!result.cart.settled) {
      say(`${result.cart.outstanding.text} still to collect`, true);
      return;
    }
    setSale(await register.post(result.cart.cart_id));
  }

  async function attestUpi(amountPaise: number, reference: string) {
    if (!upi) return;
    setBusy(true);
    try {
      await settleWith(await register.attest(upi.attempt_id, amountPaise, reference));
    } catch (error) {
      say(error instanceof ApiError ? error.message : "Could not record that", true);
    } finally {
      setBusy(false);
      setUpi(null);
      focusEntry();
    }
  }

  async function upiUnsure() {
    if (!upi) return;
    setBusy(true);
    try {
      const result = await register.markUnknown(upi.attempt_id);
      setCart(result.cart);
      // Nothing was collected, so the balance stands. The sale will post
      // flagged for a supervisor once the cashier settles it (§13.5).
      say("Held for review. Collect the balance or void the sale.", true);
    } catch (error) {
      say(error instanceof ApiError ? error.message : "Could not record that", true);
    } finally {
      setBusy(false);
      setUpi(null);
      focusEntry();
    }
  }

  async function cancelUpi() {
    if (!upi) return;
    try {
      setCart((await register.cancelPayment(upi.attempt_id)).cart);
    } finally {
      setUpi(null);
      focusEntry();
    }
  }

  async function takeCash(tenderedPaise?: number) {
    if (!cart) return;
    setBusy(true);
    try {
      await settleWith(await register.takePayment(cart.cart_id, "cash", tenderedPaise));
    } catch (error) {
      say(error instanceof ApiError ? error.message : "Payment failed", true);
    } finally {
      setBusy(false);
      setQuote(null);
      focusEntry();
    }
  }

  if (sale) return <CompletedSale sale={sale} onNext={startCart} />;

  return (
    <div className="register">
      <header className="bar">
        <span className="who">{session.full_name}</span>
        <SyncIndicator session={session} />
        {/* Only for someone who may move stock. A cashier is never offered a
            door that would refuse them. */}
        {onOpenStockroom && (
          <PermissionGate session={session} permission="stock.receive">
            <button type="button" className="link" onClick={onOpenStockroom}>
              Stockroom
            </button>
          </PermissionGate>
        )}
        {/* `product.read` rather than `product.edit`: low stock is worth
            opening for someone who may not edit the catalogue, and the tabs
            inside filter themselves. */}
        {onOpenAdmin && (
          <PermissionGate session={session} permission="product.read">
            <button type="button" className="link" onClick={onOpenAdmin}>
              Catalogue
            </button>
          </PermissionGate>
        )}
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
        <button
          type="button"
          disabled={!cart || cart.item_count === 0 || busy}
          onClick={() => void startUpi()}
        >
          UPI
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

      {unlisted !== null && rates.length > 0 && (
        <UnlistedDialog
          barcode={unlisted}
          rates={rates}
          busy={busy}
          onCancel={() => {
            setUnlisted(null);
            focusEntry();
          }}
          onConfirm={(item) => void sellUnlisted(item)}
        />
      )}

      {upi && (
        <UpiDialog
          session={session}
          attempt={upi}
          busy={busy}
          onAttest={(amount, reference) => void attestUpi(amount, reference)}
          onUnsure={() => void upiUnsure()}
          onCancel={() => void cancelUpi()}
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

function UnlistedDialog({
  barcode,
  rates,
  busy,
  onCancel,
  onConfirm,
}: {
  barcode: string;
  rates: TaxCodeOut[];
  busy: boolean;
  onCancel: () => void;
  onConfirm: (item: {
    description: string;
    rupees: string;
    tax_code: string;
  }) => void;
}) {
  const [description, setDescription] = useState("");
  const [rupees, setRupees] = useState("");
  const [taxCode, setTaxCode] = useState(rates[0]?.code ?? "");

  // Both, or the sale is wrong in a way nobody can reconstruct later: a line
  // with no description is an anonymous amount of money, and a price of zero
  // is a giveaway that looks like a completed sale.
  const ready =
    description.trim().length > 0 && Number(rupees) > 0 && taxCode.length > 0;

  return (
    <div className="dialog" role="dialog" aria-label="Sell an unlisted item">
      <h3>Not in the catalogue</h3>
      {barcode && <p className="rounding">Code {barcode} matched nothing.</p>}
      <p className="rounding">
        Sell it now and it goes on the list for someone to add properly. Stock
        will not be tracked for it.
      </p>

      <label htmlFor="unlisted-name">What is it?</label>
      <input
        id="unlisted-name"
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        placeholder="Read it off the packet"
        autoFocus
        autoComplete="off"
      />

      <label htmlFor="unlisted-price">Price</label>
      <input
        id="unlisted-price"
        inputMode="decimal"
        value={rupees}
        onChange={(event) => setRupees(event.target.value)}
        placeholder="in rupees"
      />

      <label htmlFor="unlisted-tax">Tax</label>
      <select
        id="unlisted-tax"
        value={taxCode}
        onChange={(event) => setTaxCode(event.target.value)}
      >
        {rates.map((rate) => (
          <option key={rate.code} value={rate.code}>
            {rate.name}
          </option>
        ))}
      </select>

      <div className="row">
        <button
          type="button"
          disabled={!ready || busy}
          onClick={() => onConfirm({ description, rupees, tax_code: taxCode })}
        >
          Sell it anyway
        </button>
        <button type="button" className="secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
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

function UpiDialog({
  session,
  attempt,
  busy,
  onAttest,
  onUnsure,
  onCancel,
}: {
  session: SessionResponse;
  attempt: TenderResponse;
  busy: boolean;
  onAttest: (amountPaise: number, reference: string) => void;
  onUnsure: () => void;
  onCancel: () => void;
}) {
  const asked = attempt.cart.outstanding.paise + attempt.cart.paid.paise;
  const [received, setReceived] = useState((asked / 100).toFixed(2));
  const [reference, setReference] = useState("");

  const paise = Math.round(Number(received) * 100);
  const mismatch = Number.isFinite(paise) && paise !== asked;

  return (
    <div className="dialog upi" role="dialog" aria-label="UPI payment">
      <h3>UPI</h3>
      {/*
        No QR here, deliberately. The shop's is printed and standing on the
        counter (architecture §13.3), so the till's job is to say the figure
        out loud and then record what actually arrived.
      */}
      <p className="due">
        <span>Ask for</span>
        <span>{attempt.cart.outstanding.text}</span>
      </p>
      <p className="hint">Customer pays at the counter QR.</p>

      <label htmlFor="received">Amount received</label>
      <input
        id="received"
        inputMode="decimal"
        value={received}
        onChange={(event) => setReceived(event.target.value)}
        autoFocus
      />
      {mismatch && (
        // The customer types the amount into their own app, so this is a
        // routine correction rather than an error worth blocking on.
        <p className="hint warn">
          {paise < asked ? "Short — the balance stays open." : "Over — change is due in cash."}
        </p>
      )}

      <label htmlFor="utr">Reference / UTR (optional)</label>
      <input
        id="utr"
        value={reference}
        onChange={(event) => setReference(event.target.value)}
        placeholder="from the customer's app"
        autoComplete="off"
      />

      <div className="row">
        {/*
          Saying money arrived is `payment.attest`, which is what
          `POST /register/payments/{id}/confirm` checks. Someone without it
          keeps the other two buttons: "Can't tell" is the honest answer when
          you may not make the call yourself, and it is already the path that
          holds the sale for a supervisor.
        */}
        <PermissionGate session={session} permission="payment.attest">
          <button
            type="button"
            disabled={busy || !Number.isFinite(paise) || paise <= 0}
            onClick={() => onAttest(paise, reference)}
          >
            Received
          </button>
        </PermissionGate>
        <button type="button" className="secondary" disabled={busy} onClick={onUnsure}>
          Can&rsquo;t tell
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
      <p className="hint">
        &ldquo;Can&rsquo;t tell&rdquo; holds the sale for a supervisor rather than
        guessing either way.
      </p>
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
      {sale.status === "requires_review" && (
        // Visible at the counter, not only in a report: the cashier should
        // know the sale they just took is going to a supervisor (§13.5).
        <p className="msg error" role="alert">
          Held for review — a supervisor needs to settle this payment.
        </p>
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

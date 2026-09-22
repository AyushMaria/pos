import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../core/api/client";
import { overrides } from "../../core/api/overrides";
import { catalog, register, shifts } from "../../core/api/register";
import type {
  CartLineOut,
  CartOut,
  Permission,
  PostSaleResponse,
  ProductOut,
  SessionResponse,
  TaxCodeOut,
  TenderQuote,
  TenderResponse,
} from "../../core/api/contract";
import { useBarcodeCapture } from "../../core/barcode-capture/useBarcodeCapture";
import { useScanShield } from "../../core/barcode-capture/useScanShield";
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
  //: The line a cashier is discounting, while they decide how much.
  const [discounting, setDiscounting] = useState<number | null>(null);
  //: Set when an act was refused for want of a permission. The dialog it
  //: opens performs the act itself once a supervisor has authorised, rather
  //: than handing the key back and hoping the cashier is quick.
  const [needsOverride, setNeedsOverride] = useState<{
    permission: Permission;
    label: string;
    act: () => Promise<void>;
  } | null>(null);
  const entryRef = useRef<HTMLInputElement>(null);

  const focusEntry = useCallback(() => entryRef.current?.focus(), []);

  const [needsShift, setNeedsShift] = useState(false);

  const startCart = useCallback(async () => {
    setSale(null);
    setResults([]);
    setQuote(null);
    setUpi(null);
    setMessage(null);
    try {
      setCart(await register.openCart());
      setNeedsShift(false);
    } catch (error) {
      // No shift is open (phase 8 decision 2). The register cannot sell
      // until someone says what is in the drawer, so the basket waits and
      // the dialog asks. Any other failure is still a failure.
      if (error instanceof ApiError && error.status === 409) {
        setNeedsShift(true);
        return;
      }
      throw error;
    }
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

  const discountLine = useCallback(
    async (lineNo: number, amountPaise: number) => {
      if (!cart) return;
      setBusy(true);
      try {
        setCart(await register.discountLine(cart.cart_id, lineNo, amountPaise));
        setDiscounting(null);
        setNeedsOverride(null);
        setMessage(null);
      } catch (error) {
        if (error instanceof ApiError && error.status === 403) {
          // Not a failure — the ordinary case. The cashier may not do this,
          // so ask somebody who may, and let them do it.
          setNeedsOverride({
            permission: "sale.discount.line",
            label: `take ${(amountPaise / 100).toFixed(2)} off line ${lineNo}`,
            act: () => discountLine(lineNo, amountPaise),
          });
          return;
        }
        say(error instanceof ApiError ? error.message : "Could not discount that", true);
      } finally {
        setBusy(false);
      }
    },
    [cart],
  );

  // A scan anywhere on the page lands in the basket, even with a dialog open.
  // That is right for the tender and unlisted dialogs — the next customer's
  // items belong in the basket while a cashier counts change.
  //
  // It is wrong for exactly one dialog. A supervisor authorising an act
  // against this basket must not have the basket change underneath them
  // between the asking and the doing, and a wedge scanner is a keyboard: the
  // digits would land in the PIN field and the trailing Enter would spend an
  // attempt. `useScanShield` inside the dialog covers the field; this covers
  // the basket.
  useBarcodeCapture({ onScan: addCode, enabled: needsOverride === null });

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
  if (needsShift) {
    return (
      <OpenShiftDialog
        who={session.full_name}
        onOpened={() => {
          setNeedsShift(false);
          void startCart();
        }}
      />
    );
  }

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

      <LineList
        cart={cart}
        onVoid={(lineNo) => void voidLine(lineNo)}
        onDiscount={setDiscounting}
      />
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

      {/* One dialog at a time. The amount has been chosen by the time a
          supervisor is being asked for, and leaving the first one open
          underneath puts two Cancel buttons on screen — which is confusing
          at a counter and was caught by a test that could not tell them
          apart either. */}
      {discounting !== null && needsOverride === null && (
        <DiscountDialog
          lineNo={discounting}
          line={cart?.lines.find((line) => line.line_no === discounting) ?? null}
          busy={busy}
          onCancel={() => {
            setDiscounting(null);
            focusEntry();
          }}
          onConfirm={(amountPaise) => void discountLine(discounting, amountPaise)}
        />
      )}

      {needsOverride && (
        <OverrideDialog
          permission={needsOverride.permission}
          label={needsOverride.label}
          onCancel={() => {
            setNeedsOverride(null);
            setDiscounting(null);
            focusEntry();
          }}
          onAuthorised={needsOverride.act}
        />
      )}

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

function OpenShiftDialog({ who, onOpened }: { who: string; onOpened: () => void }) {
  const [rupees, setRupees] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A barcode scanned into the float field is a thirteen-digit rupee amount.
  // The same shield the PIN field wears: a machine-speed burst is dropped
  // and said so, rather than opening the day with ₹89,012,620,100.16.
  const shield = useScanShield({
    onScanBlocked: () => {
      setRupees("");
      setProblem("That was a scan. Type the cash in the drawer.");
    },
  });

  const paise = Math.round(Number(rupees) * 100);
  const ready = rupees.trim().length > 0 && Number.isFinite(paise) && paise >= 0;

  async function open(event: React.FormEvent) {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    try {
      await shifts.open(paise);
      onOpened();
    } catch (error) {
      // 409 here means somebody else opened one between the register's
      // question and this answer — the fix is to carry on, not to retype.
      if (error instanceof ApiError && error.status === 409) {
        onOpened();
        return;
      }
      setProblem(error instanceof ApiError ? error.message : "Could not open the shift");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="dialog" role="dialog" aria-label="Open a shift" onSubmit={open}>
      <h3>Open the day</h3>
      <p className="rounding">
        Count the drawer before the first sale. {who}, this number is what
        tonight&apos;s variance is measured against — a float typed from
        memory at closing time is a guess.
      </p>

      <label htmlFor="opening-float">Cash in the drawer</label>
      <input
        id="opening-float"
        inputMode="decimal"
        value={rupees}
        onChange={(event) => setRupees(event.target.value)}
        onKeyDown={shield.onKeyDown}
        placeholder="in rupees"
        autoFocus
        autoComplete="off"
      />
      {problem && <p className="hint warn">{problem}</p>}

      <div className="actions">
        <button type="submit" disabled={!ready || busy}>
          {busy ? "Opening…" : "Open shift"}
        </button>
      </div>
    </form>
  );
}

function LineList({
  cart,
  onVoid,
  onDiscount,
}: {
  cart: CartOut | null;
  onVoid: (lineNo: number) => void;
  onDiscount: (lineNo: number) => void;
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
              {/* Offered to everyone, unlike the doors in the header. A
                  cashier may not discount, but they may *ask* — hiding the
                  button would leave them telling a customer to come back
                  when the supervisor is free. */}
              <button
                type="button"
                className="discount"
                aria-label={`Discount line ${line.line_no}`}
                disabled={cart.locked}
                onClick={() => onDiscount(line.line_no)}
              >
                %
              </button>
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

/**
 * How much comes off one line.
 *
 * **The amount is checked here, before a supervisor is summoned.** That
 * ordering follows from the override dialog performing the act: if a typo
 * were allowed through, it would mint a grant, write an audit row naming an
 * authorisation, and *then* fail at the server — leaving a record saying a
 * supervisor authorised something that never happened. The server still
 * refuses the same amount, and has to; this stops anybody being asked to
 * approve a number that was never going to work.
 *
 * Equal to the line total is allowed on both sides. Giving an item away is
 * real, and this is the path where it lands in the log with two names on it.
 */
function DiscountDialog({
  lineNo,
  line,
  busy,
  onCancel,
  onConfirm,
}: {
  lineNo: number;
  line: CartLineOut | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: (amountPaise: number) => void;
}) {
  const [amount, setAmount] = useState("");
  const paise = Math.round(Number(amount) * 100);
  const lineTotal = line?.line_total.paise ?? 0;
  const tooMuch = Number.isFinite(paise) && paise > lineTotal;

  return (
    <div className="dialog" role="dialog" aria-label={`Discount line ${lineNo}`}>
      <h3>Discount line {lineNo}</h3>
      {line && (
        <p className="why">
          {line.description} — {line.line_total.text}
        </p>
      )}
      <label htmlFor="discount-amount">Amount off (₹)</label>
      <input
        id="discount-amount"
        inputMode="decimal"
        value={amount}
        onChange={(event) => setAmount(event.target.value)}
        autoFocus
      />
      {tooMuch && (
        <p className="msg bad" role="alert">
          That is more than the line is worth. {line?.line_total.text} makes it
          free.
        </p>
      )}
      <div className="row">
        <button
          type="button"
          disabled={busy || !Number.isFinite(paise) || paise <= 0 || tooMuch}
          onClick={() => onConfirm(paise)}
        >
          Apply
        </button>
        <button type="button" className="secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/**
 * The supervisor override — architecture §11.3.
 *
 * **This dialog performs the act.** It does not hand a permission back to the
 * caller and let them try again: the grant lasts ninety seconds, and a
 * supervisor who is interrupted mid-authorisation would otherwise leave the
 * cashier holding a key that expires before they use it, landing on a 403
 * with nothing on screen to explain why. Authorising *is* the discount. The
 * ninety seconds covers a round trip rather than somebody's next decision.
 *
 * **It reads `status`, not just the message.** Three outcomes need behaviour
 * rather than wording: 409 means the session already holds the key and the act
 * should simply happen; 423 means the approver is locked out, so retrying now
 * cannot work and the button should say so; 503 means this terminal has never
 * seen them, and the answer is a different person or a cable. A dialog that
 * rendered `error.message` for all of them would say the right words and do
 * the wrong thing.
 */
function OverrideDialog({
  permission,
  label,
  onCancel,
  onAuthorised,
}: {
  permission: Permission;
  label: string;
  onCancel: () => void;
  onAuthorised: () => Promise<void>;
}) {
  const [code, setCode] = useState("");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  //: Set by a 423. Retrying cannot help until the stated time, so the button
  //: stops inviting it.
  const [lockedOut, setLockedOut] = useState(false);
  //: A scan landed in the PIN box. Said out loud, because the alternative is
  //: a supervisor watching their PIN vanish and typing it again faster.
  const [scanned, setScanned] = useState(false);

  // A wedge scanner is a keyboard. Without this, an item scanned across the
  // counter types digits into the PIN field and its trailing Enter submits
  // them — spending an attempt against a supervisor's PIN. Attempts are
  // persisted and escalating, so a cashier could lock their own supervisor
  // out by accident, with the shop open, and the audit row would name the
  // supervisor and say `pin.locked`.
  const shield = useScanShield({
    onScanBlocked: () => {
      setPin("");
      setScanned(true);
    },
  });

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await overrides.authorize(code, pin, permission);
      await onAuthorised();
    } catch (cause) {
      if (!(cause instanceof ApiError)) {
        setError("That did not work.");
        return;
      }
      if (cause.status === 409) {
        // Nothing needed authorising — the session already holds the key.
        // Refusing here would be a dialog complaining about a permission the
        // person already has, so do the thing instead.
        await onAuthorised();
        return;
      }
      if (cause.status === 423) setLockedOut(true);
      setError(cause.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog" role="dialog" aria-label="Supervisor authorisation">
      <h3>Supervisor needed</h3>
      <p className="why">A supervisor must authorise this: {label}.</p>
      <label htmlFor="approver">Supervisor code</label>
      <input
        id="approver"
        value={code}
        onChange={(event) => setCode(event.target.value)}
        autoFocus
      />
      <label htmlFor="approver-pin">PIN</label>
      <input
        id="approver-pin"
        type="password"
        inputMode="numeric"
        value={pin}
        onChange={(event) => setPin(event.target.value)}
        onKeyDown={shield.onKeyDown}
      />
      {scanned && (
        <p className="msg warn" role="alert">
          That looked like a scan, not a PIN. Nothing was added and no attempt
          was used — type the PIN again.
        </p>
      )}
      {error && (
        <p className="msg bad" role="alert">
          {error}
        </p>
      )}
      <div className="row">
        <button
          type="button"
          disabled={busy || lockedOut || !code || pin.length < 4}
          onClick={() => void submit()}
        >
          Authorise
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

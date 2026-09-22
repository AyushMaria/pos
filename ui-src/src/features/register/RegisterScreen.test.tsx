import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CartOut,
  MoneyOut,
  PostSaleResponse,
  SessionResponse,
  TenderResponse,
} from "../../core/api/contract";

/**
 * The tender flow — architecture §15 asks for this path to be covered, and
 * §13.3 is why it needs more than a smoke test.
 *
 * The shops take UPI on a printed counter QR, so the customer types the
 * amount into their own app. Short and over are both ordinary outcomes, and
 * "I can't tell" is a third one. Each of those takes a different branch
 * through this screen, and none of them are reachable by looking at it.
 *
 * These are component tests against a mocked API, not Playwright: they prove
 * the screen calls the right endpoint and reacts correctly to what comes
 * back. An end-to-end pass through a real webview is still outstanding.
 */

const money = (paise: number): MoneyOut => ({
  paise,
  text: `₹${(paise / 100).toFixed(2)}`,
});

function cart(overrides: Partial<CartOut> = {}): CartOut {
  return {
    cart_id: "cart-1",
    lines: [],
    item_count: 1,
    subtotal: money(3740),
    discount_total: money(0),
    tax_total: money(570),
    total_before_rounding: money(3740),
    tax_breakdown: [],
    paid: money(0),
    outstanding: money(3740),
    rounding_adjustment: money(0),
    settled: false,
    locked: false,
    ...overrides,
  };
}

function tender(overrides: Partial<TenderResponse> = {}): TenderResponse {
  return {
    attempt_id: "attempt-1",
    state: "pending",
    cart: cart(),
    change_due: money(0),
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    ...overrides,
  };
}

function posted(overrides: Partial<PostSaleResponse> = {}): PostSaleResponse {
  return {
    sale_id: "sale-1",
    receipt_no: "ST01-T1-000001",
    status: "completed",
    grand_total: money(3740),
    rounding_adjustment: money(0),
    change_due: money(0),
    receipt_html: "<section class='receipt'>receipt</section>",
    receipt_text: "receipt",
    ...overrides,
  };
}

const catalogApi = {
  search: vi.fn(),
  lookup: vi.fn(),
  taxCodes: vi.fn(),
};

const RATES = [
  { code: "GST0", name: "GST 0%", rate_bp: 0, is_inclusive: true },
  { code: "GST12", name: "GST 12%", rate_bp: 1200, is_inclusive: true },
];

const api = {
  openCart: vi.fn(),
  readCart: vi.fn(),
  addBarcode: vi.fn(),
  addProduct: vi.fn(),
  addUnlisted: vi.fn(),
  setQuantity: vi.fn(),
  voidLine: vi.fn(),
  abandon: vi.fn(),
  tenderQuote: vi.fn(),
  takePayment: vi.fn(),
  attest: vi.fn(),
  markUnknown: vi.fn(),
  cancelPayment: vi.fn(),
  post: vi.fn(),
  writePdf: vi.fn(),
};

const shiftsApi = { open: vi.fn() };

vi.mock("../../core/api/register", () => ({
  shifts: { open: (...args: unknown[]) => shiftsApi.open(...args) },
  register: new Proxy(
    {},
    { get: (_target, name: string) => (...args: unknown[]) => api[name as keyof typeof api](...args) },
  ),
  catalog: {
    search: (...args: unknown[]) => catalogApi.search(...args),
    lookup: (...args: unknown[]) => catalogApi.lookup(...args),
    taxCodes: () => catalogApi.taxCodes(),
  },
}));

const { RegisterScreen } = await import("./RegisterScreen");
const { ApiError } = await import("../../core/api/client");

const session: SessionResponse = {
  user_id: "u1",
  employee_code: "C001",
  full_name: "Anita Rao",
  store_id: "ST01",
  permissions: ["sale.create", "payment.attest"],
} as SessionResponse;

async function openRegister() {
  const user = userEvent.setup();
  render(<RegisterScreen session={session} />);
  await screen.findByPlaceholderText(/scan, type a barcode/i);
  return user;
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  for (const fn of Object.values(catalogApi)) fn.mockReset();
  shiftsApi.open.mockReset();
  api.openCart.mockResolvedValue(cart());
  api.post.mockResolvedValue(posted());
  catalogApi.taxCodes.mockResolvedValue({ tax_codes: RATES });
});

// ── Cash ────────────────────────────────────────────────────────────────────

describe("cash", () => {
  it("shows the rounded figure before the money is taken", async () => {
    // Architecture §13.4: the adjustment is visible while the customer is
    // still holding their money, not discovered afterwards on the receipt.
    api.tenderQuote.mockResolvedValue({
      method: "cash",
      outstanding: money(3740),
      collected: money(3700),
      rounding_adjustment: money(-40),
    });
    const user = await openRegister();

    await user.click(screen.getByRole("button", { name: "Cash" }));

    expect(await screen.findByText("₹37.00")).toBeDefined();
    expect(screen.getByText(/rounded from ₹37\.40/i)).toBeDefined();
  });

  it("posts the sale once the balance is settled", async () => {
    api.tenderQuote.mockResolvedValue({
      method: "cash",
      outstanding: money(3740),
      collected: money(3700),
      rounding_adjustment: money(-40),
    });
    api.takePayment.mockResolvedValue(
      tender({ state: "approved", cart: cart({ settled: true, paid: money(3700) }) }),
    );
    const user = await openRegister();

    await user.click(screen.getByRole("button", { name: "Cash" }));
    await user.click(await screen.findByRole("button", { name: /take payment/i }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith("cart-1"));
  });
});

// ── UPI ─────────────────────────────────────────────────────────────────────

describe("upi", () => {
  beforeEach(() => {
    api.takePayment.mockResolvedValue(tender());
  });

  it("opens the attempt and asks for the amount, showing no QR", async () => {
    // The shop's QR is printed and standing on the counter (§13.3). A QR
    // rendered here would be a second one, which is worse than none.
    const user = await openRegister();

    await user.click(screen.getByRole("button", { name: "UPI" }));

    expect(api.takePayment).toHaveBeenCalledWith("cart-1", "upi");
    const dialog = await screen.findByRole("dialog", { name: /upi/i });
    expect(within(dialog).getByText(/counter qr/i)).toBeDefined();
    expect(within(dialog).getByText("₹37.40")).toBeDefined();
    // Nothing is drawn: no QR image, canvas or SVG anywhere on the screen.
    expect(document.querySelector("img, canvas, svg")).toBeNull();
  });

  it("defaults the amount to what is owed", async () => {
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));

    const amount = (await screen.findByLabelText(/amount received/i)) as HTMLInputElement;

    expect(amount.value).toBe("37.40");
  });

  it("attests the amount and the reference", async () => {
    api.attest.mockResolvedValue(
      tender({ state: "approved", cart: cart({ settled: true, paid: money(3740) }) }),
    );
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));

    await user.type(await screen.findByLabelText(/reference/i), "UTR42");
    await user.click(screen.getByRole("button", { name: "Received" }));

    await waitFor(() =>
      expect(api.attest).toHaveBeenCalledWith("attempt-1", 3740, "UTR42"),
    );
    await waitFor(() => expect(api.post).toHaveBeenCalled());
  });

  it("does not offer Received to someone who may not attest", async () => {
    // Saying money arrived is `payment.attest`. Hiding the button is UX —
    // `POST /register/payments/{id}/confirm` refuses anyway — but the
    // alternative is a cashier pressing it in front of a customer and
    // learning the till is broken.
    //
    // What is left has to be usable, which is the half worth asserting:
    // "Can't tell" holds the sale for a supervisor, and it is the honest
    // answer for someone who may not make the call themselves.
    const user = userEvent.setup();
    render(<RegisterScreen session={{ ...session, permissions: ["sale.create"] }} />);
    await screen.findByPlaceholderText(/scan, type a barcode/i);
    await user.click(screen.getByRole("button", { name: "UPI" }));

    const dialog = await screen.findByRole("dialog", { name: /upi/i });
    expect(within(dialog).queryByRole("button", { name: "Received" })).toBeNull();
    expect(within(dialog).getByRole("button", { name: /can.t tell/i })).toBeDefined();
  });

  it("still offers Received to someone who may attest", async () => {
    // The other direction, because a gate that hides a control from the
    // people who should have it fails quietly and is the likelier mistake.
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));

    const dialog = await screen.findByRole("dialog", { name: /upi/i });
    expect(within(dialog).getByRole("button", { name: "Received" })).toBeDefined();
  });

  it("keeps the sale open when the customer paid short", async () => {
    // Exit criterion: settled in cash *without leaving the sale*.
    api.attest.mockResolvedValue(
      tender({
        state: "approved",
        cart: cart({ settled: false, paid: money(3000), outstanding: money(740) }),
      }),
    );
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));

    const amount = await screen.findByLabelText(/amount received/i);
    await user.clear(amount);
    await user.type(amount, "30.00");
    await user.click(screen.getByRole("button", { name: "Received" }));

    // The empty reference reaches the client as "", which turns it into null
    // on the wire — that conversion belongs to the client, and is mocked here.
    await waitFor(() => expect(api.attest).toHaveBeenCalledWith("attempt-1", 3000, ""));
    expect(await screen.findByRole("alert")).toHaveTextContent(/₹7\.40 still to collect/i);
    expect(api.post).not.toHaveBeenCalled();
  });

  it("warns before attesting a short payment", async () => {
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));

    const amount = await screen.findByLabelText(/amount received/i);
    await user.clear(amount);
    await user.type(amount, "30");

    expect(screen.getByText(/short — the balance stays open/i)).toBeDefined();
  });

  it("warns that an overpayment means cash change", async () => {
    // Only possible because the QR is static: the customer typed ₹50.
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));

    const amount = await screen.findByLabelText(/amount received/i);
    await user.clear(amount);
    await user.type(amount, "50");

    expect(screen.getByText(/over — change is due in cash/i)).toBeDefined();
  });

  it("refuses to attest nothing", async () => {
    // "They paid nothing" is a cancellation, not a payment.
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));

    const amount = await screen.findByLabelText(/amount received/i);
    await user.clear(amount);

    expect(screen.getByRole("button", { name: "Received" })).toHaveProperty(
      "disabled",
      true,
    );
  });
});

// ── The answer that matters most ────────────────────────────────────────────

describe("can't tell", () => {
  beforeEach(() => {
    api.takePayment.mockResolvedValue(tender());
  });

  it("holds the sale for a supervisor rather than guessing", async () => {
    api.markUnknown.mockResolvedValue(tender({ state: "unknown", cart: cart() }));
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));

    await user.click(await screen.findByRole("button", { name: /can’t tell/i }));

    await waitFor(() => expect(api.markUnknown).toHaveBeenCalledWith("attempt-1"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/held for review/i);
    // Nothing was collected, so nothing is posted on its strength.
    expect(api.post).not.toHaveBeenCalled();
  });

  it("says so on the completed sale, at the counter", async () => {
    api.attest.mockResolvedValue(
      tender({ state: "approved", cart: cart({ settled: true }) }),
    );
    api.post.mockResolvedValue(posted({ status: "requires_review" }));
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));
    await user.click(await screen.findByRole("button", { name: "Received" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /held for review — a supervisor/i,
    );
  });
});

// ── Cancelling ──────────────────────────────────────────────────────────────

describe("cancelling an attempt", () => {
  it("releases the basket", async () => {
    // A pending attempt freezes the basket, so cancelling is the way out.
    api.takePayment.mockResolvedValue(tender());
    api.cancelPayment.mockResolvedValue(
      tender({ state: "cancelled", cart: cart({ locked: false }) }),
    );
    const user = await openRegister();
    await user.click(screen.getByRole("button", { name: "UPI" }));

    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(api.cancelPayment).toHaveBeenCalledWith("attempt-1"));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: /upi/i })).toBeNull(),
    );
  });
});

// ── Change ──────────────────────────────────────────────────────────────────

describe("change", () => {
  it("is shown on the completed sale", async () => {
    api.tenderQuote.mockResolvedValue({
      method: "cash",
      outstanding: money(3740),
      collected: money(3700),
      rounding_adjustment: money(-40),
    });
    api.takePayment.mockResolvedValue(
      tender({ state: "approved", cart: cart({ settled: true }) }),
    );
    api.post.mockResolvedValue(posted({ change_due: money(1300) }));
    const user = await openRegister();

    await user.click(screen.getByRole("button", { name: "Cash" }));
    await user.click(await screen.findByRole("button", { name: /take payment/i }));

    expect(await screen.findByText(/change ₹13\.00/i)).toBeDefined();
  });
});

// ── Items the catalogue does not have ───────────────────────────────────────

describe("an unlisted item", () => {
  /**
   * A customer at the counter with something nothing recognises, and a queue.
   * The alternative to this dialog is not "the cashier adds a product": it is
   * the item rung up as something else at roughly the right price, which is
   * wrong stock, wrong tax and no record that anything was ever missing.
   */
  const missing = "8901999000014";

  async function reachTheForm() {
    api.addBarcode.mockRejectedValue(
      new ApiError(404, `No product found for ${missing}.`),
    );
    const user = await openRegister();
    await user.type(screen.getByPlaceholderText(/scan, type a barcode/i), missing);
    await user.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByRole("dialog", { name: /unlisted/i });
    return user;
  }

  it("offers a way through when a readable code is not stocked", async () => {
    await reachTheForm();

    const dialog = screen.getByRole("dialog", { name: /unlisted/i });
    expect(within(dialog).getByText(new RegExp(missing))).toBeDefined();
    // Said plainly, because it is the one thing the cashier gives up.
    expect(within(dialog).getByText(/stock will not be tracked/i)).toBeDefined();
  });

  it("does not offer it for a mistyped code", async () => {
    // A 422 is a code that failed its check digit. Offering the form here
    // would teach cashiers to hand-key items they could simply rescan.
    api.addBarcode.mockRejectedValue(
      new ApiError(422, "That code could not be read."),
    );
    const user = await openRegister();

    await user.type(screen.getByPlaceholderText(/scan, type a barcode/i), "8901999000013");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await screen.findByText(/could not be read/i);
    expect(screen.queryByRole("dialog", { name: /unlisted/i })).toBeNull();
  });

  it("sends paise, and the code that started it", async () => {
    // Rupees on screen, paise on the wire — the one conversion this screen
    // owns. ₹195.00 typed is 19500, not 195.
    api.addUnlisted.mockResolvedValue(cart());
    const user = await reachTheForm();

    await user.type(screen.getByLabelText(/what is it/i), "Kissan Jam 500g");
    await user.type(screen.getByLabelText("Price"), "195");
    await user.selectOptions(screen.getByLabelText("Tax"), "GST12");
    await user.click(screen.getByRole("button", { name: /sell it anyway/i }));

    await waitFor(() =>
      expect(api.addUnlisted).toHaveBeenCalledWith("cart-1", {
        description: "Kissan Jam 500g",
        unit_price_paise: 19500,
        tax_code: "GST12",
        barcode: missing,
      }),
    );
  });

  it("will not sell a nameless line, or a free one", async () => {
    // A line with no description is an anonymous amount of money on a
    // receipt; a price of zero is a giveaway that looks like a normal sale.
    const user = await reachTheForm();
    const sell = screen.getByRole("button", { name: /sell it anyway/i });

    await user.type(screen.getByLabelText("Price"), "195");
    expect(sell).toHaveProperty("disabled", true);

    await user.type(screen.getByLabelText(/what is it/i), "Jam");
    await waitFor(() => expect(sell).toHaveProperty("disabled", false));

    await user.clear(screen.getByLabelText("Price"));
    await waitFor(() => expect(sell).toHaveProperty("disabled", true));
  });

  it("offers the shop's own rates rather than a hardcoded list", async () => {
    // A rate the client invented would be a tax bill the client invented.
    await reachTheForm();

    const tax = screen.getByLabelText("Tax") as HTMLSelectElement;
    expect([...tax.options].map((option) => option.value)).toEqual([
      "GST0",
      "GST12",
    ]);
  });
});


describe("opening the day", () => {
  it("asks for the float when no shift is open, then starts the basket", async () => {
    // Phase 8 decision 2: the register refuses a cart until a shift is open.
    // First attempt 409s; after the dialog opens one, the second succeeds.
    api.openCart
      .mockRejectedValueOnce(new ApiError(409, "open a shift before selling"))
      .mockResolvedValue(cart());
    shiftsApi.open.mockResolvedValue({ id: "s1" });
    // A person, not a scanner: the float field wears the scan shield, and a
    // zero-delay userEvent types faster than any human.
    const user = userEvent.setup({ delay: 50 });
    render(<RegisterScreen session={session} />);

    const field = await screen.findByLabelText(/cash in the drawer/i);
    expect(screen.queryByPlaceholderText(/scan, type a barcode/i)).toBeNull();

    await user.type(field, "500");
    await user.click(screen.getByRole("button", { name: /open shift/i }));

    expect(shiftsApi.open).toHaveBeenCalledWith(50000);
    expect(await screen.findByPlaceholderText(/scan, type a barcode/i)).toBeDefined();
  });

  it("carries on if somebody else opened one in the meantime", async () => {
    api.openCart
      .mockRejectedValueOnce(new ApiError(409, "open a shift before selling"))
      .mockResolvedValue(cart());
    shiftsApi.open.mockRejectedValue(new ApiError(409, "a shift is already open"));
    const user = userEvent.setup({ delay: 50 });
    render(<RegisterScreen session={session} />);

    await user.type(await screen.findByLabelText(/cash in the drawer/i), "0");
    await user.click(screen.getByRole("button", { name: /open shift/i }));

    expect(await screen.findByPlaceholderText(/scan, type a barcode/i)).toBeDefined();
  });

  it("drops a barcode scanned into the float field", async () => {
    // Thirteen digits at machine speed is a scan, not a drawer count.
    api.openCart.mockRejectedValueOnce(new ApiError(409, "open a shift before selling"));
    const user = userEvent.setup();
    render(<RegisterScreen session={session} />);

    const field = await screen.findByLabelText(/cash in the drawer/i);
    await user.type(field, "8901030608278{Enter}");

    expect(shiftsApi.open).not.toHaveBeenCalled();
    expect(await screen.findByText(/that was a scan/i)).toBeDefined();
    expect((field as HTMLInputElement).value).toBe("");
  });

  it("does not ask when a shift is already open", async () => {
    await openRegister();
    expect(screen.queryByLabelText(/cash in the drawer/i)).toBeNull();
    expect(shiftsApi.open).not.toHaveBeenCalled();
  });
});

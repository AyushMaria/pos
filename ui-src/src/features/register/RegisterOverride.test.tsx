import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CartOut, MoneyOut, SessionResponse } from "../../core/api/contract";

/**
 * The supervisor override, at the screen — phase 7 slice 3.
 *
 * Two things here are not about dialogs in general.
 *
 * **The dialog performs the act.** A grant lasts ninety seconds. If the modal
 * handed the permission back and closed, a supervisor interrupted
 * mid-authorisation would leave the cashier holding a key that expired before
 * they used it, landing on a 403 with nothing on screen to explain why.
 *
 * **A scanner is a keyboard.** This is the first dialog where global capture
 * is wrong, and it is wrong in two directions at once: a scan would change
 * the basket the supervisor is authorising against, and its digits would land
 * in the PIN box with a trailing Enter that spends an attempt. Attempts are
 * persisted and escalating, so a stray scan could lock a supervisor out of
 * their own shop with the doors open.
 */

const money = (paise: number): MoneyOut => ({
  paise,
  text: `₹${(paise / 100).toFixed(2)}`,
});

function cart(overrides: Partial<CartOut> = {}): CartOut {
  return {
    cart_id: "cart-1",
    lines: [
      {
        line_no: 1,
        description: "Parle-G 70g",
        quantity_text: "1",
        unit_price: money(1000),
        line_total: money(1000),
      },
    ],
    item_count: 1,
    subtotal: money(1000),
    discount_total: money(0),
    tax_total: money(0),
    total_before_rounding: money(1000),
    tax_breakdown: [],
    paid: money(0),
    outstanding: money(1000),
    rounding_adjustment: money(0),
    settled: false,
    locked: false,
    ...overrides,
  } as CartOut;
}

const api = {
  openCart: vi.fn(),
  addBarcode: vi.fn(),
  discountLine: vi.fn(),
  voidLine: vi.fn(),
};

const authorize = vi.fn();
const moveCash = vi.fn();

vi.mock("../../core/api/register", () => ({
  register: new Proxy(
    {},
    {
      get:
        (_target, name: string) =>
        (...args: unknown[]) =>
          api[name as keyof typeof api](...args),
    },
  ),
  catalog: { taxCodes: () => Promise.resolve({ tax_codes: [] }) },
  shifts: { moveCash: (...args: unknown[]) => moveCash(...args) },
}));

vi.mock("../../core/api/overrides", () => ({
  overrides: { authorize: (...args: unknown[]) => authorize(...args) },
}));

const { RegisterScreen } = await import("./RegisterScreen");
const { ApiError } = await import("../../core/api/client");

const session: SessionResponse = {
  user_id: "u1",
  employee_code: "C001",
  full_name: "Anita Rao",
  store_id: "ST01",
  permissions: ["sale.create"],
} as SessionResponse;

/**
 * A person, not a machine.
 *
 * The shield treats gaps under 30ms as machine input, so the default
 * zero-delay `userEvent` would type a PIN faster than any human and be
 * blocked — correctly, and uselessly for these tests. 50ms a key is a brisk
 * typist.
 */
const person = () => userEvent.setup({ delay: 50 });

async function openRegister(user = person()) {
  render(<RegisterScreen session={session} />);
  await screen.findByPlaceholderText(/scan, type a barcode/i);
  return user;
}

async function askForADiscount(user: ReturnType<typeof person>) {
  await user.click(screen.getByLabelText(/discount line 1/i));
  await user.type(screen.getByLabelText(/amount off/i), "2");
  await user.click(screen.getByRole("button", { name: /apply/i }));
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  authorize.mockReset();
  moveCash.mockReset();
  api.openCart.mockResolvedValue(cart());
  // A cashier cannot discount. That is the whole premise.
  api.discountLine.mockRejectedValue(new ApiError(403, "permission_denied"));
});

describe("asking for an override", () => {
  it("offers the button to a cashier who may not use it", async () => {
    await openRegister();

    // Deliberately not behind a PermissionGate. Hiding it would leave the
    // cashier telling a customer to come back when a supervisor is free.
    expect(screen.getByLabelText(/discount line 1/i)).toBeEnabled();
  });

  it("opens the supervisor dialog when the act is refused", async () => {
    const user = await openRegister();

    await askForADiscount(user);

    expect(await screen.findByRole("dialog", { name: /supervisor/i })).toBeVisible();
    expect(screen.getByText(/take 2.00 off line 1/i)).toBeVisible();
  });

  it("performs the act itself once a supervisor authorises", async () => {
    const user = await openRegister();
    await askForADiscount(user);
    authorize.mockResolvedValue({ permission: "sale.discount.line" });
    api.discountLine.mockResolvedValue(cart({ discount_total: money(200) }));

    await user.type(screen.getByLabelText(/supervisor code/i), "S001");
    await user.type(screen.getByLabelText(/^pin$/i), "7241");
    await user.click(screen.getByRole("button", { name: /authorise/i }));

    // The grant is spent by the dialog, in the window it was minted for —
    // not handed back to a cashier who may be interrupted.
    expect(api.discountLine).toHaveBeenCalledTimes(2);
    expect(api.discountLine).toHaveBeenLastCalledWith("cart-1", 1, 200);
    expect(screen.queryByRole("dialog", { name: /supervisor/i })).toBeNull();
  });
});

describe("a scanner is a keyboard", () => {
  it("does not let a scan spend an attempt against the PIN", async () => {
    const user = await openRegister();
    await askForADiscount(user);
    const pin = await screen.findByLabelText(/^pin$/i);
    await user.click(pin);

    // A wedge scanner: machine-speed digits and a trailing Enter. `delay:
    // null` types them with no gap at all, which is what the hardware does.
    const scanner = userEvent.setup({ delay: null });
    await scanner.type(pin, "8901262010016{Enter}");

    expect(pin).toHaveValue("");
    expect(authorize).not.toHaveBeenCalled();
    expect(screen.getByText(/looked like a scan/i)).toBeVisible();
  });

  it("does not let a scan change the basket being authorised against", async () => {
    const user = await openRegister();
    await askForADiscount(user);
    await screen.findByRole("dialog", { name: /supervisor/i });

    // Focus has to leave the text fields for this test to mean anything.
    //
    // The first version of it typed with the approver-code box focused and
    // passed with `enabled` deleted, because `respectFocus` already makes the
    // global hook inert inside any INPUT. It was asserting a protection that
    // was not the one under test — the same shape of mistake as a 401 that
    // two layers could produce. A button is not a text field, so capture is
    // live here and only `enabled` can stop it.
    await user.click(screen.getByRole("heading", { name: /supervisor needed/i }));

    const scanner = userEvent.setup({ delay: null });
    await scanner.keyboard("8901262010016{Enter}");

    expect(api.addBarcode).not.toHaveBeenCalled();
  });

  it("still captures a scan once the dialog is gone", async () => {
    const user = await openRegister();
    await askForADiscount(user);
    await user.click(screen.getByRole("button", { name: /cancel/i }));

    api.addBarcode.mockResolvedValue(cart());
    const scanner = userEvent.setup({ delay: null });
    await scanner.keyboard("8901262010016{Enter}");

    // The positive control. Suppression that never lifted would pass both
    // tests above and quietly break the till's main input.
    expect(api.addBarcode).toHaveBeenCalledWith("cart-1", "8901262010016");
  });

  it("lets a person type a PIN at human speed", async () => {
    const user = await openRegister();
    await askForADiscount(user);
    const pin = await screen.findByLabelText(/^pin$/i);

    await user.type(pin, "7241");

    // The other positive control: a shield that blocked everything would
    // make the dialog impossible to use and every test above would still
    // pass.
    expect(pin).toHaveValue("7241");
    expect(screen.queryByText(/looked like a scan/i)).toBeNull();
  });
});

describe("the status decides the behaviour, not just the words", () => {
  it("disables the button when the approver is locked out", async () => {
    const user = await openRegister();
    await askForADiscount(user);
    authorize.mockRejectedValue(
      new ApiError(423, "Too many wrong PINs for S001. That account cannot authorise anything until 2026-09-17T11:15:00+00:00."),
    );

    await user.type(screen.getByLabelText(/supervisor code/i), "S001");
    await user.type(screen.getByLabelText(/^pin$/i), "7241");
    await user.click(screen.getByRole("button", { name: /authorise/i }));

    expect(await screen.findByText(/too many wrong pins/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /authorise/i })).toBeDisabled();
  });

  it("just does the act when the session already holds the key", async () => {
    const user = await openRegister();
    await askForADiscount(user);
    authorize.mockRejectedValue(
      new ApiError(409, "You already have permission to do that."),
    );
    api.discountLine.mockResolvedValue(cart({ discount_total: money(200) }));

    await user.type(screen.getByLabelText(/supervisor code/i), "S001");
    await user.type(screen.getByLabelText(/^pin$/i), "7241");
    await user.click(screen.getByRole("button", { name: /authorise/i }));

    // A dialog complaining about a permission the person already has is a
    // dialog arguing with itself.
    expect(api.discountLine).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/already have permission/i)).toBeNull();
  });

  it("keeps the button live when the approver is simply unknown here", async () => {
    const user = await openRegister();
    await askForADiscount(user);
    authorize.mockRejectedValue(
      new ApiError(503, "S002 has not signed in on this terminal before."),
    );

    await user.type(screen.getByLabelText(/supervisor code/i), "S002");
    await user.type(screen.getByLabelText(/^pin$/i), "7241");
    await user.click(screen.getByRole("button", { name: /authorise/i }));

    expect(await screen.findByText(/has not signed in/i)).toBeVisible();
    // Unlike a lockout, another person can fix this one from the same dialog.
    expect(screen.getByRole("button", { name: /authorise/i })).toBeEnabled();
  });
});

describe("a typo must not cost a supervisor's authorisation", () => {
  it("refuses more than the line is worth before anybody is summoned", async () => {
    const user = await openRegister();

    await user.click(screen.getByLabelText(/discount line 1/i));
    await user.type(screen.getByLabelText(/amount off/i), "999");

    expect(screen.getByText(/more than the line is worth/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /apply/i })).toBeDisabled();
    // The ordering that matters. The override dialog performs the act, so an
    // amount that cannot succeed must never reach it: it would mint a grant,
    // write an audit row naming an authorisation, and then fail — leaving a
    // record that a supervisor approved something that never happened.
    expect(api.discountLine).not.toHaveBeenCalled();
    expect(authorize).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: /supervisor/i })).toBeNull();
  });

  it("allows exactly the line total, which is how free is expressed", async () => {
    const user = await openRegister();

    await user.click(screen.getByLabelText(/discount line 1/i));
    await user.type(screen.getByLabelText(/amount off/i), "10");

    // Refusing this would not stop an item being given away — it would move
    // it to deleting the line, which needs no supervisor and leaves no row.
    expect(screen.getByRole("button", { name: /apply/i })).toBeEnabled();
    expect(screen.queryByText(/more than the line is worth/i)).toBeNull();
  });
});

describe("a payout lent through the modal — phase 8 slice 2b", () => {
  it("asks a supervisor, then pays out exactly what the cashier asked for", async () => {
    const user = await openRegister();
    moveCash.mockRejectedValueOnce(new ApiError(403, "permission_denied"));
    moveCash.mockResolvedValueOnce({ movement_id: "m1" });
    authorize.mockResolvedValue({ permission: "cash.payout" });

    await user.click(screen.getByRole("button", { name: /cash out/i }));
    await user.type(screen.getByLabelText(/amount/i), "20");
    await user.type(screen.getByLabelText(/reason/i), "milk");
    await user.click(screen.getByRole("button", { name: /pay out/i }));

    expect(await screen.findByText(/pay out 20.00 for milk/i)).toBeVisible();
    await user.type(screen.getByLabelText(/supervisor code/i), "S001");
    await user.type(screen.getByLabelText(/^pin$/i), "7241");
    await user.click(screen.getByRole("button", { name: /authorise/i }));

    expect(authorize).toHaveBeenCalledWith("S001", "7241", "cash.payout");
    expect(moveCash).toHaveBeenCalledTimes(2);
    expect(moveCash).toHaveBeenLastCalledWith("out", 2000, "milk");
    expect(await screen.findByText(/paid out 20.00/i)).toBeVisible();
  });

  it("does not offer the close to a cashier", async () => {
    render(<RegisterScreen session={session} onCloseShift={() => undefined} />);
    await screen.findByPlaceholderText(/scan, type a barcode/i);

    expect(screen.queryByRole("button", { name: /close shift/i })).toBeNull();
  });
});

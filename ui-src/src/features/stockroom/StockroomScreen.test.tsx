import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReceiptLineOut, SessionResponse } from "../../core/api/contract";

/**
 * The stockroom - architecture 9.4.
 *
 * Two things are worth testing here and they are both conversions the screen
 * owns alone. A receipt shows packs and units where the ledger holds
 * thousandths, and a count takes units where the ledger takes thousandths.
 * Everything below this file works in thousandths; a person counting a shelf
 * counts things. Getting that boundary wrong is a stock figure a thousand
 * times out, and it looks perfectly ordinary on screen.
 *
 * Component tests against a mocked API, like the register's.
 */

const api = {
  previewLine: vi.fn(),
  receive: vi.fn(),
  count: vi.fn(),
  adjust: vi.fn(),
  search: vi.fn(),
  lookup: vi.fn(),
};

vi.mock("../../core/api/register", () => ({
  inventory: {
    previewLine: (...args: unknown[]) => api.previewLine(...args),
    receive: (...args: unknown[]) => api.receive(...args),
    count: (...args: unknown[]) => api.count(...args),
    adjust: (...args: unknown[]) => api.adjust(...args),
  },
  catalog: {
    search: (...args: unknown[]) => api.search(...args),
    lookup: (...args: unknown[]) => api.lookup(...args),
  },
}));

const { StockroomScreen } = await import("./StockroomScreen");
const { ApiError } = await import("../../core/api/client");

function person(permissions: string[]): SessionResponse {
  return {
    user_id: "u3",
    employee_code: "M001",
    full_name: "Priya Nair",
    store_id: "ST01",
    permissions,
  } as SessionResponse;
}

const ALL = ["stock.receive", "stock.count", "stock.adjust"];

function line(overrides: Partial<ReceiptLineOut> = {}): ReceiptLineOut {
  return {
    product_id: "p1",
    description: "Lifebuoy Soap 100g",
    barcode: "18901030608275",
    packs: 1,
    pack_size: 24,
    units: 24,
    delta_milli: 24_000,
    ...overrides,
  } as ReceiptLineOut;
}

function open(permissions: string[] = ALL) {
  const user = userEvent.setup();
  render(<StockroomScreen session={person(permissions)} onClose={() => {}} />);
  return user;
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
});

describe("receiving", () => {
  it("shows both the case maths and the unit total", async () => {
    // A receipt three orders of magnitude out looks plausible in either
    // number alone, so the screen shows the working.
    api.previewLine.mockResolvedValue(line());
    const user = open();

    await user.type(screen.getByPlaceholderText(/scan the case/i), "18901030608275");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText(/1 x 24 =/)).toBeDefined();
    expect(await screen.findByText("24 units")).toBeDefined();
  });

  it("writes nothing until the whole delivery is received", async () => {
    api.previewLine.mockResolvedValue(line());
    const user = open();

    await user.type(screen.getByPlaceholderText(/scan the case/i), "1890103060827");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByText(/1 x 24 =/);

    expect(api.receive).not.toHaveBeenCalled();

    api.receive.mockResolvedValue({ movement_ids: ["m1"] });
    await user.click(screen.getByRole("button", { name: /receive all/i }));

    await waitFor(() =>
      expect(api.receive).toHaveBeenCalledWith([
        { barcode: "18901030608275", packs: 1 },
      ]),
    );
  });

  it("keeps the list when the delivery is refused", async () => {
    // All lines or none. The person fixes the offending one and tries again,
    // rather than re-entering forty lines they already typed.
    api.previewLine.mockResolvedValue(line());
    api.receive.mockRejectedValue(new ApiError(404, "No product found for 890…"));
    const user = open();

    await user.type(screen.getByPlaceholderText(/scan the case/i), "x");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByText(/1 x 24 =/);
    await user.click(screen.getByRole("button", { name: /receive all/i }));

    expect(await screen.findByText(/nothing was received/i)).toBeDefined();
    expect(screen.getByText(/1 x 24 =/)).toBeDefined();
  });

  it("says why a code was refused rather than swallowing it", async () => {
    api.previewLine.mockRejectedValue(
      new ApiError(422, "a weighed code is one weighing, not a delivery quantity"),
    );
    const user = open();

    await user.type(screen.getByPlaceholderText(/scan the case/i), "2200010012507");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText(/weighing/i)).toBeDefined();
  });
});

describe("counting", () => {
  it("sends thousandths when a person types units", async () => {
    // The conversion this screen exists to own. Type 7, mean seven things,
    // send 7000.
    api.search.mockResolvedValue({
      results: [{ product_id: "p1", short_name: "Soap", unit_price: { paise: 1, text: "" } }],
    });
    api.count.mockResolvedValue({ movement_ids: ["m1"] });
    const user = open();

    await user.click(screen.getByRole("button", { name: "Count" }));
    await user.type(screen.getByPlaceholderText(/scan it, or search/i), "soap");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await user.type(await screen.findByPlaceholderText("On the shelf"), "7");
    await user.click(screen.getByRole("button", { name: /record the count/i }));

    await waitFor(() =>
      expect(api.count).toHaveBeenCalledWith([
        { product_id: "p1", counted_milli: 7000 },
      ]),
    );
  });

  it("says plainly when a count corrected nothing", async () => {
    // The ordinary outcome of a count, and it must not read as a failure.
    api.search.mockResolvedValue({
      results: [{ product_id: "p1", short_name: "Soap", unit_price: { paise: 1, text: "" } }],
    });
    api.count.mockResolvedValue({ movement_ids: [] });
    const user = open();

    await user.click(screen.getByRole("button", { name: "Count" }));
    await user.type(screen.getByPlaceholderText(/scan it, or search/i), "soap");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await user.type(await screen.findByPlaceholderText("On the shelf"), "5");
    await user.click(screen.getByRole("button", { name: /record the count/i }));

    expect(await screen.findByText(/everything matched/i)).toBeDefined();
  });
});

describe("finding a product to count or adjust", () => {
  const product = { product_id: "p1", short_name: "Spectrum book" };

  it("scans a barcode into the count list", async () => {
    // The bug this covers: both tabs only ever searched by name, so scanning
    // a real barcode reported "nothing found" for a product sitting in the
    // catalogue. The register's entry field has routed digits to a lookup
    // since phase 3; this screen did not.
    api.lookup.mockResolvedValue({ resolved: true, product });
    const user = open();

    await user.click(screen.getByRole("button", { name: "Count" }));
    await user.type(screen.getByPlaceholderText(/scan it, or search/i), "9781638582359");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(api.lookup).toHaveBeenCalledWith("9781638582359"));
    expect(api.search).not.toHaveBeenCalled();
    expect(await screen.findByText("Spectrum book")).toBeDefined();
  });

  it("asks which one when a name matches several", async () => {
    // Silently taking the first is how the wrong product gets counted, and it
    // looks like it worked.
    api.search.mockResolvedValue({
      results: [product, { product_id: "p2", short_name: "Spectrum pens" }],
    });
    const user = open();

    await user.click(screen.getByRole("button", { name: "Count" }));
    await user.type(screen.getByPlaceholderText(/scan it, or search/i), "spectrum");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByRole("button", { name: "Spectrum pens" })).toBeDefined();
    // Nothing is on the list until a choice is made.
    expect(screen.queryByPlaceholderText("On the shelf")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Spectrum book" }));
    expect(await screen.findByPlaceholderText("On the shelf")).toBeDefined();
  });

  it("says so when a scanned code resolves to nothing", async () => {
    api.lookup.mockResolvedValue({ resolved: false, product: null });
    const user = open();

    await user.click(screen.getByRole("button", { name: "Adjust" }));
    await user.type(screen.getByPlaceholderText(/scan it, or search/i), "9999999999994");
    await user.click(screen.getByRole("button", { name: "Find" }));

    expect(await screen.findByText(/nothing found/i)).toBeDefined();
  });
});

describe("adjusting", () => {
  it("will not record a correction without a reason", async () => {
    api.search.mockResolvedValue({
      results: [{ product_id: "p1", short_name: "Soap", unit_price: { paise: 1, text: "" } }],
    });
    const user = open();

    await user.click(screen.getByRole("button", { name: "Adjust" }));
    await user.type(screen.getByPlaceholderText(/scan it, or search/i), "soap");
    await user.click(screen.getByRole("button", { name: "Find" }));

    const record = await screen.findByRole("button", { name: /record the adjustment/i });
    await user.type(screen.getByPlaceholderText("-2"), "-2");

    expect(record).toHaveProperty("disabled", true);

    await user.type(screen.getByPlaceholderText(/damaged in transit/i), "breakage");

    await waitFor(() => expect(record).toHaveProperty("disabled", false));
  });

  it("sends thousandths, signed", async () => {
    api.search.mockResolvedValue({
      results: [{ product_id: "p1", short_name: "Soap", unit_price: { paise: 1, text: "" } }],
    });
    api.adjust.mockResolvedValue({ movement_ids: ["m1"] });
    const user = open();

    await user.click(screen.getByRole("button", { name: "Adjust" }));
    await user.type(screen.getByPlaceholderText(/scan it, or search/i), "soap");
    await user.click(screen.getByRole("button", { name: "Find" }));
    await user.type(await screen.findByPlaceholderText("-2"), "-2");
    await user.type(screen.getByPlaceholderText(/damaged in transit/i), "breakage");
    await user.click(screen.getByRole("button", { name: /record the adjustment/i }));

    await waitFor(() =>
      expect(api.adjust).toHaveBeenCalledWith("p1", -2000, "breakage"),
    );
  });
});

describe("who may do what", () => {
  it("offers only the jobs this person holds a permission for", () => {
    // UX, not the control - the endpoint refuses them anyway. But a button
    // that only ever says no teaches someone that the till is broken.
    open(["stock.count"]);

    expect(screen.queryByRole("button", { name: "Receive" })).toBeNull();
    expect(screen.getByRole("button", { name: "Count" })).toBeDefined();
    expect(screen.queryByRole("button", { name: "Adjust" })).toBeNull();
  });
});

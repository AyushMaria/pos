import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Permission, SessionResponse } from "../../core/api/contract";
import { ApiError } from "../../core/api/client";

/**
 * Catalogue admin — phase 6 slice 6.
 *
 * Three things here are invisible to anyone reading the screen, and each of
 * them is the kind of mistake that looks fine in a demo:
 *
 *  1. Money is rupees on this screen and paise everywhere below it. The same
 *     class of boundary the stockroom owns for thousandths, with the same
 *     failure: a price a hundred times out looks perfectly ordinary.
 *  2. "No internet" and "not allowed" must not collapse into one message.
 *     One of them means try again later; the other never will.
 *  3. A duplicate barcode has to arrive as a sentence naming the product
 *     that holds the code.
 */

const api = {
  searchProducts: vi.fn(),
  product: vi.fn(),
  createProduct: vi.fn(),
  updateProduct: vi.fn(),
  barcodes: vi.fn(),
  addBarcode: vi.fn(),
  withdrawBarcode: vi.fn(),
  prices: vi.fn(),
  setPrice: vi.fn(),
  unknownScans: vi.fn(),
  resolveScan: vi.fn(),
  dismissScan: vi.fn(),
  lowStock: vi.fn(),
  stockLevel: vi.fn(),
  setReorderPoint: vi.fn(),
  taxCodes: vi.fn(),
  syncFailures: vi.fn(),
  retryFailures: vi.fn(),
};

vi.mock("../../core/api/admin", () => ({
  admin: {
    searchProducts: (...a: unknown[]) => api.searchProducts(...a),
    product: (...a: unknown[]) => api.product(...a),
    createProduct: (...a: unknown[]) => api.createProduct(...a),
    updateProduct: (...a: unknown[]) => api.updateProduct(...a),
    barcodes: (...a: unknown[]) => api.barcodes(...a),
    addBarcode: (...a: unknown[]) => api.addBarcode(...a),
    withdrawBarcode: (...a: unknown[]) => api.withdrawBarcode(...a),
    prices: (...a: unknown[]) => api.prices(...a),
    setPrice: (...a: unknown[]) => api.setPrice(...a),
    unknownScans: (...a: unknown[]) => api.unknownScans(...a),
    resolveScan: (...a: unknown[]) => api.resolveScan(...a),
    dismissScan: (...a: unknown[]) => api.dismissScan(...a),
    lowStock: (...a: unknown[]) => api.lowStock(...a),
    stockLevel: (...a: unknown[]) => api.stockLevel(...a),
    setReorderPoint: (...a: unknown[]) => api.setReorderPoint(...a),
  },
}));

vi.mock("../../core/api/register", () => ({
  catalog: { taxCodes: (...a: unknown[]) => api.taxCodes(...a) },
  sync: {
    failures: (...a: unknown[]) => api.syncFailures(...a),
    retryFailures: (...a: unknown[]) => api.retryFailures(...a),
  },
}));

const { AdminScreen } = await import("./AdminScreen");

const person = (permissions: Permission[]): SessionResponse =>
  ({
    employee_code: "M001",
    full_name: "Priya Nair",
    permissions,
  }) as SessionResponse;

const PRODUCT = {
  product_id: "p1",
  sku: "SKU-1004",
  name: "Tomatoes (loose)",
  short_name: "Tomato",
  uom: "kg",
  tax_code: "GST0",
  is_weighed: true,
  track_stock: true,
  is_active: true,
};

const SCAN = {
  scan_id: "s1",
  store_id: "st1",
  barcode: "8906110944741",
  scanned_at: "2026-09-01T10:00:00Z",
  resolved: false,
};

beforeEach(() => {
  vi.clearAllMocks();
  api.taxCodes.mockResolvedValue({ tax_codes: [] });
  api.barcodes.mockResolvedValue({ barcodes: [] });
  api.prices.mockResolvedValue({ prices: [] });
  api.unknownScans.mockResolvedValue({ scans: [] });
  api.lowStock.mockResolvedValue({ rows: [] });
  api.syncFailures.mockResolvedValue({ items: [] });
  api.stockLevel.mockResolvedValue({
    product_id: "p1", store_id: "st1", on_hand: 24_000, reorder_point: 0,
  });
});

async function openProduct(user: ReturnType<typeof userEvent.setup>) {
  api.searchProducts.mockResolvedValue({ products: [PRODUCT] });
  await user.type(screen.getByLabelText("Search the catalogue"), "tomato");
  await user.click(screen.getByRole("button", { name: "Search" }));
  await user.click(await screen.findByRole("button", { name: /SKU-1004/ }));
}

describe("what the screen owns alone", () => {
  it("takes a price in rupees and sends paise", async () => {
    const user = userEvent.setup();
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    api.setPrice.mockResolvedValue({ price: 4500 });
    await user.type(screen.getByLabelText("New price in rupees"), "45");
    await user.click(screen.getByRole("button", { name: "Set" }));

    await waitFor(() =>
      expect(api.setPrice).toHaveBeenCalledWith("p1", { price: 4500 }),
    );
  });

  it("rounds rather than truncating a price with paise in it", async () => {
    const user = userEvent.setup();
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    api.setPrice.mockResolvedValue({ price: 4599 });
    await user.type(screen.getByLabelText("New price in rupees"), "45.99");
    await user.click(screen.getByRole("button", { name: "Set" }));

    await waitFor(() =>
      expect(api.setPrice).toHaveBeenCalledWith("p1", { price: 4599 }),
    );
  });

  it("shows an existing price in rupees", async () => {
    const user = userEvent.setup();
    api.prices.mockResolvedValue({
      prices: [{ price: 12550, valid_to: null }],
    });
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    expect(await screen.findByText("₹125.50")).toBeInTheDocument();
  });
});

describe("offline is not the same as refused", () => {
  it("says the internet is what is missing", async () => {
    const user = userEvent.setup();
    api.searchProducts.mockRejectedValue(new ApiError(503, "no route to host"));
    render(<AdminScreen session={person(["product.read"])} onClose={() => {}} />);

    await user.type(screen.getByLabelText("Search the catalogue"), "tomato");
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText(/needs the internet/i)).toBeInTheDocument();
  });

  it("shows a refusal as itself, not as an outage", async () => {
    const user = userEvent.setup();
    api.searchProducts.mockRejectedValue(new ApiError(422, "no product was changed"));
    render(<AdminScreen session={person(["product.read"])} onClose={() => {}} />);

    await user.type(screen.getByLabelText("Search the catalogue"), "tomato");
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("no product was changed")).toBeInTheDocument();
    expect(screen.queryByText(/needs the internet/i)).not.toBeInTheDocument();
  });
});

describe("a duplicate barcode", () => {
  it("names the product already holding the code", async () => {
    const user = userEvent.setup();
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    api.addBarcode.mockRejectedValue(
      new ApiError(409, "8906110944741 is already on SKU-1004 — Tomatoes (loose)"),
    );
    await user.type(screen.getByLabelText("Add a barcode"), "8906110944741");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText(/already on SKU-1004/)).toBeInTheDocument();
  });
});

describe("who sees what", () => {
  it("does not offer the queue to someone who may not edit", () => {
    render(<AdminScreen session={person(["product.read"])} onClose={() => {}} />);
    expect(screen.queryByRole("button", { name: "Unknown scans" })).toBeNull();
    expect(screen.getByRole("button", { name: "Low stock" })).toBeInTheDocument();
  });

  it("tells someone with neither permission why the screen is empty", () => {
    render(<AdminScreen session={person([])} onClose={() => {}} />);
    expect(screen.getByText(/needs a permission this account does not have/)).toBeInTheDocument();
  });
});

describe("creating a product", () => {
  const GST5 = { code: "GST5", name: "5%", rate_bp: 500, is_inclusive: true };

  async function openCreator(user: ReturnType<typeof userEvent.setup>) {
    api.taxCodes.mockResolvedValue({ tax_codes: [GST5] });
    render(
      <AdminScreen
        session={person(["product.read", "product.edit", "product.create"])}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "New product" }));
  }

  it("sends what the form was given, with a blank short name as null", async () => {
    const user = userEvent.setup();
    await openCreator(user);

    api.createProduct.mockResolvedValue({ ...PRODUCT, product_id: "p9", sku: "SKU-9" });
    await user.type(screen.getByLabelText("SKU"), "SKU-9");
    await user.type(screen.getByLabelText("Product name"), "Parle-G 100g");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(api.createProduct).toHaveBeenCalledWith({
        sku: "SKU-9",
        name: "Parle-G 100g",
        short_name: null,
        uom: "each",
        tax_code: "GST5",
        is_weighed: false,
        track_stock: true,
      }),
    );
  });

  it("lands on the editor, because a new product cannot be sold yet", async () => {
    const user = userEvent.setup();
    await openCreator(user);

    api.createProduct.mockResolvedValue({ ...PRODUCT, product_id: "p9", sku: "SKU-9" });
    await user.type(screen.getByLabelText("SKU"), "SKU-9");
    await user.type(screen.getByLabelText("Product name"), "Parle-G 100g");
    await user.click(screen.getByRole("button", { name: "Create" }));

    // The barcode and price sub-editors are the two things standing between
    // this product and a sale, so the screen has to go there by itself.
    expect(await screen.findByLabelText("Add a barcode")).toBeInTheDocument();
    expect(screen.getByLabelText("New price in rupees")).toBeInTheDocument();
  });

  it("is not offered to someone who may edit but not create", () => {
    render(
      <AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />,
    );
    expect(screen.queryByRole("button", { name: "New product" })).toBeNull();
    expect(screen.getByLabelText("Search the catalogue")).toBeInTheDocument();
  });
});

describe("edits that were never saved", () => {
  // The first person to use this screen ticked "sold by weight", left, and
  // lost it: the product form saves on a button while every panel below it
  // saves on its own. The audit log showed no write had ever happened.
  it("will not offer to save a form nobody has touched", async () => {
    const user = userEvent.setup();
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("offers to save once something differs", async () => {
    const user = userEvent.setup();
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    await user.click(screen.getByLabelText(/Sold by weight/));
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });

  it("asks before the back link throws the edits away", async () => {
    const user = userEvent.setup();
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    await user.click(screen.getByLabelText(/Sold by weight/));
    await user.click(screen.getByRole("button", { name: "← All results" }));

    expect(screen.getByRole("alert")).toHaveTextContent("unsaved changes");
    // Still on the product: the click did not navigate.
    expect(screen.getByRole("button", { name: "Save changes" })).toBeInTheDocument();
  });

  it("asks before a tab throws them away too", async () => {
    const user = userEvent.setup();
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    await user.click(screen.getByLabelText(/Sold by weight/));
    await user.click(screen.getByRole("button", { name: "Low stock" }));

    expect(screen.getByRole("alert")).toHaveTextContent("unsaved changes");
  });

  it("leaves when the discard is explicit", async () => {
    const user = userEvent.setup();
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    await user.click(screen.getByLabelText(/Sold by weight/));
    await user.click(screen.getByRole("button", { name: "← All results" }));
    await user.click(screen.getByRole("button", { name: "Discard them" }));

    expect(await screen.findByLabelText("Search the catalogue")).toBeInTheDocument();
  });
});

describe("a reorder point is thousandths below the screen", () => {
  it("sends 5000 when a person types 5", async () => {
    const user = userEvent.setup();
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    api.setReorderPoint.mockResolvedValue({
      product_id: "p1", store_id: "st1", on_hand: 24_000, reorder_point: 5_000,
    });
    await user.type(await screen.findByLabelText("Reorder point"), "5");
    await user.click(screen.getByRole("button", { name: "Set reorder point" }));

    // It is compared against on_hand, which is a sum of delta_milli. Sending
    // 5 would mean "reorder below one two-hundredth of a packet", silently.
    await waitFor(() =>
      expect(api.setReorderPoint).toHaveBeenCalledWith("p1", { reorder_point: 5_000 }),
    );
  });

  it("shows the level back in things a person counts", async () => {
    const user = userEvent.setup();
    api.stockLevel.mockResolvedValue({
      product_id: "p1", store_id: "st1", on_hand: 24_000, reorder_point: 5_000,
    });
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    expect(await screen.findByText("5 kg")).toBeInTheDocument();
    expect(await screen.findByText(/24 kg on hand/)).toBeInTheDocument();
  });

  it("says to count the product in when it has no stock row", async () => {
    const user = userEvent.setup();
    api.stockLevel.mockRejectedValue(new ApiError(404, "no stock record"));
    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await openProduct(user);

    // The ordinary state of a product nobody has counted yet, not an error.
    expect(await screen.findByText(/Count this product in/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Reorder point")).not.toBeInTheDocument();
  });
});

describe("working the queue is what closes it", () => {
  it("attaches the scanned code to a new product, then closes the entry", async () => {
    const user = userEvent.setup();
    api.taxCodes.mockResolvedValue({
      tax_codes: [{ code: "GST5", name: "5%", rate_bp: 500, is_inclusive: true }],
    });
    api.unknownScans.mockResolvedValueOnce({ scans: [SCAN] });
    api.createProduct.mockResolvedValue({ ...PRODUCT, product_id: "p9" });
    api.addBarcode.mockResolvedValue({ barcode_id: "b1" });
    api.resolveScan.mockResolvedValue(undefined);
    api.unknownScans.mockResolvedValueOnce({ scans: [] });

    render(
      <AdminScreen
        session={person(["product.read", "product.edit", "product.create"])}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Unknown scans" }));
    await user.click(await screen.findByRole("button", { name: "New product" }));

    await user.type(screen.getByLabelText("SKU"), "SKU-9");
    await user.type(screen.getByLabelText("Product name"), "Parle-G 100g");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(api.addBarcode).toHaveBeenCalledWith("p9", { barcode: "8906110944741" }),
    );
    await waitFor(() => expect(api.resolveScan).toHaveBeenCalledWith("s1"));
  });

  it("points the code at a product that already exists", async () => {
    const user = userEvent.setup();
    api.unknownScans.mockResolvedValueOnce({ scans: [SCAN] });
    api.searchProducts.mockResolvedValue({ products: [PRODUCT] });
    api.addBarcode.mockResolvedValue({ barcode_id: "b1" });
    api.resolveScan.mockResolvedValue(undefined);
    api.unknownScans.mockResolvedValueOnce({ scans: [] });

    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Unknown scans" }));
    await user.click(await screen.findByRole("button", { name: "Existing product" }));

    await user.type(
      screen.getByLabelText("Search for the product this code belongs to"),
      "tomato",
    );
    await user.click(screen.getByRole("button", { name: "Search" }));
    await user.click(await screen.findByRole("button", { name: /SKU-1004/ }));

    await waitFor(() =>
      expect(api.addBarcode).toHaveBeenCalledWith("p1", { barcode: "8906110944741" }),
    );
    await waitFor(() => expect(api.resolveScan).toHaveBeenCalledWith("s1"));
  });

  it("offers both ways of closing an entry to someone who may edit", async () => {
    // Phase 7 slice 2 put "Existing product" and "Dismiss" behind
    // <PermissionGate permission="product.edit">, which is the key their
    // endpoints check. Today the Unknown scans tab needs that same key, so
    // the gate hides nothing — and that is precisely why it is worth a test
    // in this direction. A gate that is redundant now becomes load-bearing
    // the day the tab is opened up, and until then the only way it can fail
    // is by hiding a control from someone entitled to it.
    const user = userEvent.setup();
    api.unknownScans.mockResolvedValue({ scans: [SCAN] });

    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Unknown scans" }));

    expect(await screen.findByRole("button", { name: "Existing product" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeDefined();
  });

  it("leaves the entry open when the code turns out to be spoken for", async () => {
    const user = userEvent.setup();
    api.unknownScans.mockResolvedValue({ scans: [SCAN] });
    api.searchProducts.mockResolvedValue({ products: [PRODUCT] });
    api.addBarcode.mockRejectedValue(
      new ApiError(409, "8906110944741 is already on SKU-7 — Marie Gold"),
    );

    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Unknown scans" }));
    await user.click(await screen.findByRole("button", { name: "Existing product" }));

    await user.type(
      screen.getByLabelText("Search for the product this code belongs to"),
      "tomato",
    );
    await user.click(screen.getByRole("button", { name: "Search" }));
    await user.click(await screen.findByRole("button", { name: /SKU-1004/ }));

    // The work is not done, so the queue must not say it is. Closing here
    // would lose the only record that this code needs attention.
    expect(await screen.findByText(/already on SKU-7/)).toBeInTheDocument();
    expect(api.resolveScan).not.toHaveBeenCalled();
  });
});

describe("giving up on an entry is a different act from finishing it", () => {
  const ONE_SCAN = {
    scans: [
      {
        scan_id: "s1",
        store_id: "st1",
        barcode: "8906110944741",
        scanned_at: "2026-09-01T10:00:00Z",
        resolved: false,
      },
    ],
  };

  it("asks before it dismisses, and does nothing until the answer is yes", async () => {
    const user = userEvent.setup();
    api.unknownScans.mockResolvedValue(ONE_SCAN);

    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Unknown scans" }));
    await user.click(await screen.findByRole("button", { name: "Dismiss" }));

    // The press opened a question, not a request.
    expect(await screen.findByText(/nothing gets catalogued/)).toBeInTheDocument();
    expect(api.dismissScan).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Keep it open" }));
    expect(api.dismissScan).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  });

  it("dismisses through the endpoint that records it as a dismissal", async () => {
    const user = userEvent.setup();
    api.unknownScans.mockResolvedValueOnce(ONE_SCAN);
    api.dismissScan.mockResolvedValue(undefined);
    api.unknownScans.mockResolvedValueOnce({ scans: [] });

    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Unknown scans" }));
    await user.click(await screen.findByRole("button", { name: "Dismiss" }));
    await user.click(await screen.findByRole("button", { name: "Yes, dismiss it" }));

    await waitFor(() => expect(api.dismissScan).toHaveBeenCalledWith("s1"));
    // Not the resolve endpoint. Both close the entry; only one of them is a
    // claim that the code is now on a product, and 0021 checks that claim.
    expect(api.resolveScan).not.toHaveBeenCalled();
    expect(await screen.findByText(/Nothing waiting/)).toBeInTheDocument();
  });

  it("does not tell the manager that every scan found a product", async () => {
    const user = userEvent.setup();
    api.unknownScans.mockResolvedValue({ scans: [] });

    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Unknown scans" }));

    // The old copy asserted something the screen cannot know and that was
    // false for every entry closed with the button this one replaced.
    expect(await screen.findByText(/Nothing waiting/)).toBeInTheDocument();
    expect(screen.queryByText(/found a product/)).not.toBeInTheDocument();
  });
});

describe("the failures list the sync indicator has always named", () => {
  const REFUSED = {
    items: [
      {
        id: 9,
        outbox_id: 31,
        entity: "sale",
        reference: "ST01-T1-000008",
        error: "HTTP 400: unknown entity override",
        failed_at: "2026-09-19T12:55:49+00:00",
      },
      {
        id: 10,
        outbox_id: 32,
        entity: "stock_movement",
        reference: null,
        error: "HTTP 400: nope",
        failed_at: "2026-09-19T12:56:00+00:00",
      },
    ],
  };

  it("is absent for someone who may not read refused sales", () => {
    render(<AdminScreen session={person(["product.read"])} onClose={() => {}} />);
    expect(screen.queryByRole("button", { name: "Sync failures" })).toBeNull();
  });

  it("shows a refused sale by its receipt number, and says when a row has none", async () => {
    const user = userEvent.setup();
    api.syncFailures.mockResolvedValue(REFUSED);

    render(
      <AdminScreen
        session={person(["product.read", "report.sales.store"])}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Sync failures" }));

    expect(await screen.findByText("ST01-T1-000008")).toBeInTheDocument();
    expect(screen.getByText(/unknown entity override/)).toBeInTheDocument();
    // The movement carries no receipt number. A sentence, not a blank cell.
    expect(screen.getByText("no reference on this row")).toBeInTheDocument();
  });

  it("requeues everything on Try again and reports how many", async () => {
    const user = userEvent.setup();
    api.syncFailures.mockResolvedValueOnce(REFUSED);
    api.retryFailures.mockResolvedValue({ requeued: 2, status: {} });
    api.syncFailures.mockResolvedValueOnce({ items: [] });

    render(
      <AdminScreen
        session={person(["product.read", "report.sales.store"])}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Sync failures" }));
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    await waitFor(() => expect(api.retryFailures).toHaveBeenCalled());
    expect(await screen.findByText("2 put back in the queue.")).toBeInTheDocument();
    expect(await screen.findByText(/Nothing refused/)).toBeInTheDocument();
  });
});

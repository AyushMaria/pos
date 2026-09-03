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
  updateProduct: vi.fn(),
  barcodes: vi.fn(),
  addBarcode: vi.fn(),
  withdrawBarcode: vi.fn(),
  prices: vi.fn(),
  setPrice: vi.fn(),
  unknownScans: vi.fn(),
  resolveScan: vi.fn(),
  lowStock: vi.fn(),
  taxCodes: vi.fn(),
};

vi.mock("../../core/api/admin", () => ({
  admin: {
    searchProducts: (...a: unknown[]) => api.searchProducts(...a),
    product: (...a: unknown[]) => api.product(...a),
    updateProduct: (...a: unknown[]) => api.updateProduct(...a),
    barcodes: (...a: unknown[]) => api.barcodes(...a),
    addBarcode: (...a: unknown[]) => api.addBarcode(...a),
    withdrawBarcode: (...a: unknown[]) => api.withdrawBarcode(...a),
    prices: (...a: unknown[]) => api.prices(...a),
    setPrice: (...a: unknown[]) => api.setPrice(...a),
    unknownScans: (...a: unknown[]) => api.unknownScans(...a),
    resolveScan: (...a: unknown[]) => api.resolveScan(...a),
    lowStock: (...a: unknown[]) => api.lowStock(...a),
  },
}));

vi.mock("../../core/api/register", () => ({
  catalog: { taxCodes: (...a: unknown[]) => api.taxCodes(...a) },
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

beforeEach(() => {
  vi.clearAllMocks();
  api.taxCodes.mockResolvedValue({ tax_codes: [] });
  api.barcodes.mockResolvedValue({ barcodes: [] });
  api.prices.mockResolvedValue({ prices: [] });
  api.unknownScans.mockResolvedValue({ scans: [] });
  api.lowStock.mockResolvedValue({ rows: [] });
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

describe("the unknown-scan queue", () => {
  it("closes an entry and reloads what is left", async () => {
    const user = userEvent.setup();
    api.unknownScans.mockResolvedValueOnce({
      scans: [
        {
          scan_id: "s1",
          store_id: "st1",
          barcode: "8906110944741",
          scanned_at: "2026-09-01T10:00:00Z",
          resolved: false,
        },
      ],
    });
    api.resolveScan.mockResolvedValue(undefined);
    api.unknownScans.mockResolvedValueOnce({ scans: [] });

    render(<AdminScreen session={person(["product.read", "product.edit"])} onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Unknown scans" }));
    await user.click(await screen.findByRole("button", { name: "Done" }));

    await waitFor(() => expect(api.resolveScan).toHaveBeenCalledWith("s1"));
    expect(await screen.findByText(/Nothing waiting/)).toBeInTheDocument();
  });
});

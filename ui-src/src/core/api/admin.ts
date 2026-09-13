import { request } from "./client";
import type {
  AdminBarcodeOut,
  AdminPriceOut,
  AdminProductOut,
  AdminProductsResponse,
  BarcodeAddRequest,
  BarcodesResponse,
  LowStockResponse,
  PriceSetRequest,
  PricesResponse,
  ReorderPointSetRequest,
  StockLevelOut,
  ProductCreateRequest,
  ProductUpdateRequest,
  UnknownScansResponse,
} from "./contract";

/**
 * Catalogue admin.
 *
 * Everything here reaches the cloud and every call can therefore fail with
 * 503 in a way nothing else in this UI can. `ApiError.isUnavailable` is
 * already true for that status, so callers distinguish "no internet" from
 * "not allowed" without inspecting numbers.
 *
 * Editing the catalogue offline is not a degraded mode, it is not a mode
 * (phase 6 decision 2). Selling something unrecognised is the part that works
 * without the internet, and that lives in the register.
 */
export const admin = {
  searchProducts: (q: string, limit = 25) =>
    request<AdminProductsResponse>(
      `/admin/products?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),

  product: (productId: string) =>
    request<AdminProductOut>(`/admin/products/${productId}`),

  createProduct: (body: ProductCreateRequest) =>
    request<AdminProductOut>("/admin/products", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateProduct: (productId: string, body: ProductUpdateRequest) =>
    request<AdminProductOut>(`/admin/products/${productId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  barcodes: (productId: string) =>
    request<BarcodesResponse>(`/admin/products/${productId}/barcodes`),

  addBarcode: (productId: string, body: BarcodeAddRequest) =>
    request<AdminBarcodeOut>(`/admin/products/${productId}/barcodes`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Soft delete server-side: the code frees up, the history still resolves. */
  withdrawBarcode: (barcodeId: string) =>
    request<void>(`/admin/barcodes/${barcodeId}`, { method: "DELETE" }),

  prices: (productId: string) =>
    request<PricesResponse>(`/admin/products/${productId}/prices`),

  /** Paise. The store is taken from the session, never sent from here. */
  setPrice: (productId: string, body: PriceSetRequest) =>
    request<AdminPriceOut>(`/admin/products/${productId}/price`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  stockLevel: (productId: string) =>
    request<StockLevelOut>(`/admin/products/${productId}/stock-level`),

  /** Thousandths. The screen converts; nothing below it does. */
  setReorderPoint: (productId: string, body: ReorderPointSetRequest) =>
    request<StockLevelOut>(`/admin/products/${productId}/reorder-point`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  unknownScans: (resolved = false) =>
    request<UnknownScansResponse>(`/admin/unknown-scans?resolved=${resolved}`),

  resolveScan: (scanId: string) =>
    request<void>(`/admin/unknown-scans/${scanId}/resolve`, { method: "POST" }),

  lowStock: () => request<LowStockResponse>("/admin/low-stock"),
};

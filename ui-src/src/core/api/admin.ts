import { request } from "./client";
import type {
  AdminBarcodeOut,
  AdminPriceOut,
  AdminProductOut,
  AdminProductsResponse,
  AuditLogResponse,
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

  /**
   * The audit log — who did that, and who authorised it.
   *
   * Read-only and gated on `user.manage`, which this is the only caller of.
   * `actions` comes back with the entries so the filter offers what the log
   * actually contains rather than a menu of constants that can go stale.
   */
  audit: (filters: {
    since?: string;
    until?: string;
    action?: string;
    entityId?: string;
  } = {}) => {
    const query = new URLSearchParams();
    if (filters.since) query.set("since", filters.since);
    if (filters.until) query.set("until", filters.until);
    if (filters.action) query.set("action", filters.action);
    if (filters.entityId) query.set("entity_id", filters.entityId);
    const suffix = query.toString();
    return request<AuditLogResponse>(`/admin/audit${suffix ? `?${suffix}` : ""}`);
  },

  unknownScans: (resolved = false) =>
    request<UnknownScansResponse>(`/admin/unknown-scans?resolved=${resolved}`),

  /**
   * Close an entry because the code is now on a product.
   *
   * Only correct *after* `addBarcode` has landed: 0021 refuses this if the
   * barcode is still on nothing, which is the whole point of it. A 422 here
   * means the attach never happened and the entry is rightly still open.
   */
  resolveScan: (scanId: string) =>
    request<void>(`/admin/unknown-scans/${scanId}/resolve`, { method: "POST" }),

  /** Close an entry that is never going to be a product. Audited apart. */
  dismissScan: (scanId: string) =>
    request<void>(`/admin/unknown-scans/${scanId}/dismiss`, { method: "POST" }),

  lowStock: () => request<LowStockResponse>("/admin/low-stock"),
};

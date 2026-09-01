import { api, request } from "./client";
import type {
  CartOut,
  LookupResponse,
  MovementsResponse,
  PostSaleResponse,
  ReceiptLineOut,
  SearchResponse,
  SyncFailuresResponse,
  SyncRetryResponse,
  SyncStatusResponse,
  TaxCodesResponse,
  TenderQuote,
  TenderResponse,
} from "./contract";

/**
 * Register commands.
 *
 * Every one of these returns the fully recalculated basket. The UI renders
 * what it is handed and never adds anything up (architecture §1.2, §4) — so
 * there is no client-side total to drift from the server's.
 */
export const register = {
  openCart: () => request<CartOut>("/register/carts", { method: "POST" }),

  readCart: (cartId: string) => request<CartOut>(`/register/carts/${cartId}`),

  addBarcode: (cartId: string, barcode: string, qtyMilli?: number) =>
    request<CartOut>(`/register/carts/${cartId}/lines`, {
      method: "POST",
      body: JSON.stringify({ barcode, qty_milli: qtyMilli ?? null }),
    }),

  addProduct: (cartId: string, productId: string, qtyMilli?: number) =>
    request<CartOut>(`/register/carts/${cartId}/lines`, {
      method: "POST",
      body: JSON.stringify({ product_id: productId, qty_milli: qtyMilli ?? null }),
    }),

  /** Sell something the catalogue does not have. The line goes against the
   *  placeholder product; the identity travels on the line itself. */
  addUnlisted: (
    cartId: string,
    item: {
      description: string;
      unit_price_paise: number;
      tax_code: string;
      barcode?: string | null;
      qty_milli?: number | null;
    },
  ) =>
    request<CartOut>(`/register/carts/${cartId}/lines/unlisted`, {
      method: "POST",
      body: JSON.stringify({
        description: item.description,
        unit_price_paise: item.unit_price_paise,
        tax_code: item.tax_code,
        barcode: item.barcode ?? null,
        qty_milli: item.qty_milli ?? null,
      }),
    }),

  setQuantity: (cartId: string, lineNo: number, qtyMilli: number) =>
    request<CartOut>(`/register/carts/${cartId}/lines/${lineNo}`, {
      method: "PATCH",
      body: JSON.stringify({ qty_milli: qtyMilli }),
    }),

  voidLine: (cartId: string, lineNo: number) =>
    request<CartOut>(`/register/carts/${cartId}/lines/${lineNo}`, {
      method: "DELETE",
    }),

  abandon: (cartId: string) =>
    request<void>(`/register/carts/${cartId}`, { method: "DELETE" }),

  tenderQuote: (cartId: string, method: string) =>
    request<TenderQuote>(
      `/register/carts/${cartId}/tender-quote?method=${encodeURIComponent(method)}`,
    ),

  takePayment: (cartId: string, method: string, tenderedPaise?: number) =>
    request<TenderResponse>(`/register/carts/${cartId}/payments`, {
      method: "POST",
      body: JSON.stringify({
        method,
        tendered_paise: tenderedPaise ?? null,
      }),
    }),

  /**
   * The cashier heard the soundbox — architecture §13.3.
   *
   * `amountPaise` is what actually arrived. It is sent explicitly rather than
   * left to default, because on a printed counter QR the customer types the
   * figure into their own app and can type it wrong in either direction.
   */
  attest: (attemptId: string, amountPaise?: number, reference?: string) =>
    request<TenderResponse>(`/register/payments/${attemptId}/confirm`, {
      method: "POST",
      body: JSON.stringify({
        amount_paise: amountPaise ?? null,
        reference: reference?.trim() || null,
      }),
    }),

  /** The cashier cannot tell. Posts the sale for a supervisor instead. */
  markUnknown: (attemptId: string, reason?: string) =>
    request<TenderResponse>(`/register/payments/${attemptId}/unknown`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    }),

  cancelPayment: (attemptId: string) =>
    request<TenderResponse>(`/register/payments/${attemptId}/cancel`, {
      method: "POST",
    }),

  post: (cartId: string) =>
    request<PostSaleResponse>(`/register/carts/${cartId}/post`, { method: "POST" }),

  writePdf: (saleId: string) =>
    request<{ path: string }>(`/register/sales/${saleId}/receipt.pdf`, {
      method: "POST",
    }),
};

/**
 * Sync — architecture §9.
 *
 * Read-only from the register's point of view, apart from `pushNow`, which
 * exists for the person who has just plugged the network back in and would
 * rather watch the backlog empty than trust that it will.
 */
export const sync = {
  status: () => request<SyncStatusResponse>("/sync/status"),

  pushNow: () => request<SyncStatusResponse>("/sync/push", { method: "POST" }),

  failures: () => request<SyncFailuresResponse>("/sync/failures"),

  /**
   * Put quarantined sales back in the queue and try them now.
   *
   * Manager-only, and deliberately manual: a sale the cloud keeps refusing
   * should keep being refused visibly, not cycle through the queue every
   * ninety seconds burying the reason. Someone fixes the cause and says so.
   */
  retryFailures: (failureIds?: number[]) =>
    request<SyncRetryResponse>("/sync/failures/retry", {
      method: "POST",
      body: JSON.stringify({ failure_ids: failureIds ?? null }),
    }),
};

/**
 * Inventory - architecture 9.4.
 *
 * Receiving, counting and adjusting. Each is gated on its own permission at
 * the API; the screens hide what a signed-in person may not do, which is UX
 * rather than the control (11.1).
 */
export const inventory = {
  /** What scanning this code that many times would receive. Writes nothing. */
  previewLine: (barcode: string, packs: number) =>
    request<ReceiptLineOut>("/inventory/receipts/preview", {
      method: "POST",
      body: JSON.stringify({ barcode, packs }),
    }),

  /** Commit a delivery. One transaction - all lines or none. */
  receive: (lines: { barcode: string; packs: number }[]) =>
    request<MovementsResponse>("/inventory/receipts", {
      method: "POST",
      body: JSON.stringify({ lines }),
    }),

  /** A count writes the difference, never the count. */
  count: (lines: { product_id: string; counted_milli: number }[]) =>
    request<MovementsResponse>("/inventory/counts", {
      method: "POST",
      body: JSON.stringify({ lines }),
    }),

  /** The only movement with no document behind it, so the note is required. */
  adjust: (productId: string, deltaMilli: number, note: string) =>
    request<MovementsResponse>("/inventory/adjustments", {
      method: "POST",
      body: JSON.stringify({
        product_id: productId,
        delta_milli: deltaMilli,
        note,
      }),
    }),
};

export const catalog = {
  lookup: (barcode: string) =>
    request<LookupResponse>(`/catalog/lookup?barcode=${encodeURIComponent(barcode)}`),

  search: (q: string) =>
    request<SearchResponse>(`/catalog/search?q=${encodeURIComponent(q)}&limit=25`),

  /** GST slabs, for the unlisted-item form. A rate the client invented would
   *  be a tax bill the client invented, so the codes come from the server. */
  taxCodes: () => request<TaxCodesResponse>("/catalog/tax-codes"),
};

export { api };

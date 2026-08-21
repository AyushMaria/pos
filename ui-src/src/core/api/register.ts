import { api, request } from "./client";
import type {
  CartOut,
  LookupResponse,
  PostSaleResponse,
  SearchResponse,
  SyncFailuresResponse,
  SyncStatusResponse,
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
};

export const catalog = {
  lookup: (barcode: string) =>
    request<LookupResponse>(`/catalog/lookup?barcode=${encodeURIComponent(barcode)}`),

  search: (q: string) =>
    request<SearchResponse>(`/catalog/search?q=${encodeURIComponent(q)}&limit=25`),
};

export { api };

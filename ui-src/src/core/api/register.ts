import { api, request } from "./client";
import type {
  CartOut,
  LookupResponse,
  PostSaleResponse,
  SearchResponse,
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

  post: (cartId: string) =>
    request<PostSaleResponse>(`/register/carts/${cartId}/post`, { method: "POST" }),

  writePdf: (saleId: string) =>
    request<{ path: string }>(`/register/sales/${saleId}/receipt.pdf`, {
      method: "POST",
    }),
};

export const catalog = {
  lookup: (barcode: string) =>
    request<LookupResponse>(`/catalog/lookup?barcode=${encodeURIComponent(barcode)}`),

  search: (q: string) =>
    request<SearchResponse>(`/catalog/search?q=${encodeURIComponent(q)}&limit=25`),
};

export { api };

// GENERATED FILE, DO NOT EDIT BY HAND.
//
// Sources: app/api/schemas.py (via OpenAPI) and app/domain/permissions.py.
// Regenerate with:  python scripts/gen_ts_types.py
// CI fails if this file and the Python contract disagree.

export interface AddLineRequest {
  barcode?: string | null;
  product_id?: string | null;
  /** Integer thousandths; 1.250 kg is 1250 */
  qty_milli?: number | null;
}
export interface AttemptOut {
  attempt_id: string;
  method: string;
  state: string;
  amount: MoneyOut;
  reference?: string | null;
  expires_at?: string | null;
  is_pending: boolean;
}
/** What the cashier saw on the merchant phone or soundbox. */
export interface AttestRequest {
  /** What actually arrived. Defaults to the amount asked for. The customer types the figure into their own app on a printed counter QR, so it can differ in either direction. */
  amount_paise?: number | null;
  /** The UTR. With a static QR this is the only identifier the bank statement and this sale have in common — worth capturing. */
  reference?: string | null;
}
export interface CartLineOut {
  line_no: number;
  product_id: string;
  description: string;
  qty_milli: number;
  quantity_text: string;
  unit_price: MoneyOut;
  discount: MoneyOut;
  tax_amount: MoneyOut;
  line_total: MoneyOut;
}
export interface CartOut {
  cart_id: string;
  lines: CartLineOut[];
  item_count: number;
  subtotal: MoneyOut;
  discount_total: MoneyOut;
  tax_total: MoneyOut;
  total_before_rounding: MoneyOut;
  tax_breakdown: TaxComponentOut[];
  paid: MoneyOut;
  outstanding: MoneyOut;
  rounding_adjustment: MoneyOut;
  settled: boolean;
  /** True once a payment is approved; the basket cannot change */
  locked?: boolean;
}
export interface ChangeQuantityRequest {
  qty_milli: number;
}
/** The startup gate the splash polls. Unauthenticated by design. */
export interface HealthResponse {
  /** 'ready' once migrations have been applied */
  status: string;
  /** Application version */
  version: string;
  /** Applied SQLite user_version */
  schema_version: number;
  /** Schema version this build ships */
  expected_schema_version: number;
  migrations_pending: boolean;
  store_code: string;
  terminal_code: string;
  /** False when no Supabase project is configured (offline dev) */
  cloud_configured: boolean;
}
export interface LoginRequest {
  employee_code: string;
  pin: string;
}
/** A parsed scan. `product` is null when the code resolved to nothing. */
export interface LookupResponse {
  raw: string;
  symbology: string;
  resolved: boolean;
  product?: ProductOut | null;
  qty_milli?: number | null;
  is_case?: boolean;
  error?: string | null;
}
/** An amount, as both the canonical integer and something to display.

`paise` is the truth; `text` is pre-formatted so the UI never reimplements
currency formatting and cannot drift from what the receipt prints.
Formatting is presentation, not math — React still does no arithmetic
(architecture §1.2). */
export interface MoneyOut {
  paise: number;
  text: string;
}
export interface PostSaleResponse {
  sale_id: string;
  receipt_no: string;
  status: string;
  grand_total: MoneyOut;
  rounding_adjustment: MoneyOut;
  change_due: MoneyOut;
  receipt_html: string;
  receipt_text: string;
}
export interface ProductOut {
  product_id: string;
  sku: string;
  name: string;
  short_name: string;
  uom: string;
  is_weighed: boolean;
  tax_code: string;
  rate_bp: number;
  unit_price: MoneyOut;
  pack_size?: number;
  barcode?: string | null;
}
export interface ReceiptPdfResponse {
  sale_id: string;
  receipt_no: string;
  path: string;
}
export interface ResolveReviewRequest {
  /** 'paid' if the money was there after all, 'not_paid' if it never arrived. Never a bare 'resolved': a variance nobody can name is one nobody can act on at shift close. */
  outcome: string;
  note?: string | null;
}
export interface ResolveReviewResponse {
  sale_id: string;
  outcome: string;
  resolved_at: string;
}
/** A sale posted as `requires_review`, awaiting a supervisor. */
export interface ReviewItemOut {
  sale_id: string;
  receipt_no: string;
  grand_total: MoneyOut;
  disputed_amount: MoneyOut;
  posted_at: string;
}
export interface ReviewQueueResponse {
  items: ReviewItemOut[];
}
export interface SearchResponse {
  query: string;
  results: ProductOut[];
}
export interface SessionResponse {
  user_id: string;
  employee_code: string;
  full_name: string;
  store_id: string;
  roles: Role[];
  /** Advisory. Postgres RLS is the security boundary. */
  permissions: Permission[];
  authenticated_at: string;
  /** True when authenticated against the local cache */
  offline: boolean;
}
export interface TaxComponentOut {
  tax_code: string;
  rate_bp: number;
  taxable_base: MoneyOut;
  tax_amount: MoneyOut;
  cgst: MoneyOut;
  sgst: MoneyOut;
}
/** What a method would collect, before the cashier commits to it. */
export interface TenderQuote {
  method: string;
  outstanding: MoneyOut;
  collected: MoneyOut;
  rounding_adjustment: MoneyOut;
}
export interface TenderRequest {
  /** cash or upi */
  method: string;
  /** Cash handed over, when more than is owed */
  tendered_paise?: number | null;
}
export interface TenderResponse {
  attempt_id: string;
  state: string;
  cart: CartOut;
  change_due: MoneyOut;
  expires_at?: string | null;
}
export interface UnknownPaymentRequest {
  reason?: string | null;
}

/**
 * Every permission in the matrix (architecture §11.1).
 *
 * Client-side checks are UX only — Postgres RLS is the security boundary.
 * A <PermissionGate> hiding a button is a courtesy to the cashier, not a
 * control.
 */
export type Permission =
  | "cash.payout"
  | "payment.attest"
  | "price.override"
  | "product.create"
  | "product.edit"
  | "product.read"
  | "report.margin"
  | "report.sales.store"
  | "sale.create"
  | "sale.discount.line"
  | "sale.discount.unlimited"
  | "sale.refund"
  | "sale.review.resolve"
  | "sale.void"
  | "settings.manage"
  | "shift.close"
  | "stock.adjust"
  | "stock.count"
  | "stock.receive"
  | "user.manage";

export type Role = "cashier" | "supervisor" | "inventory" | "manager" | "admin";

export const PERMISSIONS: readonly Permission[] = [
  "cash.payout",
  "payment.attest",
  "price.override",
  "product.create",
  "product.edit",
  "product.read",
  "report.margin",
  "report.sales.store",
  "sale.create",
  "sale.discount.line",
  "sale.discount.unlimited",
  "sale.refund",
  "sale.review.resolve",
  "sale.void",
  "settings.manage",
  "shift.close",
  "stock.adjust",
  "stock.count",
  "stock.receive",
  "user.manage",
] as const;

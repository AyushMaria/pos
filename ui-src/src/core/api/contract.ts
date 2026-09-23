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
export interface AdjustmentRequest {
  product_id: string;
  delta_milli: number;
  note: string;
}
export interface AdminBarcodeOut {
  barcode_id: string;
  product_id: string;
  barcode: string;
  symbology: string;
  pack_size: number;
  is_primary: boolean;
}
export interface AdminPriceOut {
  price_id: string;
  product_id: string;
  store_id: string;
  price: number;
  valid_from: string;
  valid_to?: string | null;
}
export interface AdminProductOut {
  product_id: string;
  sku: string;
  name: string;
  short_name?: string | null;
  uom: string;
  tax_code: string;
  is_weighed: boolean;
  track_stock: boolean;
  is_active: boolean;
}
export interface AdminProductsResponse {
  products: AdminProductOut[];
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
/** One audited act, named by the two people who could have been involved.

`approver_code` is set only for an override. It is the field the whole
audit log exists for: an escalation that names one person reads as
ordinary work. */
export interface AuditEntryOut {
  id: string;
  action: string;
  entity?: string | null;
  entity_id?: string | null;
  store_id?: string | null;
  occurred_at: string;
  /** Set whenever a person did this, even if their name cannot be read */
  actor_id?: string | null;
  actor_code?: string | null;
  actor_name?: string | null;
  approver_id?: string | null;
  approver_code?: string | null;
  approver_name?: string | null;
  before?: unknown | null;
  after?: unknown | null;
}
export interface AuditLogResponse {
  entries: AuditEntryOut[];
  /** Every action present in this store's log, for the filter */
  actions?: string[];
}
export interface BarcodeAddRequest {
  barcode: string;
  pack_size?: number;
}
export interface BarcodesResponse {
  barcodes: AdminBarcodeOut[];
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
export interface CashMovementRequest {
  /** in or out */
  direction: string;
  amount_paise: number;
  reason: string;
}
export interface CashMovementResponse {
  movement_id: string;
}
export interface ChangeQuantityRequest {
  qty_milli: number;
}
export interface CheckLineOut {
  figure: string;
  till: number;
  cloud: number;
  agrees: boolean;
}
/** The till's figures beside the cloud's, and what the difference means. */
export interface CloseCheckResponse {
  session_id: string;
  agrees: boolean;
  lines: CheckLineOut[];
  explanation: string;
}
export interface CloseShiftRequest {
  counted_cash_paise: number;
  note?: string | null;
}
export interface CloseShiftResponse {
  close_id: string;
  shift: ShiftOut;
  figures: ShiftFiguresOut;
  zreport_html: string;
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
/** Money off one line, in paise, never a percentage as a fraction.

The first act in the application gated on an overridable permission. A
cashier cannot do it; a supervisor can lend them ninety seconds in which
they can. */
export interface LineDiscountRequest {
  /** How much to take off this line */
  amount_paise: number;
  reason?: string;
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
export interface LowStockOut {
  product_id: string;
  sku: string;
  name: string;
  uom: string;
  on_hand: number;
  reorder_point: number;
}
/** At or under the reorder point. A query, not an alert. */
export interface LowStockResponse {
  rows: LowStockOut[];
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
/** The ledger rows written. Empty when a count matched everywhere. */
export interface MovementsResponse {
  movement_ids: string[];
}
export interface OpenShiftRequest {
  /** What is in the drawer before the first sale */
  opening_float_paise: number;
}
/** Who is authorising, with what, and for which permission.

No `actor` field: the cashier being lent the permission is whoever is
signed in at this terminal, read from the session rather than the body.
A request that could name its own beneficiary would let a caller mint a
grant for somebody who is not standing there. */
export interface OverrideRequest {
  approver_code: string;
  pin: string;
  /** One of app.domain.permissions.OVERRIDABLE */
  permission: string;
}
/** The grant, as the modal needs to see it.

`expires_in_seconds` is redundant with `expires_at` and is here anyway:
the UI counts down, and a countdown computed from two clocks is a
countdown that can start at a negative number. The service owns the
window, so the service says how much of it is left. */
export interface OverrideResponse {
  permission: string;
  granted_at: string;
  expires_at: string;
  expires_in_seconds: number;
  approver_code: string;
  /** The cashier the permission was lent to */
  actor_code: string;
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
export interface PriceSetRequest {
  price: number;
}
export interface PricesResponse {
  prices: AdminPriceOut[];
}
export interface ProductCreateRequest {
  sku: string;
  name: string;
  short_name?: string | null;
  uom?: string;
  tax_code: string;
  is_weighed?: boolean;
  track_stock?: boolean;
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
/** Every field optional: a PATCH touches only what it names. */
export interface ProductUpdateRequest {
  name?: string | null;
  short_name?: string | null;
  uom?: string | null;
  tax_code?: string | null;
  is_weighed?: boolean | null;
  track_stock?: boolean | null;
  is_active?: boolean | null;
}
/** What that scan would receive, for the running list on the screen. */
export interface ReceiptLineOut {
  product_id: string;
  description: string;
  barcode: string;
  packs: number;
  pack_size: number;
  units: number;
  delta_milli: number;
}
export interface ReceiptPdfResponse {
  sale_id: string;
  receipt_no: string;
  path: string;
}
/** One scan of a delivery, before it is committed. */
export interface ReceiptPreviewRequest {
  barcode: string;
  packs: number;
}
export interface ReceiptRequest {
  lines: ReceiptPreviewRequest[];
}
/** Thousandths, like every other quantity below the screen.

It is compared directly against `stock_levels.on_hand`, which is a sum of
`delta_milli`, so the two must be the same unit or the comparison is
nonsense in a way nothing would report. One packet is 1000. The screen
converts, exactly as the stockroom does. */
export interface ReorderPointSetRequest {
  reorder_point: number;
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
/** The Z-report's numbers. Every line the close screen shows.

`expected_cash` is absent on an X-report by design: a supervisor who sees
the expected figure before counting types the expected figure. */
export interface ShiftFiguresOut {
  cash_sales: MoneyOut;
  upi_attested: MoneyOut;
  upi_verified: MoneyOut;
  cash_in: MoneyOut;
  cash_out: MoneyOut;
  rounding: MoneyOut;
  takings: MoneyOut;
  under_review_count: number;
  under_review_total: MoneyOut;
  sales_count: number;
  expected_cash?: MoneyOut | null;
  counted_cash?: MoneyOut | null;
  variance?: MoneyOut | null;
  under_review_receipts?: string[];
}
export interface ShiftOut {
  id: string;
  opened_at: string;
  opened_by: string;
  opening_float: MoneyOut;
}
export interface StockCountLine {
  product_id: string;
  counted_milli: number;
}
export interface StockCountRequest {
  lines: StockCountLine[];
}
/** A product's level after a write. Both quantities are thousandths. */
export interface StockLevelOut {
  product_id: string;
  store_id: string;
  on_hand: number;
  reorder_point: number;
}
export interface SyncFailureOut {
  id: number;
  outbox_id?: number | null;
  entity?: string | null;
  reference?: string | null;
  error: string;
  failed_at: string;
}
export interface SyncFailuresResponse {
  items: SyncFailureOut[];
}
export interface SyncRetryRequest {
  failure_ids?: number[] | null;
}
export interface SyncRetryResponse {
  requeued: number;
  status: SyncStatusResponse;
}
export interface SyncStatusResponse {
  online: boolean;
  backlog: number;
  oldest_pending_at?: string | null;
  failures?: number;
  last_push_at?: string | null;
  last_pull_at?: string | null;
  last_error?: string | null;
  needs_update?: boolean;
  needs_signin?: boolean;
}
/** A rate the quick-create form may offer. */
export interface TaxCodeOut {
  code: string;
  name: string;
  /** Basis points; 18% is 1800 */
  rate_bp: number;
  is_inclusive: boolean;
}
export interface TaxCodesResponse {
  tax_codes: TaxCodeOut[];
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
export interface UnknownScanOut {
  scan_id: string;
  store_id: string;
  barcode: string;
  scanned_at: string;
  terminal_id?: string | null;
  resolved: boolean;
  resolution?: string | null;
}
export interface UnknownScansResponse {
  scans: UnknownScanOut[];
}
/** Sell an item the catalogue does not have (architecture 9.2, phase 6).

No `product_id`: the line goes against the one placeholder product and
carries its real identity itself. `tax_code` is a key into `tax_codes`,
never a rate — a rate the client chose is a tax bill the client chose. */
export interface UnlistedLineRequest {
  /** What the cashier read off the packet */
  description: string;
  /** Integer paise, never a float */
  unit_price_paise: number;
  tax_code: string;
  /** The code that matched nothing, if there was one */
  barcode?: string | null;
  /** Integer thousandths; 1.250 kg is 1250 */
  qty_milli?: number | null;
}
export interface XReportResponse {
  shift: ShiftOut;
  figures: ShiftFiguresOut;
}
export interface ZReportPdfResponse {
  close_id: string;
  path: string;
}
export interface ZReportResponse {
  close_id: string;
  session_id: string;
  closed_at: string;
  figures: ShiftFiguresOut;
  zreport_html: string;
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

/**
 * The matrix itself, so the UI can be tested against every role without a
 * second copy of §11.1 to keep in step.
 *
 * Not for deciding anything at runtime: a session carries the permissions the
 * server gave it, and that is what <PermissionGate> reads. This exists so
 * `permissionMatrix.test.tsx` can assert all twenty keys against all five
 * roles from the same source the other two layers use.
 */
export const ROLE_PERMISSIONS: Readonly<Record<Role, readonly Permission[]>> = {
  "cashier": ["payment.attest", "product.read", "sale.create"],
  "supervisor": ["cash.payout", "payment.attest", "price.override", "product.read", "report.sales.store", "sale.create", "sale.discount.line", "sale.refund", "sale.review.resolve", "sale.void", "shift.close"],
  "inventory": ["product.create", "product.edit", "product.read", "stock.count", "stock.receive"],
  "manager": ["cash.payout", "payment.attest", "price.override", "product.create", "product.edit", "product.read", "report.margin", "report.sales.store", "sale.create", "sale.discount.line", "sale.discount.unlimited", "sale.refund", "sale.review.resolve", "sale.void", "shift.close", "stock.adjust", "stock.count", "stock.receive", "user.manage"],
  "admin": ["cash.payout", "payment.attest", "price.override", "product.create", "product.edit", "product.read", "report.margin", "report.sales.store", "sale.create", "sale.discount.line", "sale.discount.unlimited", "sale.refund", "sale.review.resolve", "sale.void", "settings.manage", "shift.close", "stock.adjust", "stock.count", "stock.receive", "user.manage"],
} as const;

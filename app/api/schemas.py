"""The local API contract.

Frozen in phase 1 and published as OpenAPI; ``scripts/gen_ts_types.py``
generates the TypeScript the React tracks import. Without this the Python and
JS tracks diverge and a week goes on reconciling them (execution plan §4).

Money is always integer minor units on the wire, never a decimal string and
never a float. The UI formats; it does not compute.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.money import Money


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ── /health ─────────────────────────────────────────────────────────────────


class HealthResponse(ApiModel):
    """The startup gate the splash polls. Unauthenticated by design."""

    status: str = Field(description="'ready' once migrations have been applied")
    version: str = Field(description="Application version")
    schema_version: int = Field(description="Applied SQLite user_version")
    expected_schema_version: int = Field(description="Schema version this build ships")
    migrations_pending: bool
    store_code: str
    terminal_code: str
    cloud_configured: bool = Field(
        description="False when no Supabase project is configured (offline dev)"
    )


# ── /auth ───────────────────────────────────────────────────────────────────


class LoginRequest(ApiModel):
    employee_code: str = Field(min_length=1, max_length=32)
    pin: str = Field(min_length=4, max_length=12)


class SessionResponse(ApiModel):
    user_id: str
    employee_code: str
    full_name: str
    store_id: str
    roles: list[str]
    permissions: list[str] = Field(
        description="Advisory. Postgres RLS is the security boundary."
    )
    authenticated_at: datetime
    offline: bool = Field(
        description="True when authenticated against the local cache"
    )


class ErrorResponse(ApiModel):
    detail: str
    code: str | None = None


# ── Money on the wire ───────────────────────────────────────────────────────


class MoneyOut(ApiModel):
    """An amount, as both the canonical integer and something to display.

    `paise` is the truth; `text` is pre-formatted so the UI never reimplements
    currency formatting and cannot drift from what the receipt prints.
    Formatting is presentation, not math — React still does no arithmetic
    (architecture §1.2).
    """

    paise: int
    text: str

    @staticmethod
    def of(amount: Money) -> MoneyOut:
        return MoneyOut(paise=amount.paise, text=str(amount))


# ── /catalog ────────────────────────────────────────────────────────────────


class ProductOut(ApiModel):
    product_id: str
    sku: str
    name: str
    short_name: str
    uom: str
    is_weighed: bool
    tax_code: str
    rate_bp: int
    unit_price: MoneyOut
    pack_size: int = 1
    barcode: str | None = None


class LookupResponse(ApiModel):
    """A parsed scan. `product` is null when the code resolved to nothing."""

    raw: str
    symbology: str
    resolved: bool
    product: ProductOut | None = None
    #: Set when the code itself carried a quantity (a `22…` weighed code).
    qty_milli: int | None = None
    is_case: bool = False
    error: str | None = None


class SearchResponse(ApiModel):
    query: str
    results: list[ProductOut]


# ── /register ───────────────────────────────────────────────────────────────


class TaxComponentOut(ApiModel):
    tax_code: str
    rate_bp: int
    taxable_base: MoneyOut
    tax_amount: MoneyOut
    cgst: MoneyOut
    sgst: MoneyOut


class CartLineOut(ApiModel):
    line_no: int
    product_id: str
    description: str
    qty_milli: int
    quantity_text: str
    unit_price: MoneyOut
    discount: MoneyOut
    tax_amount: MoneyOut
    line_total: MoneyOut


class CartOut(ApiModel):
    cart_id: str
    lines: list[CartLineOut]
    item_count: int
    subtotal: MoneyOut
    discount_total: MoneyOut
    tax_total: MoneyOut
    total_before_rounding: MoneyOut
    tax_breakdown: list[TaxComponentOut]
    paid: MoneyOut
    outstanding: MoneyOut
    rounding_adjustment: MoneyOut
    settled: bool
    locked: bool = Field(
        default=False,
        description="True once a payment is approved; the basket cannot change",
    )


class AddLineRequest(ApiModel):
    barcode: str | None = None
    product_id: str | None = None
    qty_milli: int | None = Field(
        default=None, description="Integer thousandths; 1.250 kg is 1250"
    )


class UnlistedLineRequest(ApiModel):
    """Sell an item the catalogue does not have (architecture 9.2, phase 6).

    No `product_id`: the line goes against the one placeholder product and
    carries its real identity itself. `tax_code` is a key into `tax_codes`,
    never a rate — a rate the client chose is a tax bill the client chose.
    """

    description: str = Field(description="What the cashier read off the packet")
    unit_price_paise: int = Field(gt=0, description="Integer paise, never a float")
    tax_code: str
    barcode: str | None = Field(
        default=None, description="The code that matched nothing, if there was one"
    )
    qty_milli: int | None = Field(
        default=None, description="Integer thousandths; 1.250 kg is 1250"
    )


class TaxCodeOut(ApiModel):
    """A rate the quick-create form may offer."""

    code: str
    name: str
    rate_bp: int = Field(description="Basis points; 18% is 1800")
    is_inclusive: bool


class TaxCodesResponse(ApiModel):
    tax_codes: list[TaxCodeOut]


class ChangeQuantityRequest(ApiModel):
    qty_milli: int


class TenderQuote(ApiModel):
    """What a method would collect, before the cashier commits to it."""

    method: str
    outstanding: MoneyOut
    collected: MoneyOut
    rounding_adjustment: MoneyOut


class TenderRequest(ApiModel):
    method: str = Field(description="cash or upi")
    tendered_paise: int | None = Field(
        default=None, description="Cash handed over, when more than is owed"
    )


class TenderResponse(ApiModel):
    attempt_id: str
    state: str
    cart: CartOut
    change_due: MoneyOut
    #: When a UPI attempt lapses if nobody confirms it. Null for cash, which
    #: resolves the instant it is begun.
    expires_at: str | None = None


class AttestRequest(ApiModel):
    """What the cashier saw on the merchant phone or soundbox."""

    amount_paise: int | None = Field(
        default=None,
        description=(
            "What actually arrived. Defaults to the amount asked for. The "
            "customer types the figure into their own app on a printed "
            "counter QR, so it can differ in either direction."
        ),
    )
    reference: str | None = Field(
        default=None,
        description=(
            "The UTR. With a static QR this is the only identifier the bank "
            "statement and this sale have in common — worth capturing."
        ),
    )


class UnknownPaymentRequest(ApiModel):
    reason: str | None = None


class AttemptOut(ApiModel):
    attempt_id: str
    method: str
    state: str
    amount: MoneyOut
    reference: str | None = None
    expires_at: str | None = None
    #: True while the attempt is still waiting on the world.
    is_pending: bool


class ReviewItemOut(ApiModel):
    """A sale posted as `requires_review`, awaiting a supervisor."""

    sale_id: str
    receipt_no: str
    grand_total: MoneyOut
    disputed_amount: MoneyOut
    posted_at: str


class ReviewQueueResponse(ApiModel):
    items: list[ReviewItemOut]


class ResolveReviewRequest(ApiModel):
    outcome: str = Field(
        description=(
            "'paid' if the money was there after all, 'not_paid' if it never "
            "arrived. Never a bare 'resolved': a variance nobody can name is "
            "one nobody can act on at shift close."
        )
    )
    note: str | None = None


class ResolveReviewResponse(ApiModel):
    sale_id: str
    outcome: str
    resolved_at: str


class PostSaleResponse(ApiModel):
    sale_id: str
    receipt_no: str
    status: str
    grand_total: MoneyOut
    rounding_adjustment: MoneyOut
    change_due: MoneyOut
    receipt_html: str
    receipt_text: str


class ReceiptPdfResponse(ApiModel):
    sale_id: str
    receipt_no: str
    path: str


# ── Sync — architecture §9 ──────────────────────────────────────────────────


class SyncStatusResponse(ApiModel):
    online: bool
    #: Sales taken here that the cloud has not acknowledged. The one figure a
    #: cashier actually needs.
    backlog: int
    oldest_pending_at: str | None = None
    #: Rows the server refused permanently. Never zero for a good reason.
    failures: int = 0
    last_push_at: str | None = None
    last_pull_at: str | None = None
    last_error: str | None = None
    #: The server rejected this build's payload version (§17). Waiting will
    #: not fix it; updating the terminal will.
    needs_update: bool = False


class SyncFailureOut(ApiModel):
    id: int
    outbox_id: int | None = None
    entity: str | None = None
    error: str
    failed_at: str


class SyncFailuresResponse(ApiModel):
    items: list[SyncFailureOut]


class SyncRetryRequest(ApiModel):
    #: Which failures to re-queue. Omitted or null means all of them, which is
    #: what a manager wants after fixing one shared cause.
    failure_ids: list[int] | None = None


class SyncRetryResponse(ApiModel):
    #: Rows put back in the queue. Fewer than asked for is normal: a failure
    #: with no outbox row behind it is acknowledged but cannot be re-sent.
    requeued: int
    status: SyncStatusResponse


# ── /inventory ──────────────────────────────────────────────────────────────


class ReceiptPreviewRequest(ApiModel):
    """One scan of a delivery, before it is committed."""

    barcode: str
    #: How many of the thing were scanned. A case counts as one pack; how many
    #: units that is comes from the barcode row's `pack_size`.
    packs: int = Field(ge=1, le=10_000)


class ReceiptLineOut(ApiModel):
    """What that scan would receive, for the running list on the screen."""

    product_id: str
    description: str
    barcode: str
    packs: int
    pack_size: int
    #: Whole units, which is what the person counting off the pallet sees.
    units: int
    #: Thousandths, which is what lands in the ledger. Both are shown because
    #: a receipt three orders of magnitude out looks plausible in either one
    #: alone.
    delta_milli: int


class ReceiptRequest(ApiModel):
    lines: list[ReceiptPreviewRequest] = Field(min_length=1)


class StockCountLine(ApiModel):
    product_id: str
    #: What was actually on the shelf, in thousandths. The correction is
    #: computed against what the terminal expected, inside one transaction.
    counted_milli: int = Field(ge=0)


class StockCountRequest(ApiModel):
    lines: list[StockCountLine] = Field(min_length=1)


class AdjustmentRequest(ApiModel):
    product_id: str
    #: Signed: negative for breakage or shrinkage, positive for stock found.
    delta_milli: int
    #: Required. The only stock movement with no document behind it.
    note: str = Field(min_length=1, max_length=500)


class MovementsResponse(ApiModel):
    """The ledger rows written. Empty when a count matched everywhere."""

    movement_ids: list[str]


# ── Admin: catalogue editing (phase 6 slice 6) ────────────────────────────
#
# These cross the local service to Supabase rather than to SQLite, so every
# one of them can fail with "no internet" in a way nothing else in this file
# can. The router turns that into 503, which the UI client already reads as
# unavailable rather than refused.


class AdminProductOut(ApiModel):
    product_id: str
    sku: str
    name: str
    short_name: str | None = None
    uom: str
    tax_code: str
    is_weighed: bool
    track_stock: bool
    is_active: bool


class AdminProductsResponse(ApiModel):
    products: list[AdminProductOut]


class ProductCreateRequest(ApiModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    short_name: str | None = Field(default=None, max_length=64)
    uom: str = Field(default="each", min_length=1, max_length=16)
    tax_code: str = Field(min_length=1, max_length=32)
    is_weighed: bool = False
    #: False for anything sold loose or by weight, and for the unlisted
    #: placeholder. A product that does not track stock writes no ledger row.
    track_stock: bool = True


class ProductUpdateRequest(ApiModel):
    """Every field optional: a PATCH touches only what it names."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    short_name: str | None = Field(default=None, max_length=64)
    uom: str | None = Field(default=None, min_length=1, max_length=16)
    tax_code: str | None = Field(default=None, min_length=1, max_length=32)
    is_weighed: bool | None = None
    track_stock: bool | None = None
    is_active: bool | None = None


class AdminBarcodeOut(ApiModel):
    barcode_id: str
    product_id: str
    barcode: str
    symbology: str
    pack_size: int
    is_primary: bool


class BarcodesResponse(ApiModel):
    barcodes: list[AdminBarcodeOut]


class BarcodeAddRequest(ApiModel):
    barcode: str = Field(min_length=1, max_length=64)
    #: Units in the coded pack. A case code is the same product at a
    #: different multiple, not a different product.
    pack_size: int = Field(default=1, ge=1)


class AdminPriceOut(ApiModel):
    price_id: str
    product_id: str
    store_id: str
    #: Paise. `cost` is deliberately absent — 0003 keeps the margin behind a
    #: column grant and slice 6 has no reason to widen it.
    price: int
    valid_from: str
    valid_to: str | None = None


class PricesResponse(ApiModel):
    prices: list[AdminPriceOut]


class PriceSetRequest(ApiModel):
    #: Paise, like every other money value that crosses this boundary.
    price: int = Field(ge=0)


class UnknownScanOut(ApiModel):
    scan_id: str
    store_id: str
    barcode: str
    scanned_at: str
    terminal_id: str | None = None
    resolved: bool


class UnknownScansResponse(ApiModel):
    scans: list[UnknownScanOut]


class LowStockOut(ApiModel):
    product_id: str
    sku: str
    name: str
    uom: str
    on_hand: int
    reorder_point: int


class LowStockResponse(ApiModel):
    """At or under the reorder point. A query, not an alert."""

    rows: list[LowStockOut]

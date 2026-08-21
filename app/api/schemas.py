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

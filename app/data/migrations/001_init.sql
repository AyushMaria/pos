-- 001_init — the local terminal schema (architecture §7).
--
-- Money is INTEGER minor units (paise). Quantities are INTEGER thousandths,
-- so 1.250 kg is 1250. SQLite has no decimal type and REAL is never
-- acceptable for either — see architecture §6 and the §17 risk table.
--
-- Released. Do not edit; fix forward with 002_.

-- ── Cached reference data (pull-only, server authoritative) ────────────────

CREATE TABLE products (
    id          TEXT PRIMARY KEY,
    sku         TEXT NOT NULL,
    name        TEXT NOT NULL,
    short_name  TEXT,
    category_id TEXT,
    uom         TEXT NOT NULL DEFAULT 'each',
    is_weighed  INTEGER NOT NULL DEFAULT 0,
    track_stock INTEGER NOT NULL DEFAULT 1,
    tax_code    TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT
);

CREATE TABLE product_barcodes (
    id         TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    barcode    TEXT NOT NULL,
    symbology  TEXT NOT NULL,
    -- Scanning a case of 12 receives 12 units (architecture §10.2).
    pack_size  INTEGER NOT NULL DEFAULT 1,
    is_primary INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE product_prices (
    id         TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    store_id   TEXT NOT NULL,
    price      INTEGER NOT NULL,
    cost       INTEGER,
    valid_from TEXT NOT NULL,
    valid_to   TEXT
);

CREATE TABLE promotions (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    value      INTEGER NOT NULL,
    starts_at  TEXT,
    ends_at    TEXT,
    priority   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE tax_codes (
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    -- Basis points: 18% GST is 1800. Never a float.
    rate_bp      INTEGER NOT NULL,
    is_inclusive INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL,
    deleted_at   TEXT
);

CREATE TABLE stock_levels (
    product_id    TEXT PRIMARY KEY REFERENCES products(id),
    on_hand       INTEGER NOT NULL DEFAULT 0,
    reorder_point INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

-- ── Local identity cache (offline auth, architecture §11.4) ────────────────

CREATE TABLE cached_users (
    user_id             TEXT PRIMARY KEY,
    employee_code       TEXT NOT NULL UNIQUE,
    full_name           TEXT NOT NULL,
    store_id            TEXT NOT NULL,
    pin_hash            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    roles_json          TEXT NOT NULL DEFAULT '[]',
    snapshot_signed_at  TEXT NOT NULL,
    snapshot_expires_at TEXT NOT NULL
);

CREATE TABLE cached_permissions (
    user_id        TEXT NOT NULL REFERENCES cached_users(user_id) ON DELETE CASCADE,
    store_id       TEXT NOT NULL,
    permission_key TEXT NOT NULL,
    PRIMARY KEY (user_id, store_id, permission_key)
);

-- ── Transactional (push-only, immutable once posted) ──────────────────────

CREATE TABLE register_sessions (
    id            TEXT PRIMARY KEY,
    terminal_id   TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    opened_at     TEXT NOT NULL,
    opening_float INTEGER NOT NULL DEFAULT 0,
    closed_at     TEXT,
    counted_cash  INTEGER,
    expected_cash INTEGER,
    variance      INTEGER,
    status        TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE sales (
    id                  TEXT PRIMARY KEY,
    store_id            TEXT NOT NULL,
    terminal_id         TEXT NOT NULL,
    session_id          TEXT REFERENCES register_sessions(id),
    receipt_no          TEXT,
    cashier_id          TEXT NOT NULL,
    type                TEXT NOT NULL DEFAULT 'sale',
    -- parked | completed | voided | requires_review
    status              TEXT NOT NULL,
    subtotal            INTEGER NOT NULL DEFAULT 0,
    discount_total      INTEGER NOT NULL DEFAULT 0,
    tax_total           INTEGER NOT NULL DEFAULT 0,
    -- Applied after tender selection, cash portion only (architecture §13.4).
    rounding_adjustment INTEGER NOT NULL DEFAULT 0,
    grand_total         INTEGER NOT NULL DEFAULT 0,
    original_sale_id    TEXT REFERENCES sales(id),
    client_created_at   TEXT NOT NULL,
    posted              INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sale_lines (
    id              TEXT PRIMARY KEY,
    sale_id         TEXT NOT NULL REFERENCES sales(id),
    line_no         INTEGER NOT NULL,
    product_id      TEXT NOT NULL,
    barcode_scanned TEXT,
    description     TEXT NOT NULL,
    qty_milli       INTEGER NOT NULL,
    -- Snapshot at scan time; a mid-transaction price sync must not change
    -- what the customer was quoted (architecture §6).
    unit_price      INTEGER NOT NULL,
    discount_amount INTEGER NOT NULL DEFAULT 0,
    tax_amount      INTEGER NOT NULL DEFAULT 0,
    line_total      INTEGER NOT NULL,
    overridden_by   TEXT,
    override_reason TEXT,
    UNIQUE (sale_id, line_no)
);

CREATE TABLE payment_attempts (
    id             TEXT PRIMARY KEY,
    sale_id        TEXT NOT NULL REFERENCES sales(id),
    method         TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    -- pending | approved | declined | cancelled | expired | unknown
    state          TEXT NOT NULL,
    txn_ref        TEXT,
    qr_payload     TEXT,
    started_at     TEXT NOT NULL,
    expires_at     TEXT,
    resolved_at    TEXT,
    failure_reason TEXT
);

CREATE TABLE payments (
    id                  TEXT PRIMARY KEY,
    sale_id             TEXT NOT NULL REFERENCES sales(id),
    attempt_id          TEXT REFERENCES payment_attempts(id),
    method              TEXT NOT NULL,          -- cash | upi
    amount              INTEGER NOT NULL,
    status              TEXT NOT NULL,          -- approved | voided
    tender_ref          TEXT,
    utr                 TEXT,
    -- immediate | manual_attestation | psp_webhook
    confirmation_method TEXT NOT NULL,
    confirmed_by        TEXT,
    -- 0 until settlement matches; reported separately on the Z-report (§13.5).
    verified            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE stock_ledger (
    id          TEXT PRIMARY KEY,
    store_id    TEXT NOT NULL,
    product_id  TEXT NOT NULL,
    delta_milli INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    ref_type    TEXT,
    ref_id      TEXT,
    occurred_at TEXT NOT NULL,
    terminal_id TEXT,
    user_id     TEXT
);

CREATE TABLE cash_movements (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES register_sessions(id),
    direction   TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    reason      TEXT,
    actor_id    TEXT NOT NULL,
    approved_by TEXT,
    occurred_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    id          TEXT PRIMARY KEY,
    actor_id    TEXT,
    approver_id TEXT,
    action      TEXT NOT NULL,
    entity      TEXT,
    entity_id   TEXT,
    before_json TEXT,
    after_json  TEXT,
    occurred_at TEXT NOT NULL
);

CREATE TABLE unknown_scans (
    id          TEXT PRIMARY KEY,
    barcode     TEXT NOT NULL,
    scanned_at  TEXT NOT NULL,
    terminal_id TEXT,
    resolved    INTEGER NOT NULL DEFAULT 0
);

-- ── Terminal-only plumbing (never synced upward) ──────────────────────────

CREATE TABLE outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity       TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    op           TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    client_seq   INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    synced_at    TEXT
);

CREATE TABLE sync_state (
    entity         TEXT PRIMARY KEY,
    cursor         TEXT,
    last_pulled_at TEXT
);

CREATE TABLE sync_failures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id    INTEGER,
    payload_json TEXT NOT NULL,
    error        TEXT NOT NULL,
    failed_at    TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);

-- Receipt numbers are {STORE}-{TERMINAL}-{SEQ} from a local monotonic
-- counter: unique without coordination, and gaps are auditable.
CREATE TABLE terminal_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO terminal_state (key, value) VALUES ('receipt_seq', '0');
INSERT INTO terminal_state (key, value) VALUES ('client_seq', '0');

-- ── Indexes ───────────────────────────────────────────────────────────────

CREATE UNIQUE INDEX ux_barcode ON product_barcodes(barcode) WHERE deleted_at IS NULL;
CREATE INDEX ix_barcode_product ON product_barcodes(product_id);
CREATE INDEX ix_prices_product ON product_prices(product_id, store_id);
CREATE INDEX ix_outbox_pending ON outbox(synced_at, id);
CREATE INDEX ix_sale_lines_sale ON sale_lines(sale_id);
CREATE INDEX ix_payments_sale ON payments(sale_id);
CREATE INDEX ix_attempts_sale ON payment_attempts(sale_id);
CREATE INDEX ix_ledger_product ON stock_ledger(store_id, product_id, occurred_at);
CREATE INDEX ix_sales_session ON sales(session_id, status);
CREATE UNIQUE INDEX ux_sales_receipt_no ON sales(receipt_no) WHERE receipt_no IS NOT NULL;

-- ── Full-text product search ──────────────────────────────────────────────
-- External-content FTS5 over products; triggers keep it in step. Target is
-- 200k products with search under 100 ms (architecture §16).

CREATE VIRTUAL TABLE products_fts USING fts5(
    name, sku, content='products', content_rowid='rowid'
);

CREATE TRIGGER products_fts_ai AFTER INSERT ON products BEGIN
    INSERT INTO products_fts(rowid, name, sku) VALUES (new.rowid, new.name, new.sku);
END;

CREATE TRIGGER products_fts_ad AFTER DELETE ON products BEGIN
    INSERT INTO products_fts(products_fts, rowid, name, sku)
        VALUES ('delete', old.rowid, old.name, old.sku);
END;

CREATE TRIGGER products_fts_au AFTER UPDATE ON products BEGIN
    INSERT INTO products_fts(products_fts, rowid, name, sku)
        VALUES ('delete', old.rowid, old.name, old.sku);
    INSERT INTO products_fts(rowid, name, sku) VALUES (new.rowid, new.name, new.sku);
END;

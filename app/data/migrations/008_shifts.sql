-- 008_shifts - the close is an event, not an edit.
--
-- `register_sessions` has had `closed_at`, `counted_cash`, `expected_cash`
-- and `variance` since 001, on the assumption that closing a shift updates
-- the row. It cannot: the outbox carries inserts (architecture §9.2), and a
-- shop closes at ten with the line down as often as not. A close that needs
-- the cloud is a shop that cannot go home.
--
-- So the close is its own append-only row, the way `sale_reviews` is the
-- resolution of a sale rather than an edit to it (§13.5). The session row
-- says `open` for ever; `shift_closes` is what happened to it. The columns
-- left on `register_sessions` stay, unused, so that 001 need not be rewritten.
--
-- Every figure the Z-report shows is stored, not recomputed: the numbers a
-- supervisor signed off are the numbers, even if a review lands later.
CREATE TABLE shift_closes (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL REFERENCES register_sessions(id),
    closed_at          TEXT NOT NULL,
    closed_by          TEXT NOT NULL,
    counted_cash       INTEGER NOT NULL,
    expected_cash      INTEGER NOT NULL,
    variance           INTEGER NOT NULL,
    cash_sales         INTEGER NOT NULL,
    upi_attested       INTEGER NOT NULL,
    upi_verified       INTEGER NOT NULL,
    cash_in            INTEGER NOT NULL,
    cash_out           INTEGER NOT NULL,
    rounding           INTEGER NOT NULL,
    under_review_count INTEGER NOT NULL,
    under_review_total INTEGER NOT NULL,
    sales_count        INTEGER NOT NULL,
    note               TEXT
);

CREATE UNIQUE INDEX shift_closes_session ON shift_closes(session_id);

-- The cloud keys a session by store as well as terminal; the local row never
-- carried the store because nothing pushed it. Written at open from the
-- signed-in session, read by the payload builder.
ALTER TABLE register_sessions ADD COLUMN store_id TEXT;

-- One open shift per terminal. Enforced here rather than only in the service
-- so that a crash between "check" and "insert" cannot leave two.
CREATE UNIQUE INDEX register_sessions_one_open
    ON register_sessions(terminal_id) WHERE status = 'open';

-- The Z-report reads a shift's sales by session; nothing indexed this before
-- because nothing wrote it.
CREATE INDEX sales_by_session ON sales(session_id);

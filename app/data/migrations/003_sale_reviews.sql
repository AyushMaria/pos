-- 003_sale_reviews — resolving a `requires_review` sale, without an UPDATE.
--
-- A UPI attempt that ends UNKNOWN posts its sale as `requires_review`
-- (architecture §13.5): the customer says they paid, no notification arrived,
-- and both confident answers are wrong. A supervisor settles it afterwards.
--
-- The obvious implementation is `UPDATE sales SET status = 'completed'`. It is
-- also impossible, and the reason is worth stating in the schema rather than
-- discovering during phase 5: **there is no UPDATE policy on `sales` in
-- Postgres and there never will be** (architecture §1.4, §11.2). Transactions
-- are immutable events, which is what makes sync merge-free. A status this
-- terminal rewrote locally could never be pushed.
--
-- So the resolution is its own append-only row, exactly like every other
-- correction in the system. The sale keeps saying `requires_review` forever —
-- which is honest, because it did — and this table says what was decided
-- about it, by whom, and when.

CREATE TABLE sale_reviews (
    id          TEXT PRIMARY KEY,
    sale_id     TEXT NOT NULL REFERENCES sales(id),
    -- paid: the money was there after all (a delayed notification, usually).
    -- not_paid: it never arrived, and the shop is short.
    -- Deliberately not 'resolved': a variance nobody can name is a variance
    -- nobody can act on at shift close.
    outcome     TEXT NOT NULL CHECK (outcome IN ('paid', 'not_paid')),
    note        TEXT,
    resolved_by TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

-- One resolution per sale. A second supervisor changing their mind writes
-- nothing; disagreement is a conversation, not a row.
CREATE UNIQUE INDEX ux_sale_reviews_sale ON sale_reviews(sale_id);

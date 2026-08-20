-- 002_sale_line_tax — snapshot the tax rate onto the sale line.
--
-- 001 stored `tax_amount` per line but not which rate produced it, so a
-- reprint had to re-read the rate from the catalogue. That is wrong for the
-- same reason re-reading the price would be wrong (architecture §6): if the
-- rate changes afterwards, the reprinted invoice stops matching the one the
-- customer was given, and an auditor reconciling a GST return finds two
-- different documents for one sale.
--
-- The rate is now snapshotted at sale time exactly like the unit price.

ALTER TABLE sale_lines ADD COLUMN tax_code TEXT;
ALTER TABLE sale_lines ADD COLUMN tax_rate_bp INTEGER NOT NULL DEFAULT 0;

CREATE INDEX ix_sale_lines_tax ON sale_lines(sale_id, tax_code);

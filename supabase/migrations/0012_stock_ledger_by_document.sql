-- 0012_stock_ledger_by_document — a cashier may sell, not restock.
--
-- 0003 wrote one policy for every way stock moves:
--
--     with check (pos.in_store(store_id)
--                 and (pos.has_perm('sale.create')
--                      or pos.has_perm('stock.receive')
--                      or pos.has_perm('stock.count')
--                      or pos.has_perm('stock.adjust')))
--
-- `sale.create` is in there because a sale writes its own deltas and a
-- cashier has to be able to post a sale. At the time that was the *only* way
-- a row reached this table, so the disjunction cost nothing.
--
-- Phase 6 adds three more ways, and the same disjunction now says a cashier
-- may push a goods receipt. `POST /inventory/receipts` refuses them — it is
-- gated on `stock.receive` — but that is the application, and §11.1 is
-- explicit that client-side and API checks are UX while RLS is the security
-- boundary. Anyone holding a cashier's JWT can talk to PostgREST directly,
-- and until this migration they could have inflated stock to any figure they
-- liked without ever selling anything.
--
-- The fix is to read the column that already says which kind of movement it
-- is. A sale's deltas carry `ref_type = 'sale'` and are written by whoever
-- may create a sale; everything else needs a stock permission. The two are
-- different acts of trust — one has a customer and a receipt behind it, the
-- other has a supplier's note or nothing at all — and the permission matrix
-- has separated them since phase 1 with nothing enforcing it below the API.
--
-- Nothing legitimate changes shape. `SalesRepository.post_sale` has always
-- written `ref_type = 'sale'`, and `InventoryRepository.record` refuses that
-- value outright, so the two sets were already disjoint by construction.

drop policy if exists stock_ledger_insert on public.stock_ledger;

create policy stock_ledger_insert on public.stock_ledger
    for insert to authenticated
    with check (
        pos.in_store(store_id)
        and (
            -- A sale's own deltas, written by whoever may take the sale.
            (ref_type = 'sale' and pos.has_perm('sale.create'))
            -- A receipt, a count, an adjustment: a stock permission each.
            or pos.has_perm('stock.receive')
            or pos.has_perm('stock.count')
            or pos.has_perm('stock.adjust')
        )
    );

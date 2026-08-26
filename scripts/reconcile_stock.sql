-- reconcile_stock.sql — phase 6's exit criterion, as a query.
--
--     "receive a delivery by entering case codes, count a section, and have
--      stock_levels reconcile against the ledger"
--
-- Run it in the Supabase SQL editor, against the cloud. **Not against the
-- terminal**, and the distinction is the whole point.
--
-- `public.stock_levels` is derived from `public.stock_ledger` by
-- `stock_ledger_apply`, a trigger installed in 0001. Every delta that lands
-- moves the level by exactly that delta and nothing else touches it. So the
-- sum of a product's deltas *must* equal its level — always, for every
-- product, with no exception worth allowing. Anything this returns is a bug
-- in a trigger or a write that went around one.
--
-- That has happened. 0008 originally added a second trigger over the same
-- function, so every sale moved stock twice; it was caught by pushing one
-- sale and checking the level moved by one unit rather than two. This query
-- is the general form of that check, and the reason phase 6 is safe to add
-- three new ways of writing to the ledger.
--
-- ── Why not on the terminal ────────────────────────────────────────────────
--
-- The terminal has no such trigger. Its `stock_levels` is a *pulled cache* of
-- the cloud's number, written only by the puller. Its `stock_ledger` holds
-- what this till did. Those are claims about different things, and the gap
-- between them is not an error — it is the queue. `InventoryRepository.
-- unpushed()` answers the terminal's version of the question: what have I
-- done that head office has not heard about?

-- ── The check ─────────────────────────────────────────────────────────────

select coalesce(l.product_id, s.product_id) as product_id,
       coalesce(s.on_hand, 0)               as level,
       coalesce(l.summed, 0)                as ledger,
       coalesce(s.on_hand, 0) - coalesce(l.summed, 0) as drift
from (
    select product_id, sum(delta_milli) as summed
      from public.stock_ledger
     group by product_id
) l
full outer join public.stock_levels s on s.product_id = l.product_id
where coalesce(s.on_hand, 0) <> coalesce(l.summed, 0)
order by abs(coalesce(s.on_hand, 0) - coalesce(l.summed, 0)) desc;

-- **No rows is the pass.** A `drift` of exactly the ledger total means a
-- level that was written by hand; a drift of exactly half means something is
-- applying every delta twice.

-- ── What moved, and why ───────────────────────────────────────────────────
--
-- Not a check — the thing to read when the check fails, or when the owner
-- asks where the stock went. Quantities are thousandths: one unit is 1000.

select p.name,
       sl.reason,
       sl.ref_type,
       sum(sl.delta_milli) / 1000.0 as units,
       count(*)                     as movements
from public.stock_ledger sl
join public.products p on p.id = sl.product_id
where sl.server_received_at > now() - interval '7 days'
group by p.name, sl.reason, sl.ref_type
order by p.name, sl.reason;

# Phase 6 — Inventory and catalogue

*Execution plan §3, weeks 14–15.*

**Exit criteria:** receive a delivery by entering case codes, count a section,
and have `stock_levels` reconcile against the ledger.

Six deliverables:

- Product / barcode / price admin screens — the first real admin UI
- Goods receipt, with pack-size multipliers on ITF-14 codes
- Stock counts and adjustments
- Internal `21…` code assignment for products with no manufacturer barcode
- Unknown-barcode quick-create and the `unknown_scans` queue
- Low-stock alerts

---

## What already exists

More than it looks. Checked, not assumed:

| | Where | State |
|---|---|---|
| `stock_ledger`, `stock_levels` | both schemas | Done. The trigger maintains levels from deltas. |
| `unknown_scans` | both schemas | Table exists. **Nothing writes to it.** |
| `internal_barcode()` — the `21…` generator | `app/domain/barcode.py` | Written and tested. **No caller.** |
| `stock.receive`, `stock.count`, `stock.adjust`, `product.create`, `product.edit` | `app/domain/permissions.py` | In the matrix, generated into Postgres and TypeScript. |
| RLS for stock writes | `0003_rls.sql` | `stock_ledger_insert` already accepts any of the three stock permissions. |
| `reorder_point` | `stock_levels` | Column exists, always 0, nothing reads it. |

So the ledger, the permissions and the security boundary are already in place.
Phase 6 is mostly application code on top of them.

## Two decisions before any code

### 1. Can stock move while the terminal is offline?

It has to. A delivery arrives when it arrives, and the shop that cannot receive
it without the internet is the shop the offline-first premise was for.

That forces the real work of this phase, and it is not the admin screens:

**`sync_push` accepts two entity kinds — `sale` and `sale_review`.** Stock
ledger rows reach the cloud only nested inside a sale envelope
(`app/sync/payloads.py` selects `WHERE ref_type = 'sale'`). A goods receipt or
a stock count produces ledger rows with **no sale behind them**, and there is
no way to push one today. It would sit in the outbox and be refused as
`unknown entity`.

So phase 6 starts server-side, not in the UI.

### 2. Are products created on the terminal, or only in the cloud?

Products are currently **pull-only**: the puller writes them, "server wins,
always", and nothing local edits them. Quick-create from an unknown scan breaks
that — the cashier is holding an item with a barcode nothing recognises, and
the answer cannot be "wait for the internet".

Two routes, and this is a genuine choice:

**(a) Products become two-way.** A new envelope kind, terminal-generated UUIDv7
ids so a create is idempotent, and the pull must stop clobbering a local row
that has not yet been pushed. Most work, and the only one that lets a shop run
a day without the cloud.

**(b) Admin is online-only; quick-create is offline-capable but minimal.** The
admin screens talk to Supabase directly and are simply unavailable offline. A
quick-create offline writes an `unknown_scans` row and a provisional local
product, which a manager confirms later. Less machinery, and it matches how the
plan describes `unknown_scans` — a queue someone works through, not a
full-fidelity offline catalogue editor.

**(b) is the smaller phase 6 and the one I would build**, keeping (a) for when
a second terminal makes it unavoidable. But it means telling the owner that
adding a product needs the internet, and that is their call, not a technical
one.

---

## Build order

Five slices, each shippable and provable on its own. Do them in this order —
each one's proof depends on the one before.

### Slice 1 — Push a ledger row that has no sale behind it

Nothing else in this phase can reach the cloud until this works.

1. **`supabase/migrations/0011_sync_push_stock.sql`** — extend `sync_push` with
   a `stock_movement` entity: the same `on conflict (id) do nothing` insert into
   `stock_ledger` that the sale branch already does, keyed on the
   terminal-generated id. RLS already permits it; no policy changes.
2. **`app/sync/payloads.py`** — build an envelope for an outbox row whose
   entity is `stock_movement`, rather than only assembling ledger rows as a
   child of a sale.
3. **`app/sync/envelope.py`** — bump `SCHEMA_VERSION` only if the sale envelope
   changes shape. Adding a *new* entity kind does not break an old terminal;
   an old *server* refusing a new kind does, and `sync_push` already answers
   that with `unknown entity`. Decide deliberately and write down which.

**Prove it:** a stock movement in the outbox, one cycle, one row in
`public.stock_ledger`, and `stock_levels` moved by exactly the delta. Push it
twice and the level moves once.

### Slice 2 — Receiving

1. **Domain** — `app/domain/receiving.py`, pure: given a scan result and a
   quantity, produce the ledger delta. `delta = +qty × pack_size` (architecture
   §9.4). An ITF-14 resolves to a barcode row with `pack_size > 1`, so scanning
   one case of 24 receives 24 units, and that multiplication is the part worth
   property-testing.
2. **Service** — `app/services/inventory_service.py`, one transaction per
   receipt: ledger rows plus an outbox row, exactly as `post_sale` does. Copy
   that seam rather than inventing a second one.
3. **API** — `app/api/inventory.py`, `POST /inventory/receipts`, gated on
   `stock.receive`.
4. **UI** — a receiving screen: scan or type a code, enter quantity, running
   list, commit.

**Prove it:** receive 24 of a case-coded product and `on_hand` rises by 24000
thousandths — not 1000, and not 24.

### Slice 3 — Counts and adjustments

`delta = counted − expected` (§9.4). The subtlety is *when* "expected" is read:
between reading it and committing the count, a sale can happen. Read expected
and write the delta in one transaction, or the count silently reverses a sale.

1. Service and endpoints for a count and for a manual adjustment, gated on
   `stock.count` and `stock.adjust`.
2. A count screen scoped to a section — the exit criterion says "count a
   section", not "count the shop".
3. An adjustment needs a reason. It is the one stock movement with no document
   behind it, and it is where shrinkage gets hidden.

**Prove it:** the exit criterion. Count a section, then reconcile — the sum of
`stock_ledger.delta_milli` per product must equal `stock_levels.on_hand`, in
the cloud, for every product touched. That query is the phase's acceptance
test and belongs in the suite.

### Slice 4 — Codes for the 38.6%

`internal_barcode()` already generates a valid `21…` EAN-13. It needs a caller
and an allocator.

1. A monotonic item-code counter, per store, in `terminal_state` alongside the
   receipt sequence — same pattern, already proven.
2. `POST /catalog/products/{id}/internal-barcode`, gated on `product.edit`.
3. An admin action that assigns codes in bulk to everything uncoded, because
   12,431 products will not be done one at a time.

**Read execution plan §6 step 5 before building this.** An internal code makes
a product *sellable*, not *scannable* — nothing prints these labels. A packaged
good needs a printed label; something sold loose needs the weighed flow
instead. The triage is per product and is the owner's, not the software's.

### Slice 5 — Unknown scans, quick-create, low stock

1. Write an `unknown_scans` row whenever a lookup misses. The table has been
   waiting since phase 1 and would already have been collecting evidence.
2. A quick-create from that queue: name, price, tax code, and either the
   scanned barcode or a fresh internal one.
3. Low stock is a query, not a feature: `on_hand <= reorder_point` where
   `reorder_point > 0`. Put it on the manager's screen and leave alerting alone
   until someone asks.

---

## What to watch

**Reconciliation is the exit criterion, so write that query first.** Sum the
ledger per product, compare to `stock_levels`, and make it a test. Everything
else in this phase is a way of putting rows in that ledger.

**The duplicate-trigger bug came back once already.** Phase 1 maintained
`stock_levels` from the ledger; phase 5 added a second trigger over the same
function and every sale moved stock twice. Any new path into the ledger is a
chance to do it again — which is why the reconciliation test earns its place.

**Receiving offline then counting online is the interesting case.** The count
reads `expected` from a local level that may not yet reflect an unpushed
receipt. Decide what a count means when the terminal knows it has unsent
movements: refuse it, warn, or count anyway. The architecture calls the local
level "indicative while offline" — this is where that phrase gets tested.

**One terminal, one store.** `stock_levels` is keyed on `(store_id,
product_id)` in the cloud and on `product_id` alone on the terminal. That
asymmetry is already handled in the puller, and it will bite again anywhere
this phase writes a level rather than a delta. Write deltas.

---

## Before starting

Two things from earlier phases are worth closing first, because both touch code
this phase will build on:

- **argon2 re-tuning** (`docs/argon2-tuning.md`) — a supervisor override for an
  adjustment is a PIN check with a customer waiting, and phase 6 adds more of
  them.
- **Opening stock.** `stock_levels` is empty by design. The count flow in
  slice 3 is what seeds it, so phase 6 is the phase that makes an opening
  count possible — worth sequencing before go-live rather than after
  (plan §6 step 6).

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

## The two decisions, settled

### 1. Stock moves while the terminal is offline

It has to. A delivery arrives when it arrives, and the shop that cannot receive
it without the internet is the shop the offline-first premise was for.

That forces the real work of this phase, and it is not the admin screens:

**`sync_push` accepts two entity kinds — `sale` and `sale_review`.** Stock
ledger rows reach the cloud only nested inside a sale envelope
(`app/sync/payloads.py` selects `WHERE ref_type = 'sale'`). A goods receipt or
a stock count produces ledger rows with **no sale behind them**, and there is
no way to push one today. It would sit in the outbox and be refused as
`unknown entity`.

So phase 6 starts server-side, not in the UI. That is slice 1.

### 2. Admin is online-only; quick-create is not

Products are **pull-only** today: the puller writes them, "server wins,
always", and nothing local edits them.

The alternative was making products two-way — a new envelope kind,
terminal-generated ids, and a pull that stops clobbering local rows not yet
pushed. That is roughly a second phase 6, and it waits until a second terminal
makes it unavoidable.

**So the admin screens talk to Supabase directly and are unavailable offline.**
Adding a product properly needs the internet, and the owner should be told so
in those words.

**Quick-create is the exception, and it is the one that matters.** A cashier
holding an item the till has never seen cannot be told to wait for the
internet — the basket is open and a customer is standing there. Offline, an
unknown scan writes a provisional local product and an `unknown_scans` row; the
sale completes; a manager resolves the queue later. A queue someone works
through, which is exactly how the plan describes `unknown_scans` — not a
full-fidelity offline catalogue editor.

The line to hold: **selling something unrecognised works offline. Cataloguing
it properly does not.**

---

## Build order

Six slices, one per deliverable, each shippable and provable on its own. Do
them in this order — each one's proof depends on the one before.

**Two words, and they are not the same word.**

- **code complete** — written, tested, green. Nothing more than that.
- **done** — the slice's own *Prove it* line has been walked, by a person, on
  Windows, against the real project.

Slice 6 was marked **done** while the same paragraph said "live check still to
run", and the thing the live check would have found was that the first step of
its Prove it line — *create a product* — was impossible: `createProduct`
existed in the service, the route, the request model and the TypeScript
client, and nothing called it. 695 green tests, 22 of them about this screen,
and none of them could see it, because they all tested the code that had been
written rather than the job that had been asked for.

A slice is not done because the tests are green. The tests measure what was
built; the Prove it line is the only thing that measures what was wanted.

### Slice 1 — Push a ledger row that has no sale behind it — **done**

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

### Slice 2 — Receiving — **done**

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

### Slice 3 — Counts and adjustments — **done**

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

### Slice 4 — Codes for the 38.6% — **done**

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

### Slice 5 — Unknown scans and unlisted items — **done**

The first place decision 2 became code, and the place the plan above turned out
to be wrong. What shipped:

1. **Every missed lookup is filed.** `app/data/repositories/unknown_scans.py`
   writes an `unknown_scans` row and an outbox row whenever `resolve()` finds
   nothing. Deliberately not deduplicated: the same unknown code presented five
   times in a week is a far stronger signal than the same code once.
2. **`supabase/migrations/0014_unlisted_and_unknown_scans.sql`** — a `21…`-free
   placeholder product (`SKU-UNLISTED`, `track_stock false`, fixed UUID) plus an
   `unknown_scan` branch in `sync_push`.
3. **`POST /register/carts/{id}/lines/unlisted`** and a register dialog behind
   it, gated on `sale.create` alone. The cashier types what it is, a price, and
   picks a rate from `GET /catalog/tax-codes`.

**What changed from the plan, and why.** This slice was written as *offline
quick-create*: invent a local, provisional product row and point the sale line
at it. That cannot work, and the schema says so —

```sql
product_id uuid not null references public.products(id)
```

A line pointing at a product the cloud has never heard of fails that key, and
it fails *after* the customer has paid: the sale is taken, printed, and then
quarantined hours later by the pusher. This is exactly the sharp edge the
"What to watch" section below predicted — a provisional row the puller knows
nothing about, in a table it overwrites with "server wins, always" — and the
fix was to not create the row at all.

Instead one placeholder product carries every unlisted line, and the item's
real identity rides on the line, in columns that have existed since phase 1:

```
sale_lines.description       what the cashier typed
sale_lines.barcode_scanned   the code that matched nothing
```

The sale is right in money terms — right total, right GST, a legible receipt —
and syncs like any other sale. `tests/test_rls.py` proves the reference holds
against a real Postgres; that is the only place it could be proved.

**What is given up:** stock is not tracked for an unlisted item. It cannot be.
Once a manager creates the real product it needs an opening count, which is a
stock-take either way.

**Step 3 of the original plan — resolution — moves to slice 6**, where the
admin screens are. Working the queue is a catalogue edit and belongs with the
other catalogue edits.

**Proved by:** `tests/test_unlisted_items.py` (16), four RLS tests, five
component tests. **Live check passed:** offline, scanned something unknown,
sold it anyway, completed the sale, synced, and both the sale and the queue
entry landed.

### Slice 6 — Admin screens, low stock, and the unknown-scan queue — **done**

The first real admin UI, and — by decision 2 — the simplest thing in the phase,
because it is online-only. It talks to Supabase through the same RLS every
other client does, so `product.create` and `product.edit` are already enforced.

1. Product list, search, a **create** form and an edit form: SKU, name, short
   name, category, UOM, tax code, `is_weighed`, `track_stock`. *Create was not
   in this list when the slice was built, and so it was not built. It is in
   the Prove it line, which is the list that counts.*
2. Barcodes and prices as sub-editors. `ux_product_barcodes_barcode` will
   refuse a duplicate code — surface that as a message about *which* product
   already holds it, not as a constraint name.
3. **Work the unknown-scan queue** (moved here from slice 5): a manager takes
   an entry and either matches it to an existing product or creates a real one.
   Resolving must not retroactively alter a sale — a sold line records what was
   charged, whatever the catalogue later says the item is called.
4. Low stock is a query, not a feature: `on_hand <= reorder_point` where
   `reorder_point > 0`. Put it on the manager's screen and leave alerting alone
   until somebody asks for it. *Shipped without any way to set
   `reorder_point`, so the list was correct and structurally incapable of
   returning a row — and the column it displayed was thousandths rendered as
   units, which nobody saw because the table was always empty. 0018 adds the
   write path and the screen converts. Alerting is still nobody's job yet.*

**Prove it:** create a product, give it a barcode and a price, sell it. Then
try to give a second product the same barcode and read what the screen says.

**What "talks to Supabase directly" turned out to mean.** Decision 2 is about
the *write path*: no local row, no outbox, no offline. It is not about the
transport, and it could not be — architecture §5 forbids the page holding a
token that outlives its launch, so putting `supabase-js` in the webview would
have traded one architectural rule for another. The admin router forwards to
PostgREST carrying **the signed-in user's own access token**, which is already
in `SessionStore` from login. RLS therefore evaluates exactly as it would for
any other client, and the three enforcement points (§11.1) stay intact.

**Four policies were missing, and none of them would have failed loudly.**
This is the whole story of the slice. With RLS on, a missing policy is not an
error: the statement succeeds, matches nothing, and returns 200. Every screen
built on one looks like it works until somebody refreshes.

- `unknown_scans` had no UPDATE policy — so *every* resolve was a no-op (0016).
- `product_barcodes` had no UPDATE policy — removing a code is a soft delete,
  which is an update (0017).
- `product_prices` had no INSERT or UPDATE policy, and no column grants for
  either (0017).
- Low stock compares two columns, which a PostgREST filter cannot express at
  all; it needed a `security_invoker` view (0017).

`sale_lines` was the exception that needed nothing: it has insert and select
and has never had anything else, so requirement 3 — resolving a scan must not
retroactively alter a sale — was already true by construction. It got a test
rather than a policy. That test asserts the line is *unchanged* as well as
that the update matched nothing, because a rowcount of zero also happens when
the table is empty, which is how it read on the first attempt.

**Proved by:** `tests/test_admin.py` (28), eleven RLS tests against a real
Postgres, fifteen component tests.

**Live check passed** on Windows against the real project — search, add a
barcode, set a price, sell it, offer the same code to a second product and
read the sentence, withdraw the code and re-add it. Every step behaved.

**What the live pass found, which nothing else could.** Step one of the Prove
it line is *create a product*, and there was no way to do it. `createProduct`
existed in `admin_service.py`, in the router, in `ProductCreateRequest` and in
`core/api/admin.ts`; no component called it, and no test in the repository
mentioned it. The same hole ran through requirement 3 — the queue could mark a
scan resolved but could neither catalogue the item nor point the code at an
existing product, so the one action that stops the next scan failing was the
one action missing.

Both are now built, and two mechanical guards came with them, because the
interesting part is that the toolchain could not see any of it:

- `scripts/check_dead_client.py` — every function in the TypeScript API client
  must be called from a non-test file. `noUnusedLocals` cannot see a property
  on an exported object, and `gen_ts_types.py --check` passed throughout: the
  type was correct, it was simply never used.
- `tests/test_route_coverage.py` — every route must have an HTTP-level test.
  It counts method *and* path, because `GET /admin/products` was well tested
  and any path-level count would have called `POST /admin/products` covered.
  It ships with eleven routes listed as debt, seven of them this slice's own
  router. That list may shrink and nothing else.

---

## What the admin path did not record

The five catalogue mutations wrote no `audit_log` row. Every sync path writes
them, so the admin screen was the only way to change this shop's data
invisibly — and the definition of done says "audit rows exist for anything a
manager would need to investigate later", which is a line about price changes
whether or not it names them.

0019 puts it in a trigger rather than in `admin_service.py`, because the
screen reaches PostgREST under the user's own token: a Python-side audit is
exactly as honest as the client holding that token. Two things fell out of
writing it. `audit_log_select` required `pos.in_store(store_id)` while
`audit_log_insert` allowed a null store, so a catalogue edit — `products` has
no store column — would have been recorded and then invisible to everyone.
And `to_jsonb(NEW)` on `product_prices` would have carried `cost`, which 0003
revoked and 0005 built a guarded view to reach, straight into a table a
manager can read.

0020 came out of checking the grants behind it. Supabase grants `anon` and
`authenticated` every privilege on everything in `public`; only three tables
had ever been narrowed, each in response to a specific hole. Nothing there is
reachable with an anon key — PostgREST issues only the four verbs RLS gates —
but TRUNCATE is not one RLS can gate, and the local test shim granted
everything *except* TRUNCATE, so the suite had been starting from a safer
position than production. A shim kinder than the real thing hides the class
of finding it exists to catch.

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

**~~A provisional product is not a catalogue product.~~ — sidestepped.** This
warned that a quick-created row lives in a table the puller overwrites with
"server wins, always", so the first catalogue refresh would silently delete the
thing the cashier just sold. Slice 5 avoids it by never creating the row: one
placeholder product carries every unlisted line and the identity travels on the
line. Nothing provisional has to survive a pull, because nothing provisional
exists. `unknown_scans` is push-only and not in `ENTITIES`, so the puller never
touches it either.

**Not every sale line moves stock, as of slice 5.** `sales.py` wrote a
`stock_ledger` row for every line unconditionally; against the placeholder that
is the sum of unrelated products, drifting negative on a row nobody can act on.
The write path now honours `track_stock`, which also covers any loose or
weighed product the catalogue marks the same way. Slice 6's low-stock query
filters on `reorder_point > 0` and would have hidden this rather than surfacing
it — worth remembering that the reconciliation invariant stays *true* while the
data underneath it turns meaningless.

---

## Before starting

**Nothing is blocking.** Both prerequisites this document originally listed are
resolved:

- ~~**argon2 re-tuning**~~ — **done.** 5,274 ms to 1,808 ms in the Edge
  Function, and a timing oracle closed along the way
  (`docs/argon2-tuning.md`). It mattered here because a supervisor override on
  an adjustment is a PIN check with a customer waiting, and phase 6 adds more
  of them.
- **Opening stock** was never a prerequisite — it is the reverse.
  `stock_levels` is empty by design and slice 3's count flow is what seeds it,
  so phase 6 is what makes an opening count possible at all. Sequence it before
  go-live rather than after (plan §6 step 6).

Start at slice 1.

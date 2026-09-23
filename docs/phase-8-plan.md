# Phase 8 — Shifts and reporting

*Execution plan §3, weeks 18–20. Architecture §13.4–13.5, §18.*

**Exit criteria:** a full trading day opens and closes with a variance figure
the owner can actually explain — which means rounding gain/loss is reported
separately and not mistaken for cashier error. Z-report totals tie to the sum
of `payments` by method. Reports export to a file the owner can open without
the app.

Five deliverables, from the plan:

- Register sessions, opening float, cash movements with approval
- Shift close: counted vs. expected, variance, UPI attested totals listed
  separately, accumulated rounding shown as its own figure
- X/Z reports, day-close Edge Function
- Sales, margin and stock reports with permission-scoped columns
- Purchase orders, deferred from phase 6

Four **extensions** ride alongside and do not hold the phase open (execution
plan §3, added 21 September): the daily report to the owner, the low-stock
notification, fast repricing of loose goods, control barcodes at the till.
The **owner channel decision** gates the first two and is taken first.

---

## What already exists

Checked, not assumed.

| | Where | State |
|---|---|---|
| `register_sessions` | `001_init.sql:100`, `0001_core.sql:165` | Both schemas. `opening_float`, `counted_cash`, `expected_cash`, `variance`, `status`. **Nothing writes a row.** |
| `cash_movements` | `001_init.sql:197`, `0001_core.sql:286` | Both schemas, with `approved_by` for the override path. **Nothing writes a row.** |
| `sales.session_id` | `sales.py:60` | Column exists, parameter exists, defaults to `None`. **Every sale ever posted has a null session.** |
| `sales.rounding_adjustment` | both schemas | Written correctly since phase 4; never summed. |
| `payments.verified`, `confirmation_method` | both schemas | Written since phase 4. The Z-report's UPI split is already in the data. |
| `sale_reviews` outcomes | 0007 | `paid` / `not_paid`, append-only. The shift close's `requires_review` line reads these. |
| RLS on sessions | `0003_rls.sql:165–180` | Insert under `sale.create` and own `user_id`; select own or `report.sales.store`; **`UPDATE` under `shift.close`** — the one permitted mutation of a transactional row. |
| RLS on cash movements | `0003_rls.sql:246` | Insert under `cash.payout` and own `actor_id`. |
| `shift.close`, `cash.payout` | `permissions.py:29–30` | Supervisor, manager, admin. In `NO_API_SURFACE` — no route behind either. |
| `report.sales.store`, `report.margin` | `permissions.py:31–32` | Routes exist: `/sync/failures` and `/reports/margin`. The second **answers 501** by design. |
| `reports.product_margin` | `0005_reports.sql` | A security-definer view that returns nothing without `report.margin`. The only cloud report. |
| `sync_push` entities | `payloads.py:54` | `sale`, `sale_review`, `stock_movement`, `unknown_scan`, `override`, `audit`. **Insert-only, `ON CONFLICT DO NOTHING`.** No update path exists. |
| `close_shift` RPC | architecture §2 diagram | **Does not exist.** Neither does a day-close function. |
| Receipt renderer | `receipt_render.py` | Text and HTML from one document model, PDF on demand. The Z-report is the same shape. |
| `PaymentProvider.poll` | `payment_providers.py` | In `KNOWN_UNCALLED`. Still nothing to poll; UPI stays attested. |
| Shift UI | — | **Nothing.** No screen, no client function, no route. |

The picture: phase 1 laid the tables, phase 4 filled the payment columns the
close needs, and nobody has opened a shift. Every sale in Supabase belongs to
no session.

---

## The three decisions that shape this phase

### 1. A close is a row, not an update

`register_sessions_close` (0003) permits an `UPDATE` under `shift.close`, and
the architecture diagram names a `close_shift` RPC. Both assume the close
happens online. **It cannot be allowed to.** The shop closes at ten with the
line down as often as not, and a shift that cannot close offline is a shop
that cannot go home. The close must be local-first and ride the outbox, and
the outbox carries inserts.

So the close is an append-only `shift_closes` row — `session_id`,
`closed_at`, `counted_cash`, `expected_cash`, `variance`, the UPI figures, the
rounding figure, `closed_by` — pushed as a `shift_close` entity. This is the
`sale_reviews` pattern (§13.5): the session row says `open` forever, and the
close is the event that happened to it. A trigger on the cloud may mirror the
figures back onto `register_sessions` for convenience; the terminal never
updates a pushed row.

The `UPDATE` policy from 0003 is left in place and unused, with a comment.
Revoking it is a phase 10 tidy-up if nothing has needed it.

### 2. A sale needs an open session

`sales.session_id` has been null for the life of the project because nothing
required otherwise. The Z-report is *the sales of this session*, so from this
phase a sale cannot be posted without one. That changes the first minute of
every trading day: the till opens, someone with `sale.create` opens a session
with a float, and only then does the register accept a scan.

This is the one user-visible change in the phase that a cashier will notice,
and it is the right one — a float typed at 8am is the number the variance at
10pm is measured against, and a float typed at 10pm from memory is a guess.

`SaleService.post_sale` refuses without an open session, with a sentence.
Every existing sale test opens one in a fixture. Historic sales with a null
session stay null; the reports treat them as "before shifts existed".

### 3. The Z-report is the terminal's; the reports are the cloud's

Two different questions with two different sources of truth, and the phase
goes wrong if they are conflated:

- **The X/Z report** is *what this till did today*. It must render offline,
  from the local database, at the moment the drawer is counted. It is the
  cashier's and the supervisor's document.
- **The sales, margin and stock reports** are *what the shop did*. They read
  `server_received_at` (§9.4), never terminal time, so they are cloud-direct
  like the admin screens (phase 6 decision 2), gated by RLS, online-only, and
  say so. They are the owner's documents.

The **day-close function** is the bridge: once the `shift_close` row lands,
a cloud-side function recomputes the same figures from the cloud's `payments`
and compares. Agreement is the sync-health check; disagreement names a sale
the terminal has and the cloud does not, which is the failures queue's job to
explain. The exit criterion's "Z-report totals tie to the sum of `payments`"
is asserted twice — locally at close, and in the cloud after push — and the
two must agree.

---

## The arithmetic, written down before any of it is built

All integer paise. This lives in `app/domain/shift.py` under property tests
before a route exists — phase 2's rule, and this is the phase it matters most.

```
cash_sales      = Σ payments.amount        where method = 'cash', sale completed
upi_attested    = Σ payments.amount        where method = 'upi', verified = 0
upi_verified    = Σ payments.amount        where method = 'upi', verified = 1
cash_in         = Σ cash_movements.amount  where direction = 'in'
cash_out        = Σ cash_movements.amount  where direction = 'out'
rounding        = Σ sales.rounding_adjustment   (signed; negative is the shop's loss)
under_review    = Σ grand_total of sales with status = 'requires_review'
                  and no sale_reviews row, listed, never folded into takings

expected_cash   = opening_float + cash_sales + cash_in − cash_out
variance        = counted_cash − expected_cash
```

Three things to get right, each of which produces "an unexplainable variance
at every shift close" (§13.4) if wrong:

- **Rounding is already inside `cash_sales`.** A ₹123.40 basket paid in cash
  is a ₹123 payment row. `rounding` is reported *beside* the total as the
  explanation, not added to or subtracted from it. Adding it double-counts.
- **A split tender contributes to two lines.** ₹100 UPI + ₹23 cash is a
  ₹23 cash payment and a ₹100 UPI payment, and the rounding on that sale is
  the cash portion's rounding only.
- **`requires_review` sales are not takings.** The customer says they paid;
  the shop does not know. They are a line with a count and a sum, and they
  do not enter `expected_cash` or the UPI totals until a supervisor resolves
  them — and a `not_paid` outcome means they never do.

Property to assert: for any generated day, `expected_cash − opening_float −
cash_in + cash_out` equals the sum of cash payments to the paisa, and the
rounding figure equals the sum of `(basket_total − cash_collected)` over
cash-paid sales. If either property needs a special case, the model is wrong.

---

## Slice 1 — Open a session, and make sales belong to it

*Local only. No sync, no cloud change.*

- Local migration `008`: `shift_closes` table; `sales.session_id` stays as
  is. Cloud migration `0024` for the same table plus its insert policy under
  `shift.close` and own `closed_by`.
- `ShiftService.open(session, opening_float)` → a `register_sessions` row,
  one open per terminal, refused if one is already open.
- `SaleService.post_sale` requires the open session and writes its id.
- `POST /shifts/open`, `GET /shifts/current`. Both under `sale.create`: the
  cashier opens their own shift, as the RLS insert policy already says.
- Cash movements: `POST /shifts/cash` with direction, amount, reason, under
  `cash.payout`. A cashier who does not hold it goes through the override
  modal from phase 7 — this is the first use of `cash_movements.approved_by`,
  and it is the third overridable act after discount and void.
- A register screen that will not scan until a shift is open, and a one-field
  dialog to open it.

**Prove it:** start the till, scan — refused with "open a shift first". Open
with ₹500. Scan — accepted; the sale row carries the session id.

## Slice 2 — Close it, and the Z-report

*Local only. The domain first, then the document.*

- `app/domain/shift.py`: the arithmetic above as pure functions over plain
  rows, Hypothesis-tested.
- `ShiftService.close(session, counted_cash)` → the `shift_closes` row, in
  one transaction with an audit row, queued to the outbox. Refused if a sale
  is mid-tender.
- `ZReport` document model beside `Receipt`, rendered to text and HTML by the
  existing renderer pattern; PDF on demand via the same path as receipts.
- `POST /shifts/close` under `shift.close`; `GET /shifts/{id}/z` returns the
  document; `GET /shifts/current/x` is the same document over an open
  session, read-only, marked X.
- The close screen: counted cash typed, expected shown *only after* counting
  (a supervisor who sees expected first types expected), variance and
  rounding on separate lines, UPI attested and verified separate,
  `requires_review` listed by receipt number.

**Prove it:** ten sales across cash, UPI and split, one under review, one
₹50 cash-out for tea. Close with a deliberately wrong count. The variance is
the amount you were wrong by, the rounding line is a few rupees, and the
review line names the one sale. Kill the till mid-close; reopen; the session
is still open and nothing was written.

## Slice 3 — Push it, and the day-close check

- `shift_close` and `register_session` join `SUPPORTED_ENTITIES`;
  `sync_push` accepts both (0024). The session pushes when opened, the close
  when closed; a close arriving before its session is the ordinary
  out-of-order case the outbox sequence already prevents.
- `pos.day_close(session_id)` — SQL, not an Edge Function, because it needs
  no secret and PostgREST can call it. Recomputes the figures from the
  cloud's `payments` and `cash_movements` and returns them beside the
  terminal's. A trigger runs it on `shift_closes` insert and writes the
  comparison to a `shift_close_checks` table.
- `GET /shifts/{id}/check`, cloud-direct: the two columns side by side, and a
  sentence when they differ — *"the cloud has 41 sales for this shift, the
  till closed with 42; one is in the failures queue"*.

**Prove it:** close a shift with the line down. Reconnect. The close lands;
the check agrees to the paisa. Then quarantine one sale on purpose (the
phase 7 `refuses` trick) and close again: the check names the gap.

## Slice 4 — The owner's reports

*Cloud-direct, online-only, `report.sales.store`; margin columns appear only
under `report.margin`, by RLS not by screen.*

- `reports.sales_by_day`, `reports.sales_by_product`, `reports.stock_position`
  as security-definer views in the `reports` schema, each guarded like
  `product_margin`. Date range on `server_received_at`.
- `GET /reports/sales`, `/reports/products`, `/reports/stock`. The 501 on
  `/reports/margin` finally goes: it becomes a column set on
  `/reports/products` that is empty for a caller without the key — the same
  shape the view already has.
- A Reports tab under Admin: date range, the three tables, **Export** to CSV
  (the file the exit criterion asks for; a PDF is the Z-report's job, not
  this screen's).
- A margin column that is absent for a supervisor and present for a manager,
  asserted at all three layers as the permission matrix already does for
  everything else.

**Prove it:** a manager exports yesterday's sales to CSV and opens it in
Excel; the total matches the Z-report's grand total for the same day. A
supervisor's export of the same report has no cost or margin columns.

## Slice 5 — Purchase orders

*Deferred from phase 6, sequenced here "because a PO is only worth raising
once there is a reorder report to raise it from".*

- `purchase_orders` and `purchase_order_lines`, both schemas, with a
  `supplier` text field rather than a supplier entity — the v1 fence (§8)
  excludes a supplier portal, and a name on a document is not a portal.
- Raise from the Low stock tab: every product at or below its reorder point,
  quantities editable, one supplier per order.
- Receive against it: `POST /inventory/receipts` gains an optional
  `purchase_order_id`; lines received are matched to order lines by product;
  a partial delivery leaves the order `open` with outstanding quantities;
  the order closes when every line is met or is closed short with a reason.
- Pushed as `purchase_order` (insert) and `po_receipt` (the matching, an
  append-only row per receipt against an order) — the same event-not-update
  shape as the shift close.

**Prove it:** low-stock list shows three products; raise an order; receive
two of the three; the order shows one outstanding; close it short; the audit
trail has the reason.

---

## Extensions

Not the gate. Each has a card; each is sequenced after the slice it depends
on.

| Extension | After | Blocked by |
|---|---|---|
| **Decision — the owner channel** | nothing; take it first | the partner's call |
| Daily report to the owner | slice 3 (day close is the trigger) and slice 4 (the content) | the channel |
| Notify the owner when stock runs low | slice 3 (a once-a-day hook) | the channel |
| Fast daily repricing of loose goods | nothing | — |
| Control barcodes at the till | nothing | a decision on how a code that *completes a sale* is kept from being scanned by accident |

The channel decision is the one to take this week, before slice 1, because
it is not engineering and it gates two features. Record it in execution plan
§2 beside the other week-1 decisions.

---

## What to watch

**The three decisions above are the phase.** Each one, taken the other way,
produces a shift close that works in the office and fails in the shop: an
update that cannot push offline, a Z-report over sales that belong to no
shift, or a report whose numbers disagree with the drawer because one reads
terminal time and the other reads server time.

**The arithmetic is where the pilot will find the bug.** §13.4 says it
plainly: getting rounding wrong "produces a permanent, unexplainable variance
at every shift close." Every till in India rounds; every owner counts the
drawer. If the figure is ₹3 wrong every day, the owner will conclude the
cashier is stealing ₹3 a day, and that is a worse outcome than the software
crashing. The Hypothesis properties are not optional.

**Expected before counted is the classic shift-close cheat.** The close
screen shows the expected figure only after the counted one is committed.
That is a design rule, not a UI preference, and it wants a test that the
response to `GET /shifts/current/x` does not include `expected_cash` while
the session is open. (X-reports show sales; they do not show the drawer.)

**A sale that needs a session will break every existing sale test.** That
is fine and expected; do it in one commit with a fixture, not across five
with `session_id=None` sprinkled about.

**Carry the house rule.** A day-close check whose passing state is "the
figures agree" is indistinguishable from one that compared nothing. Slice
3's proof includes a deliberate disagreement.

**The PO is the most likely thing to grow.** Approval workflows, supplier
records, price lists — all on the fence, all one "while you're in there"
away. The card says what a PO is here: a list of products and quantities
with a supplier's name on it, that a delivery can be matched against.
Nothing more until the pilot asks.

---

## Definition of done

Plan §5, all six, as for every phase — and the exit criteria at the top,
proven by a run sheet in the shape of `phase-7-acceptance.md`: open a shift,
trade a day (cash, UPI, split, a review, a payout), close it, read the
Z-report, watch the close push and the check agree, export the day to a
file, raise and receive an order.

---

## Slices 1 and 2 — shipped, 22 September

Delivered together, with slice 3's push half brought forward, because the
guards made the seams honest: a `register_session` row queued at open was
quarantined by a payload builder that had never heard of it, and a
repository method for the Z-report with no route yet was reported by
`check_dead_python.py` and deleted rather than listed.

**What exists now.** `app/domain/shift.py` and its two Hypothesis
properties. Migration 008 (`shift_closes`, one open shift per terminal by
partial index, `store_id` on the local session). `ShiftRepository`,
`ShiftService`, and five routes under `/shifts`. `SaleService.post` and the
register's cart-open both refuse without an open shift. The `till` fixture
opens one; every existing sale test passed unchanged once it did, which was
the plan's own bet. The register shows an "Open the day" dialog on a 409,
wearing the same scan shield as the PIN field. Cloud migration 0024:
`shift_closes`, three `sync_push` branches derived from 0023, and the
insert policies widened the way 0009 widened them — the pusher holds
`sale.create` and belongs to the store; the actor columns are provenance.

**Two things decided on the way.**

- **`cash.payout` is not overridable yet**, but the reason changed. It was
  the policy; it is now only the plumbing (`approved_by` carried into the
  row, the modal on the cash screen).
  `test_a_payout_pushes_under_a_colleagues_claim_since_0024` says so.
  Slice 3 may add it to `OVERRIDABLE`.
- **`shift_closes` is read-gated on `report.sales.store` alone**, so the RLS
  matrix discovers it as key-gated and asserts it for every role without a
  line being written by hand. The push therefore catches `unique_violation`
  rather than checking for a conflict, as the audit branch does.

**Proven live, 22 September, `3d31c10`.** The register showed *Open the day*
on sign-in; a scanned barcode into the float field was refused; ₹500 opened
it. One ₹500 sale pushed after its session — in Supabase the session row
says `open`, float 50000, with receipt ST01-T1-000019 joined to it, the first
sale in the project with a non-null `session_id`. The X-report carried
`expected_cash: null`. A close with a deliberately wrong count (₹537 against
an expected ₹1,000) returned `variance −46300, rounding 0, sales_count 1`,
and the `shift_closes` row in Supabase reads the same four numbers.

**Still to do in this phase's core:** the rendered Z-report (text, HTML,
PDF — the document, not the numbers), the close screen, the day-close check
in the cloud (slice 3's second half), the owner's reports (slice 4), and
purchase orders (slice 5).

## Slice 2b — the Z-report and the close screen

**The document.** `app/domain/zreport.py` is the stored close with names and
times around it; nothing is recomputed, so a review resolved or a payment
edited after the close leaves the Z where it was (asserted). One `rows()`
list feeds text, HTML and PDF; the PDF writer is now shared with receipts
(`write_text_pdf`). Variance and rounding are separate lines, and so are UPI
attested and UPI verified. Sales still under review at the moment of the
close are listed by receipt number and are in no total. `GET
/shifts/{id}/z` and `POST /shifts/{id}/z.pdf` need `shift.close` — the Z
shows the variance. The close response carries the rendered Z.

**The close screen.** Reached from *Close shift* in the register header
(`shift.close` only). Before the count it shows the sales count and the
under-review receipts and no cash figure; expected, variance and rounding
appear only with the committed close. The count field wears the scan
shield.

**`cash.payout` is overridable.** The session now remembers who lent each
grant (`Session.approvers`, `approver_for()`); `/shifts/cash` writes that
supervisor into `cash_movements.approved_by` and the audit row. *Cash out*
is in the register header for everyone — a cashier's 403 opens the
supervisor modal, which performs the payout itself. `shift.close` stays
unlendable: a close is the supervisor's count.

**A crash mid-close** (failure after the row, before the outbox) leaves the
shift open with no close, no audit and nothing queued; the retry succeeds.

## Slice 3 — the day-close check

**Migration 0025, `public.day_close_check(session_id)`.** SQL, security
invoker, exposed by PostgREST as `rpc/day_close_check`. It recomputes the
nine figures from the cloud's own `sales`, `payments`, `sale_reviews` and
`cash_movements`, rule for rule with `app/domain/shift.py` (a review counts
only if resolved by the close), and returns them beside the stored close.
A caller without `report.sales.store` gets no rows, the same as a close that
has not arrived.

**Changed from the sketch above: computed on demand, not by a trigger into
a `shift_close_checks` table.** A trigger checks at the one moment the
answer is most likely wrong — a quarantined sale retried tomorrow lands
after the close, and a stored check would say "one missing" for ever. On
demand, the check converges as late rows arrive, with no second table.

**On the till.** `GET /shifts/{id}/check` (`shift.close`, cloud-direct:
503 offline, 404 until the close has pushed) returns both columns and a
sentence from `app/domain/close_check.py`. When the counts differ it looks
in this till's outbox and says how many of the shift's sales are waiting to
push and how many are in the failures queue. UPI verified after the close is
reported as that, not as a discrepancy. The close screen has *Check against
the cloud*.

---

## Before starting

Nothing is blocking. Phase 7 is closed with its debts paid, the guards are
green at 277 / 48, and the refresh-token check is verified live. The two
pieces of P7 debt still open — `stock.receive` admitting an adjustment, and
`auth.revoked` with no listener — are not in this phase's path.

Take the channel decision. Then start at the arithmetic.

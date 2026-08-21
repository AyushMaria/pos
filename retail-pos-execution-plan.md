# Retail POS — Execution Plan

Companion to `retail-pos-architecture-python.md`. That document says *what* to build; this one says *in what order, by whom, and how you know a phase is finished.*

**Scope note: this is a software-only project, with one deliberate exception — the barcode scanner.** No thermal printer, cash drawer, scale, or customer-display integration is in v1. Receipts are digital and cash is counted by hand into a till.

**A USB barcode scanner is in scope and already in use** — a Helett HT20 Pro on the target counter, confirmed scanning into the register. It crosses the fence because it costs almost nothing to cross it: an HID wedge presents itself to the OS as a keyboard, so the entire integration is the capture hook (architecture §10.1), which had to exist anyway for typed entry. Every other peripheral needs a driver, a transport, a worker thread and a certification pass per model. The scanner needs none of those.

Barcode *parsing* was always fully in scope and is unaffected — the codes arrive as text either way and still have to be understood correctly. Remaining device integration is deferred, not cancelled; see §8.

---

## 0. Assumptions

Stated so you can correct them — several materially change the schedule.

| Assumption | If it's wrong |
|---|---|
| 2 developers (1 Python-leaning, 1 React-leaning) + 1 part-time owner/PM | 1 dev ≈ ×1.8 elapsed time; 3+ devs saves less than you'd hope until phase 5 |
| Single store, single terminal for v1; multi-store is a later concern | Multi-store from day one adds ~2 weeks across phases 1, 7 and 8 |
| Windows-only deployment | Cross-platform adds ~1 week in phase 9 (signing, packaging) |
| Cash + UPI only; no card | Card adds 3–4 weeks, returns PCI scope, and would reintroduce hardware |
| UPI is received against the shop's existing printed counter QR, and confirmed by the cashier | If a shop has no standee, or wants the till to present the QR, QR rendering and a way to show it to the customer come back — roughly a week |
| GST-inclusive pricing (India retail) | Tax-added-at-tender changes the pricing engine and invoice layout |
| Digital receipts are acceptable to the store and to GST invoice rules | If a printed invoice is legally or practically required, peripherals come back and add ~3 weeks |
| A USB HID scanner is on the counter (Helett HT20 Pro — confirmed, not assumed) | If a store has no scanner, entry falls back to the keyboard and the week-8 speed risk returns in full |
| A real pilot store is available from week 13 | No pilot access is the single biggest schedule risk — see §7 |

**Baseline: ~26 weeks to a pilot-ready single-terminal till.** Treat the weekly figures as relative weight, not commitments.

---

## 1. Sequencing principle

**Build one thin vertical slice through every layer before widening any of them.**

The failure mode for a system like this is spending six weeks on a beautiful catalog admin screen before discovering the GST breakdown is wrong on multi-rate baskets or that the barcode field eats fast keyboard input. So phase 3 targets an ugly but complete path — enter a barcode, add a line, take cash, produce a receipt, close the sale — and everything after that widens the slice.

Corollary: **do not build the admin UI early.** Seed the catalog with a CSV import script and a few SQL inserts. The admin screens are phase 6 work.

---

## 2. Phase 0 — Setup (week 1)

Nothing here is optional and almost none of it is coding.

**Close these decisions.** Each one costs rework if answered in week 10 instead of week 1:

- [x] Single vs. multi-store — determines `store_id` scoping everywhere — single store
- [x] Tax model: GST-inclusive vs. added at tender — GST-inclusive
- [x] Cash rounding to nearest ₹1, or exact with coins — nearest ₹1, applied at tender only, never on line items or the GST base
- [x] UPI VPA per terminal or per store (per terminal makes settlement matching far easier) — per terminal *where the counter's printed QR allows it*. The shops pay into an existing standee QR rather than one the till generates, so in practice this is settled by whatever is already stuck to the counter, not by configuration (architecture §13.3).
- [x] Target OS and Python version floor (3.14+ gives stdlib `uuid7`) — Windows; **runs on 3.10+ in practice**. 3.14 was not available when phase 1 was built, so `domain/ids.py` carries an RFC 9562 `uuid7()` that the stdlib call simply replaces when the floor moves. CI runs 3.12 and 3.14.
- [x] Receipt delivery: on-screen only, PDF, or share by link/WhatsApp — decides the renderer and whether customer contact details are captured — **on-screen, with PDF on demand.** Built in phase 3 from a single receipt document model.
  - WhatsApp sharing is wanted and is *not* built. It needs a customer phone number captured at the till, which is a separate decision about what the shop stores about its customers and where — not a rendering question. The PDF is the prerequisite for it either way, so none of the phase 3 work becomes rework.

- [x] **Every product carries a barcode — no exceptions.** Decided after M1. A product with no code cannot be created and cannot be sold: the catalogue import assigns an internal `21…` code to anything the manufacturer did not code, and weighed goods get a generated `22…` code at the counter. This makes name search a fallback rather than a daily path, and it is the reason the no-barcode tail is no longer treated as a risk (§6, §9).

**Stand up the skeleton:**

- Repo with the module layout from architecture §3, minus `app/peripherals/`
- Supabase project (dev), plus a local Supabase for tests
- CI: `ruff`, `mypy --strict` on `domain/`, `pytest`, `import-linter` enforcing the dependency rule
- Issue tracker seeded with the phase list below

**Start the code-signing certificate application now.** EV certificate issuance takes 1–6 weeks of identity verification and is on the critical path for phase 9 — but only if you forget about it until then. This is the one long-lead item left in the plan.

---

## 3. Phases

### Phase 1 — Foundation (weeks 2–3)

*Goal: a window that opens, authenticates, and talks to a local database.*

| Track | Work |
|---|---|
| Backend | Postgres schema + RLS policies + roles/permissions seed; SQLite migration runner; repository base |
| Shell | `main.py` bootstrap, uvicorn-in-thread, health gate, splash, single-instance lock |
| Security | Session token, `Host`/`Origin` middleware, ephemeral port |
| Auth | Supabase login, JWT custom access token hook, keyring storage, PIN hashing (argon2id tuned to ~100 ms) |

**Exit criteria:** app launches to a login screen; a seeded cashier logs in with employee code + PIN; `/health` reports schema version; RLS denies a cashier reading `report.margin` columns, proven by a test.

### Phase 2 — Domain core (weeks 4–5)

*Goal: the business rules, fully tested, with no UI and no database.*

- `Money` value object, integer minor units, `ROUND_HALF_UP` at the tax boundary
- Cart, line pricing, discount stacking, GST calculation, promotions
- Nearest-₹1 rounding as a distinct, separately tested step at the tender boundary — the rounding adjustment is its own recorded figure, not a silent adjustment to the total
- Barcode parser: EAN-13/UPC-A checksums, Code128/39, ITF-14 pack sizes, GS1-128 AIs, embedded weight/price prefixes
- Hypothesis property tests throughout

**Exit criteria:** `pytest domain/` runs in under 2 seconds with no fixtures; a lint rule rejects `float` in `domain/`; parser handles a corpus of real barcode numbers collected from actual stock; a property test proves rounding adjustments never exceed ₹0.50 and always reconcile against the unrounded total.

> This phase feels like a detour because nothing is visible at the end of it. It is the highest-value two weeks in the plan — these are the rules most likely to be wrong and cheapest to fix now. With hardware out of scope it is also, by some distance, the phase carrying the most risk.

### Phase 3 — Register MVP (weeks 6–8)

*Goal: the vertical slice. A complete cash sale, end to end.*

| Track | Work |
|---|---|
| Backend | Cart endpoints, `PaymentProvider` protocol + `CashProvider`, attempt state machine, split-tender balance loop, tender-dependent rounding, `post_sale` single transaction |
| Receipts | Receipt document model, HTML/PDF renderer, GST breakdown and rounding line, reprint by sale id |
| UI | Register screen, barcode entry field with fast-input handling, global scan capture, product search fallback, line list, tender screen, WebSocket event client |

**Exit criteria:** enter 10 items by barcode and by search, void a line, take cash, produce a legible receipt with a correct GST breakdown and a visible rounding line, sale lands in SQLite with balanced totals. Kill the process mid-sale and confirm no partial sale exists. Scan ten items with the real scanner, including one scanned while the tender dialog is open, and confirm each lands exactly once.

**Milestone M1: first complete sale. ✅ Done.** A non-developer ran the register with the scanner.

The open question going in was the no-barcode tail — 38% of the migrated catalogue, findable only by typing a name. **That is now closed by a product decision rather than by a UI change:** every product will carry a barcode, so name search becomes a fallback for when a code will not read, not a path anyone walks twenty times a shift. The 38% is a migration job (§6), not a counter workflow.

One thing the decision does not settle, and it belongs to phase 6 rather than here: **an internal code that is never printed cannot be scanned.** Assigning `21…` to loose potatoes makes the catalogue complete; it does not put a code on the potatoes. Whatever is sold loose or by weight still has to be *selected* somehow — a shelf label the cashier scans, a quick-key grid, or the search — and which of those it is decides how much the weighed-item flow (architecture §10.3) needs at the counter.

### Phase 4 — UPI tender (weeks 9–10, now smaller) — ✅ **done**

**The shops already have a printed UPI QR on the counter, and there is no customer-facing screen.** So the till never renders a QR: no deep-link construction, no `qrcode` dependency, no second window, no scan-from-arm's-length test. The customer pays the standee; the POS records that it happened.

- `UpiProvider` — attestation-only, no QR surface
- Manual attestation with audit row, capturing the UTR and **the amount actually paid**, attempt expiry and auto-cancel
- `requires_review` queue and supervisor resolution
- Split cash + UPI flow end to end, including where rounding lands when only part of the basket is paid in cash

**One consequence of the static QR, and it is the reason to read §13.3 before building this:** the payment carries no `tr`, so nothing in the bank statement points back at a sale. Reconciliation is by amount and time, and the customer types the amount themselves — so a UPI payment can arrive short or over. The attested amount is an input defaulting to the outstanding balance, never an assumption.

**Exit criteria — all met:** all three tender combinations complete correctly; a short UPI payment leaves a balance the cashier can settle in cash without leaving the sale; an expired attempt releases the cart cleanly; an attested UPI sale is visibly distinct from a verified one in the data (`verified = 0`, `manual_attestation`); a UPI-only sale carries no cash rounding adjustment.

Two things surfaced during the build and are worth knowing:

- **Rounding was being settled at the wrong moment.** It was applied when the *first* tender was chosen, which is right for cash-first and wrong for UPI-first: it rounded the whole basket at a moment when nothing was being paid in cash, then collected the cash remainder unrounded — the exact inversion of §13.4. It now settles when cash is chosen, against the balance outstanding then.
- **The RLS suite's migration list was hand-maintained and had fallen a migration behind.** It is discovered from the directory now. A security suite quietly testing a schema older than the one that ships is worse than one that fails.

*The MVP answer to “did the money arrive?” is a cashier tapping Received. That is how these shops already work, and it is deliberately temporary — a payment terminal (Pine Labs or equivalent) is the intended replacement and brings card with it. Everything routes through `PaymentProvider` so that swap is a new provider, not a rewrite.*

### Phase 5 — Sync engine (weeks 11–13)

*Goal: the terminal stops being an island.*

- Outbox drain with backoff, idempotent replay, quarantine to `sync_failures`
- Watermark pullers per entity, tombstone handling, polling loop
- `sync_push` RPC server-side with `ON CONFLICT DO NOTHING`
- Stock ledger deltas, server-side `stock_levels` trigger
- Sync status UI and backlog indicator

**Exit criteria — the chaos test:** run 200 sales offline, reconnect, confirm exactly 200 arrive. Kill the app mid-push and confirm no duplicates. Drop the network mid-sale and confirm the cashier notices nothing. Corrupt one payload and confirm it quarantines without blocking the queue.

**Milestone M2: offline-capable.** This is the point where it becomes a system rather than a program.

### Phase 6 — Inventory and catalog (weeks 14–15)

- Product/barcode/price admin screens (the first real admin UI)
- Goods receipt against POs, pack-size multipliers on ITF-14 codes
- Stock counts and adjustments
- Internal `21…` prefix code assignment for products with no manufacturer barcode
- Unknown-barcode quick-create and the `unknown_scans` queue
- Low-stock alerts

**Exit criteria:** receive a delivery by entering case codes, count a section, and have `stock_levels` reconcile against the ledger.

### Phase 7 — RBAC completion (weeks 16–17)

- Manager override flow: modal, Edge Function, 90-second grants
- Offline permission snapshots with signed TTL and revocation on sync
- `<PermissionGate>` audit across every privileged control
- Audit log viewer

**Exit criteria:** the permission matrix from architecture §11.1 exists as a parametrised test that passes at all three layers — UI, FastAPI dependency, RLS. Disconnect the network and confirm override still works; advance the clock past the TTL and confirm it stops.

### Phase 8 — Shifts and reporting (weeks 18–20)

- Register sessions, opening float, cash movements with approval
- Shift close: counted vs. expected, variance, UPI attested totals listed separately, accumulated rounding shown as its own figure
- X/Z reports, day close Edge Function
- Sales, margin and stock reports with permission-scoped columns

**Exit criteria:** a full trading day opens and closes with a variance figure the owner can actually explain — which means rounding gain/loss is reported separately and not mistaken for cashier error. Z-report totals tie to the sum of `payments` by method. Reports export to a file the owner can open without the app.

### Phase 9 — Fleet operations (weeks 21–23)

- PyInstaller one-folder build, Inno Setup installer
- Code signing (certificate from phase 0), antivirus reputation submission
- Signed `version.json` manifest, background download, verify, apply-on-restart, rollback
- Crash supervision (Windows service or scheduled task), rotating structured logs, diagnostics screen
- Database location outside install dir, backup on shift close

**Exit criteria:** install from scratch on a clean machine, run a sale, push an update, confirm the database survives it, then roll back. Confirm Defender and one third-party AV don't quarantine the binary.

### Phase 10 — Pilot hardening (weeks 24–26)

Reserved deliberately. Every deployment surfaces work you cannot predict from a desk: a product name that breaks the receipt layout, a cashier workflow nobody anticipated, a supplier whose barcodes are all duplicates. Do not fill this time in advance.

---

## 4. Parallel tracks

The two developers should not work strictly sequentially through the phases:

```
Weeks:      2─3    4─5    6─8      9─10    11─13   14─15   16─17  18─20  21─23
Dev A (Py): P1 ────P2 ────P3 back──P4 ────P5 ─────P6 back──P7 ────P8 ────P9
Dev B (JS): P1 shell ─────P3 UI ───P4 UI ──P6 UI ──P6 UI ───P7 UI ─P8 UI ─pilot prep
```

Dev B's idle-ish window is weeks 4–5 while the domain is being built headless. Use it for the design system, the register screen prototype against a mocked API, and the barcode entry field — which needs to handle very fast typed input, out-of-order key events and Enter-terminated codes without losing characters, and is worth isolating early.

**Status: built and under test** (`useBarcodeCapture`, 17 Vitest cases covering the timing rules). One trap is worth knowing before anyone touches it again: the register's own barcode field must be **excluded** from the global capture hook, not included in it. That field is an ordinary input — the scanner types into it and Enter submits the form. If the global hook also fires there, every scan travels two paths at once, and which one wins depends on the order `preventDefault` happens to run in. The hook exists for the case the field *cannot* cover: a scan while focus is somewhere else, such as an open tender dialog.

**Contract-first:** freeze the API shapes in phase 1 as Pydantic models and generate TypeScript types from the OpenAPI schema. Without this, the two tracks diverge and you lose a week reconciling them.

---

## 5. Definition of done (every phase)

A phase is not done until:

- [ ] Tests pass in CI, including the permission matrix
- [ ] It runs from the **packaged build**, not the dev environment
- [ ] It works with the network disconnected, or fails with a message a cashier can act on
- [ ] No new `float` in money paths, no new direct SQLite connections outside repositories
- [ ] Audit rows exist for anything a manager would need to investigate later
- [ ] A non-developer has used it for 15 minutes

That last one catches more real defects than any of the others.

---

## 6. Catalog migration

Usually underestimated, and it blocks the pilot rather than the build. Start at week 18, in parallel:

1. **Export** from the existing system or spreadsheets. Expect junk.
2. **Clean:** duplicate barcodes (the most common problem — the same code on two products), missing check digits, prices as text, products with no barcode at all.
3. **Map** to the schema: categories, tax codes, UOM, `is_weighed` flags.
4. **Dry-run import** into a scratch SQLite; produce an exceptions report for the owner to resolve by hand.
5. **Assign internal codes** to everything with no manufacturer barcode — 38% of the current export, and the largest single finding from profiling it. This is now **mandatory rather than advisable** (§2): a product without a code cannot be created or sold, so the import must leave nothing uncoded and the exceptions report in step 4 must be empty before go-live, not merely short.

   An internal code makes the product *sellable*; it does not make it *scannable*, because nothing prints these labels yet. Decide per product which of the two it needs: a packaged good that simply lacks a manufacturer code needs a printed shelf or product label; something sold loose or by weight needs the weighed-item flow instead. Budget time for that triage, and for renaming — the names are still what the fallback search matches on.
6. **Opening stock count** the day before go-live; it seeds the ledger and it cannot be rushed.

Write the importer as a repeatable script, never a one-off. You will run it more than once.

---

## 7. Pilot and rollout

**Weeks 24–26, one store, one terminal.**

**Shadow week first.** The new till runs alongside the existing one for a week, on the same real transactions where practical. Compare totals daily. This is the only cheap way to find pricing and tax discrepancies.

**Go/no-go criteria before it becomes the primary till:**

- [ ] 500+ transactions with zero data loss
- [ ] Two full days offline with clean catch-up
- [ ] Shift close variance under ₹50 for three consecutive days, or explainable
- [ ] Cashiers rate the register screen usable without prompting
- [ ] Basket entry is fast enough at the morning rush — measured, not assumed
- [ ] Rollback plan rehearsed — the old system can be back in an hour
- [ ] Digital receipts confirmed acceptable to customers and compliant with local GST invoice requirements

**Training:** one page, next to the till. Enter an item, tender, void with override, close shift, what to do when it says offline. Anything longer will not be read.

**Support:** for the first fortnight, a developer is reachable during trading hours and someone visits during the morning rush. You will learn more in one busy hour on-site than a month of remote logs.

Only after 4 weeks of stable single-store operation should terminal 2 be added — that is the first genuine test of multi-terminal sync and receipt sequencing.

---

## 8. Scope fence for v1

Explicitly **not** building, to protect the schedule. Each is a genuine feature that can wait:

**Hardware integration** — thermal receipt printing · cash drawer control · weighing scales · customer-facing display · shelf and product label printing

> USB barcode scanners were on this list and have been taken off it. An HID wedge is indistinguishable from a keyboard, so it needs no driver, no transport, no worker thread and no per-model certification pass — the cost of supporting it is a hook that had to exist anyway for typed entry. Nothing else on the line above is remotely that cheap, and taking the scanner off is not a precedent for taking anything else off.

**Features** — card payments · loyalty and customer accounts · multi-store consolidated reporting · e-invoicing/e-way bill integration · supplier portal · purchase order approval workflows · promotions beyond simple percentage and fixed-amount · gift cards · layaway · employee time clock · mobile companion app · Tally/accounting export · returns without a receipt · serial-number tracking · expiry-batch FEFO enforcement

Write this list somewhere the owner can see it, and revisit it only after the pilot. Of everything on it, peripherals are the most likely to come back first — see §9.

---

## 9. Risk checkpoints

| Week | Check | If it fails |
|---|---|---|
| 1 | Signing certificate applied for | Long lead time; slipping it delays phase 9 |
| 5 | GST breakdown and ₹1 rounding correct on a mixed-rate basket, verified against a real invoice | Tax and rounding errors found at pilot are the most expensive class of defect here |
| 6 | The store's own scanner drives the register end to end | Cleared early — a Helett HT20 Pro scans into the till today. **Recheck on the pilot machine**: the terminator suffix is configurable, and a scanner set to send Tab instead of Enter never completes a scan at all |
| 8 | M1 demo — a 20-item basket at realistic speed | ✅ Passed. Entry speed is no longer the live risk: the scanner covers it, and every product will carry a barcode by policy (§2) |
| 15 | Every product in the imported catalogue has a code, and everything sold loose has a way to be *selected* at the counter | A complete catalogue is not the same as a scannable shelf. If loose goods have no answer by the end of phase 6, the gap surfaces during the shadow week, which is the worst place to find it |
| 8 | Owner confirms a digital receipt is acceptable in practice | If customers or GST rules demand paper, peripherals return and the schedule moves by ~3 weeks |
| 13 | M2 chaos test passes | Sync defects found after go-live mean lost sales and lost trust |
| 18 | Catalog export obtained and profiled | Bad source data is the most common cause of pilot slippage |
| 21 | Antivirus doesn't quarantine the signed build | Budget for reputation-building submissions |
| 24 | Shadow-week totals match the existing till | Do not go live on mismatched numbers, ever |

---

## 10. Week 1 checklist

1. ~~Answer the last open decision in §2 — receipt delivery~~ — closed: on-screen, with PDF on demand
2. Begin the code-signing certificate application — **still open, still the only long-lead item, still blocking phase 9**
3. Create the repo, module skeleton, CI with `import-linter`
4. Create dev Supabase project; commit the initial migration
5. Obtain a sample product export from the existing system — just to look at it
6. Collect 50 real barcode numbers from actual stock, including a case code and something imported, as the phase 2 test corpus — done at scale instead: the full 32k-row export was profiled. Worth recording that it contained **no scale-printed labels at all**, consistent with the target shops being too small to own a weighing scale, and the reason the POS generates its own `22…` weighed-item codes
7. Confirm with the store owner that a screen-and-PDF receipt is workable at the counter — now *after* it is built, so confirm it against the real thing rather than a description

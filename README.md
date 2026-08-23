# Retail POS

A single-terminal till for Indian retail: offline-first, GST-inclusive pricing,
cash and UPI. pywebview shell · React/TypeScript UI · FastAPI local service ·
SQLite on the terminal · Supabase in the cloud.

See [`retail-pos-architecture-python.md`](retail-pos-architecture-python.md)
for what is being built and
[`retail-pos-execution-plan.md`](retail-pos-execution-plan.md) for the order it
is being built in.

**Status: phases 1–5 complete. M2 ("offline-capable") passed on real hardware
against a live Supabase project.** The till opens, authenticates a cashier
against the cloud or — for up to fourteen days — against a local snapshot, and
Postgres refuses a cashier the margin columns. The business rules — money, GST,
discounts, the cart, cash rounding and barcodes — are complete and
property-tested. A cashier can sign in, ring up a basket by barcode or by name
against the real catalogue, void a line, take cash or UPI, and get a receipt
with a correct GST breakdown.

The till is no longer an island. It sells with the network unplugged, queues
what it takes, and drains the queue when the line comes back; a sale refused by
the cloud is quarantined rather than blocking the ones behind it, shows on the
badge, and can be retried by a manager once the cause is fixed. All five M2
claims hold — nothing lost, nothing duplicated, stock moved once, no sale
waiting on the network, refusals visible and recoverable.

Running M2 against a real project found seven defects that the suite could not
have caught, every one of them in the seam between the terminal and a hosted
Supabase rather than in the code either side. `docs/m2-test-guide.md` lists them
with their symptoms; each has a test now. `docs/existing-project-setup.md`
covers standing the schema up on a project that already holds other tables.

---

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate                # source .venv/bin/activate on POSIX
pip install -e ".[dev]"

python scripts/seed_local.py          # three staff, a demo store
python scripts/import_catalog.py      # products to sell (needs the legacy .env)
python -m app.main                    # opens the till
```

**`import_catalog.py` is for a terminal with no cloud behind it.** It generates
its own product ids, so running it against a terminal that already syncs leaves
two unrelated catalogues and every sale is refused with a foreign key error on
`sale_lines`. Once a Supabase project is configured the catalogue arrives by
itself — see `docs/existing-project-setup.md`.

Sign in with one of the seeded accounts:

| Code | PIN  | Role       |
|------|------|------------|
| C001 | 4913 | cashier    |
| S001 | 7261 | supervisor |
| M001 | 5820 | manager    |

These are development credentials, published in this repository. They exist so
the till can be demonstrated with no cloud behind it; real staff arrive from
the cloud on first online login.

The React bundle is optional — if `app/ui/` is empty, FastAPI serves a plain
login page instead, so a clean checkout runs with nothing but Python installed.
To build the real one:

```bash
cd ui-src && npm install && npm run build
```

## Point it at a Supabase project

`docs/existing-project-setup.md` — including a project that already holds other
tables, which is what `0000_legacy_rehome` is for. Then
`docs/m2-test-guide.md` proves it works: sell with the network unplugged, plug
it back in, and count.

## Test it

```bash
pytest                                # 665 tests, ~15s
pytest tests/domain                   # the money rules alone, ~1s, no fixtures
HYPOTHESIS_PROFILE=ci pytest          # search the properties far harder
pytest -m "not slow"                  # skips the argon2 timing check
python scripts/run_rls_tests.py       # RLS against a bundled Postgres, no Docker
python scripts/check_sql_barcode_parity.py   # the SQL barcode reader still agrees with the domain one
```

The last one exists because `scripts/migrate_legacy_catalog.sql` contains a
plpgsql translation of `app/domain/barcode.py`. A fork of domain logic is a
place for two copies to drift, so both run over the 308-code corpus plus
twelve synthetic codes and every verdict must match. It starts its own
Postgres if you do not give it one.

The RLS suite skips unless `POS_TEST_PG_DSN` points at a Postgres. Either use
the script above (`pip install -e ".[pg]"` first) or bring your own:

```bash
docker run -d -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:17
POS_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres pytest tests/test_rls.py
```

## Check it

```bash
ruff check .                          # lint
python scripts/check_no_float.py      # no binary floating point in domain/
mypy app/domain                       # --strict, money rules only
lint-imports                          # api -> services -> {domain, data}
python scripts/gen_permission_seed.py --check
python scripts/gen_ts_types.py --check
```

---

## Layout

```
app/
  main.py            shell bootstrap: lock, port, token, uvicorn thread, health gate
  api/               FastAPI routers — thin, no logic
  services/          orchestration and transaction boundaries
  domain/            PURE. No I/O, no imports from any other app package.
  data/              SQLite connections, forward-only migrations, repositories
  security/          session token, Host/Origin guard, argon2id PINs, single-instance lock
  sync/              outbox drain, keyset pullers, envelopes, backoff
  ui/                built React bundle (generated, not committed)
ui-src/              React + TypeScript + Vite
supabase/
  migrations/        Postgres schema, RLS, permission seed, access token hook, sync_push
  functions/         Edge Functions (authenticate-pin)
  seed/              development data
  test/              auth-schema shim, for running RLS tests on plain Postgres
scripts/             seeding, argon2 tuning, code generation, catalogue migration
docs/                setting a project up, and proving M2 against it
```

**The dependency rule** is `api → services → {domain, data}`, and `domain`
imports nothing from the rest. `lint-imports` enforces it in CI. It is the
difference between a testable codebase and a ball of mud, and it erodes one
convenient import at a time if nothing is watching.

## Three things worth knowing before changing anything

**Money is integer minor units, everywhere.** Paise in `INTEGER` columns,
quantities as thousandths so 1.250 kg is `1250`. There is no `REAL` column in
either schema and a test asserts there never will be. `float` reaching the
database is the classic failure of a Python POS.

**Client-side permission checks are UX. RLS is security.** The matrix lives in
`app/domain/permissions.py` and is *generated* into Postgres and TypeScript, so
the three copies cannot disagree. Never test a role string; always test a
permission key.

**Transactions are append-only.** No `UPDATE` or `DELETE` policy exists on
sales, payments or stock movements. A correction is a compensating row, which
is also what makes the phase 5 sync merge-free.

---

## What phase 5 delivered

The terminal stops being an island.

| Track | Delivered |
|---|---|
| Outbox | Drain with backoff, idempotent replay, quarantine to `sync_failures`, and a manager-only retry once the cause is fixed |
| Pull | Per-entity keyset cursors, tombstone handling, the polling loop |
| Cloud | `sync_push` as one `security invoker` transaction, `on conflict do nothing` throughout, stock ledger deltas |
| UI | Sync badge and backlog count, **Send now**, and a refusal shown in red rather than buried in a tooltip |

**Exit criteria — the chaos test, all four passing as tests:** 200 sales
offline and exactly 200 arrive; a push that lands but is never acknowledged
replays without duplicating; the cashier never waits on the cloud; a payload
the server refuses quarantines and the queue keeps moving.

**Milestone M2 passed on real hardware**, against a live Supabase project, with
a day of selling on an unplugged network. That half cannot be automated and it
is where the seven defects below came from — every one in the seam between the
terminal and a hosted Supabase, none reachable by the suite as it stood.

| Symptom | Cause |
|---|---|
| `permission denied for schema auth` on `db push` | Supabase reserves `auth`; the claim helpers moved to a private `pos` schema |
| `Request failed (500)` on sign-in | A 404 from an undeployed Edge Function escaped unhandled instead of falling back offline |
| `employee_not_provisioned`, with healthy-looking rows | Seeded `auth.users` left GoTrue's token columns NULL, so the Admin API could not read a user SQL shows you |
| Backlog frozen, retried forever, failures list empty | `cashier_id = auth.uid()` made a mixed-cashier batch unpushable by anyone |
| `column "entity_id" is of type uuid` | A missing cast in the audit branch took the whole sale with it |
| Catalogue stopped at exactly 1000 products | Every imported row shared one `updated_at`; a watermark cannot page past that |
| `table stock_levels has no column named store_id` | The cloud keys it on the pair; the terminal, being one store, does not carry a store id |

Each has a test now. `docs/m2-test-guide.md` carries them as the shapes to
watch for on the next terminal.

## What phase 4 delivered

UPI, without a QR on the screen. The shops already have a printed standee and
there is no customer-facing display, so the till records that a payment
happened rather than asking for one.

| Track | Delivered |
|---|---|
| Payments | `UpiProvider`, attestation-only; attempt expiry and auto-cancel |
| Attestation | Manual, with an audit row, the UTR, and **the amount actually paid** |
| Review | `requires_review` queue and supervisor resolution, appended not edited |
| Tender | Split cash + UPI end to end, including where rounding lands when only part of the basket is cash |

**Exit criteria, all met:** all three tender combinations complete; a short UPI
payment leaves a balance settleable in cash without leaving the sale; an
expired attempt releases the cart; an attested sale is visibly distinct from a
verified one (`verified = 0`); a UPI-only sale carries no cash rounding.

Because the QR is static the payment carries no `tr`, so nothing in the bank
statement points back at a sale. Reconciliation is by amount and time, and the
customer types the amount themselves — so the attested amount is an input
defaulting to the outstanding balance, never an assumption. Architecture §13.3
before touching any of it.

## What phase 3 delivered

The vertical slice: a complete cash sale, end to end.

| Track | Delivered |
|---|---|
| Backend | Cart endpoints with server-side basket state, `PaymentProvider` protocol + `CashProvider`, the attempt state machine, split-tender balance loop, tender-dependent rounding, and `post_sale` as one SQLite transaction |
| Receipts | Receipt document model, text/HTML/PDF renderers from one structure, GST breakdown with CGST/SGST, visible rounding line, reprint by sale id |
| UI | Register screen, global barcode capture, search fallback, line list, tender dialog, WebSocket event client |

**Exit criteria, and where each is proven** (all in `tests/test_register_flow.py`):

| Criterion | Proof |
|---|---|
| 10 items by barcode and by search | `test_ten_items_by_barcode_and_by_search` |
| Void a line | `test_voiding_a_line` — line numbers stay stable |
| Take cash | `test_taking_cash_settles_the_sale` |
| Legible receipt, correct GST, visible rounding line | `test_the_receipt_is_legible_and_shows_its_working` |
| Sale lands in SQLite with balanced totals | `test_the_sale_lands_in_sqlite_with_balanced_totals` |
| Kill mid-sale, no partial sale | `test_a_crash_mid_post_leaves_no_partial_sale` |

Measured on the real catalogue: **3 ms per scan** against the 150 ms budget of
architecture §16, and FTS search at 0.2 ms over 31,467 products.

## What phase 2 delivered

The business rules, with no UI and no database behind them — the highest-value
two weeks in the plan, and the cheapest place to be wrong.

| Module | Rule it owns |
|---|---|
| `domain/money.py` | Integer paise. Exact arithmetic always available; anything that can lose a paisa is a named function that rounds once, HALF_UP. `allocate` splits a total so the parts always sum back to the whole. |
| `domain/tax.py` | GST extracted from inclusive prices, never added. Base by subtraction so `base + tax == gross` for every input. CGST/SGST halves that reconcile. |
| `domain/pricing.py` | Line gross, discount stacking in a defined order, tax on the **discounted** amount. A discount can never make a line negative. |
| `domain/cart.py` | Immutable basket, stable line numbers, the GST breakdown block. |
| `domain/tender.py` | Nearest-₹1 rounding, cash only, applied after tender choice. The adjustment is its own recorded figure. |
| `domain/promotions.py` | Percentage or fixed amount, scoped, date-windowed. Nothing that cannot be explained in one sentence at a queue. |
| `domain/barcode.py` | Check digits, GS1-128 AIs, and the `22…` weighed-item generator/parser round trip. |

**Exit criteria, and where each is proven:**

| Criterion | Proof |
|---|---|
| `pytest tests/domain` under 2s, no fixtures | 194 tests in ~1.0s; nothing under `tests/domain/` requests a fixture |
| A lint rule rejects `float` in `domain/` | `scripts/check_no_float.py`, its own CI step, with `tests/test_no_float.py` proving it bites |
| Parser handles a corpus of real stock barcodes | `tests/domain/test_barcode.py` runs the parser over 308 real codes from the pilot catalogue |
| Rounding never exceeds ₹0.50 and always reconciles | `tests/domain/test_tender.py::test_an_adjustment_never_exceeds_fifty_paise` and `::test_rounding_always_reconciles_against_the_unrounded_total` |

**On weighed items.** The target shops are too small for a label-printing
scale, so the POS generates the code rather than parsing someone else's. The
in-store EAN-13 range is partitioned deliberately: `21…` stays product
identity (architecture §10.2, and 38.6% of the pilot catalogue will need it),
`22…` carries a weighing. Because quantities are already integer thousandths
and thousandths *are* grams for anything sold by the kilo, the weight is
embedded verbatim — no scaling, no unit conversion, no float anywhere on the
path. Weight is embedded rather than price, because price changes at every
repricing and would pin the GST base to a stale number.

## What phase 1 delivered

| Track | Delivered |
|---|---|
| Backend | Postgres schema, RLS policies, generated roles/permissions seed, custom access token hook, margin report view; SQLite forward-only migration runner; repository base with a single write connection behind a lock |
| Shell | `main.py` bootstrap, uvicorn in a daemon thread, splash, `/health` gate, single-instance lock, rotating logs |
| Security | Per-launch session token, `Host` and `Origin` middleware, ephemeral port, no CORS headers at all |
| Auth | `authenticate-pin` Edge Function, JWT claims hook, keyring refresh-token storage, argon2id PINs tuned to ~87 ms, 14-day offline snapshot with hard TTL |
| Contract | Pydantic models frozen and generated into `ui-src/src/core/api/contract.ts` |

**Exit criteria, and where each is proven:**

| Criterion | Proof |
|---|---|
| App launches to a login screen | `tests/test_shell.py` — real uvicorn, real health gate, real HTTP |
| Seeded cashier logs in with code + PIN | `tests/test_auth_api.py::test_seeded_cashier_logs_in` |
| `/health` reports schema version | `tests/test_auth_api.py::test_health_reports_schema_version` |
| RLS denies a cashier `report.margin` columns | `tests/test_rls.py` — 27 tests against real Postgres |

## What the real catalogue looks like

`scripts/import_barcode_corpus.py` profiles the legacy billing project and
samples `tests/fixtures/barcode_corpus.json` — 308 codes chosen to cover every
parser branch rather than every row, deterministically, so a re-run produces a
stable diff instead of churn.

Findings from 32,165 products, which shape phases 2 and 6:

| Finding | Consequence |
|---|---|
| **38.6% (12,431) have no barcode at all** | Plan §6 step 5 is the biggest catalogue-migration task by far. Without label printing these products are findable only by name, so product search and sane naming are load-bearing, not nice-to-have. |
| 0.3% (106) have a bad check digit | Small enough to fix by hand from an exceptions report. |
| 0.5% (166) are not a GTIN length | Supplier and internal codes of 1–19 digits. The parser must not assume 8/12/13/14. |
| 4 duplicate barcodes (`2`, `200`, `450`, `900`) | Junk short codes on 28 products. `ux_barcode` will reject these on import — they need resolving before go-live. |
| Only 2 verified ITF-14 case codes | Pack-size receiving has almost no real test data. |
| **Zero scale labels** | See below. |

**On the missing scale labels: that is the correct shape for this market.**
The target shops are too small to own a label-printing scale, so nothing
external ever produces a scale label. A weighed item is hand-weighed, its
quantity keyed into the POS, and the POS generates the code itself. The 20-29
in-store range is therefore ours to author, and what would have been the
riskiest parser branch — reverse-engineering an unknown scale's digit layout —
becomes a round-trip against a format we define. Inbound scale-label parsing
moves to the §8 scope fence alongside the scale hardware.

The profiler still refuses to interpret a code that fails its own check digit.
Reading an embedded price off a corrupt number is how a till charges the wrong
amount with complete confidence.

## Still open

- **Receipt delivery** (execution plan §2) — on-screen only, PDF, or share by
  link. Still undecided, and it determines the renderer and whether customer
  contact details are captured. Phase 3 needs an answer.
- **Code-signing certificate** — EV issuance takes 1–6 weeks and is on the
  critical path for phase 9. Start the application now, not in week 20.
- **Python floor** — the plan says 3.14+ for stdlib `uuid7`. `app/domain/ids.py`
  generates RFC 9562 v7 identifiers itself on older interpreters, so the code
  runs on 3.10+; CI covers 3.12 and 3.14.
- **argon2 parameters** are tuned to this development machine (~87 ms), and
  the same parameters run as WebAssembly inside `authenticate-pin`, where a
  real sign-in measured **3.7–5.3 seconds** — near Supabase's per-invocation
  CPU cap. Re-run `python scripts/tune_argon2.py` on the actual till hardware
  before the pilot, and move the Edge Function in step: they must match
  exactly or a hash made by one will not verify in the other, and every stored
  hash moves with them. **This is the one to do before a pilot.**
- **Per-terminal identity.** `0009` dropped `cashier_id = auth.uid()` from the
  sales insert policy because a terminal pushes a day's backlog under whichever
  session is signed in, so one cashier's sale was unpushable by any other. The
  actor id is now provenance rather than an authorization claim. The proper
  repair is a terminal that authenticates as itself, which is phase 9; the
  trade is written into `tests/test_rls.py` so it is not quietly forgotten.
- **Opening stock.** `stock_levels` starts empty. The catalogue migration
  deliberately does not import the legacy `quantity` column: a count copied out
  of a retired system is a number nobody has verified standing in front of a
  shelf. A stock-take is phase 6.
- **Repeat scans merge, weighed items do not.** Four scans of one book make one
  line at quantity four. Two weighings do not merge, because folding them keeps
  only one of the two `22…` codes and the line would claim a weight no scale
  produced. Worth confirming against a real counter.

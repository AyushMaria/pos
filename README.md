# Retail POS

A single-terminal till for Indian retail: offline-first, GST-inclusive pricing,
cash and UPI. pywebview shell · React/TypeScript UI · FastAPI local service ·
SQLite on the terminal · Supabase in the cloud.

See [`retail-pos-architecture-python.md`](retail-pos-architecture-python.md)
for what is being built and
[`retail-pos-execution-plan.md`](retail-pos-execution-plan.md) for the order it
is being built in.

**Status: phases 1 (Foundation), 2 (Domain core) and 3 (Register MVP) complete.** The till opens, authenticates a
cashier against a local database, and Postgres refuses a cashier the margin
columns. The business rules — money, GST, discounts, the cart, cash rounding
and barcodes — are complete and property-tested. A cashier can sign in, ring
up a basket by barcode or by name against 31,467 real products, void a line,
take cash and get a receipt with a correct GST breakdown. UPI arrives in
phase 4 and the sync engine in phase 5, so the till is still an island.

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

Sign in with one of the seeded accounts:

| Code | PIN  | Role       |
|------|------|------------|
| C001 | 4913 | cashier    |
| S001 | 7261 | supervisor |
| M001 | 5820 | manager    |

These are development credentials, published in this repository. They exist so
phase 1 can be demonstrated before the sync engine does; real staff arrive from
the cloud on first online login.

The React bundle is optional — if `app/ui/` is empty, FastAPI serves a plain
login page instead, so a clean checkout runs with nothing but Python installed.
To build the real one:

```bash
cd ui-src && npm install && npm run build
```

## Test it

```bash
pytest                                # 513 tests, ~10s
pytest tests/domain                   # the money rules alone, ~1s, no fixtures
HYPOTHESIS_PROFILE=ci pytest          # search the properties far harder
pytest -m "not slow"                  # skips the argon2 timing check
python scripts/run_rls_tests.py       # RLS against a bundled Postgres, no Docker
```

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
  ui/                built React bundle (generated, not committed)
ui-src/              React + TypeScript + Vite
supabase/
  migrations/        Postgres schema, RLS, permission seed, access token hook
  functions/         Edge Functions (authenticate-pin)
  seed/              development data
  test/              auth-schema shim, for running RLS tests on plain Postgres
scripts/             seeding, argon2 tuning, code generation
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
- **argon2 parameters** are tuned to this development machine
  (~87 ms). Re-run `python scripts/tune_argon2.py` on the actual till hardware
  before the pilot, and keep the Edge Function's parameters in step.

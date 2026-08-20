# Desktop Retail POS — Architecture (Python + SQLite + React)

**Stack:** pywebview shell · React/TypeScript UI · FastAPI local service · SQLite (local) · Supabase (cloud)

**Supersedes** the Tauri/Rust variant. The distributed architecture — offline-first, append-only transactions, ledger-based stock, RLS as the trust boundary — is unchanged. What changes is process topology, the language boundary, and the operational concerns that come with shipping a Python runtime.

---

## 0. Implementation status

This document describes the whole system. Roughly a third of it is now running code, and the two have been reconciled here — where the build diverged from the design, the built version is what this document now says, with the reason given inline.

**As of the end of phase 3** (execution plan §3):

| § | Area | State |
|---|---|---|
| 2 | Process topology | Built. pywebview on the main thread, uvicorn in a daemon thread, health gate, single-instance lock. No peripheral worker threads and no sync tasks yet — nothing needs the former, and the latter is phase 5. |
| 3 | Layers | Built, and the dependency rule is enforced in CI by `import-linter`. |
| 4 | Local API | Built for auth, catalog, register and reports. Inventory, shifts, admin, park/resume, override and print endpoints are unwritten. |
| 5 | Local API security | Built in full, one test per defence. |
| 6 | Money and quantities | Built. `float` is banned from `domain/` by a CI check that walks the AST. |
| 7 | SQLite schema | Built through migration `002`. |
| 8 | Concurrency | Built. One write connection behind a lock, thread-local readers, `post_sale` as a single transaction. |
| 9 | Sync engine | **Not built.** Outbox rows are already written by every sale, so nothing is being lost; nothing drains them yet. Phase 5. |
| 10 | Barcode subsystem | Built, plus a weighed-item *generator* (§10.3) that the original design did not anticipate. |
| 11 | RBAC | The matrix is built and generated from one source into Postgres, Python and TypeScript. Manager override is phase 7. |
| 12 | Peripherals | Scanner only — and it needs no code. |
| 13 | Payments | Lifecycle, state machine and split-tender balance loop built; `CashProvider` is the only registered provider. UPI is phase 4. |
| 14 | Packaging | Not started. The code-signing certificate is the long-lead item and is still unstarted. |
| 15 | Testing | 513 Python tests (+27 skipped without the Postgres extra), 27 RLS tests against a real Postgres, 17 UI tests. |

```
pytest -q                        # 513 passed, 27 skipped
python scripts/run_rls_tests.py  # 27 passed, against bundled Postgres
npm test          (in ui-src/)   # 17 passed
```

Two things known to be wrong in the **data** rather than the code, both to be fixed during the catalogue migration and neither of them a pilot-ready state:

* **GST rates are inferred** from `main_group`, with 19,614 products defaulted to 18%. Nobody has reviewed them.
* **38% of the catalogue has no barcode at all.** Every product must carry one — see §10.4 — so the import has to code them, and the till has to refuse to create a product without one.

---

## 1. Design principles

1. **Offline-first.** SQLite is the runtime store. Supabase is a sync target, never a request-path dependency.
2. **The domain lives in Python.** Cart, pricing, discounts and tax use `Decimal` and are pure functions with no I/O. React renders state and dispatches commands; it does no money math. Losing this discipline forfeits the main reason for choosing Python.
3. **Client-side permission checks are UX. Postgres RLS is security.**
4. **Transactions are append-only** — sales, payments and stock movements are immutable events, which eliminates sync merge conflicts.
5. **Stock is a ledger of deltas, never a counter.**
6. **Localhost is not a private channel.** The local API is authenticated (§5).
7. **Never bundle the `service_role` key.** A PyInstaller bundle decompiles trivially.

---

## 2. Process topology

```
┌──────────────────────────────────────────────────────────────┐
│ pos.exe  (single OS process, single-instance locked)          │
│                                                              │
│  MAIN THREAD ── pywebview                                    │
│    OS webview (WebView2 / WebKitGTK / WKWebView)             │
│    kiosk window, loads http://127.0.0.1:{port}/?t={token}    │
│         │  HTTP commands  ·  WebSocket events                │
│  ───────┼──────────────────────────────────────────────────  │
│  SERVER THREAD ── uvicorn + FastAPI                          │
│    routers → application services → domain → repositories    │
│                                                              │
│  ASYNCIO TASKS (in server loop)                              │
│    sync_push · sync_pull · realtime_or_poll · heartbeat       │
│                                                              │
│  DEDICATED WORKER THREADS (blocking I/O)                     │
│    printer queue · scale reader · display writer             │
│                                                              │
│  SQLite (WAL)  ·  one write connection + lock, N readers      │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTPS (anon key + user JWT)
┌───────────────────────────▼──────────────────────────────────┐
│ SUPABASE — Postgres + RLS · Auth (custom token hook)          │
│ RPC: sync_push, close_shift · Edge Functions: authorize-      │
│ override, register-device, close-day · Storage: images, Z-rpts │
└──────────────────────────────────────────────────────────────┘
```

*Built as drawn, with two absences. There are no peripheral worker threads — nothing yet needs one, since the only device attached is a scanner and a scanner is a keyboard (§12). And there are no sync tasks in the loop; the outbox rows they will drain are already accumulating.*

**pywebview must own the main thread.** uvicorn therefore runs in a daemon thread started before `webview.start()`. Getting this backwards produces a window that never paints on macOS.

```python
def main() -> None:
    acquire_single_instance_lock()          # exits if a till is already open
    port  = pick_free_port()
    token = secrets.token_urlsafe(32)
    app   = build_app(token=token)

    threading.Thread(
        target=uvicorn.run,
        args=(app,),
        kwargs={"host": "127.0.0.1", "port": port, "log_config": LOG_CFG},
        daemon=True,
    ).start()

    wait_for_health(port, token, timeout=20)  # splash until ready
    webview.create_window(
        "Register", f"http://127.0.0.1:{port}/?t={token}",
        fullscreen=True, confirm_close=True,
    )
    webview.start()                           # blocks on main thread
```

---

## 3. Layers and module structure

```
pos/
├─ app/
│  ├─ main.py                  # shell bootstrap (above)
│  ├─ config.py
│  ├─ api/                     # FastAPI routers — thin, no logic
│  │  ├─ auth.py  catalog.py  register.py  reports.py  health.py
│  │  ├─ deps.py  schemas.py  server.py  dev_ui.py
│  │  ├─ events.py             # WebSocket hub
│  │  └─ (phase 6+) inventory.py  shifts.py  admin.py
│  ├─ services/                # orchestration + transaction boundaries
│  │  ├─ sale_service.py       # post: one SQLite txn, one outbox write
│  │  ├─ cart_service.py  payment_providers.py  receipt_render.py
│  │  └─ auth_service.py  supabase_auth.py
│  ├─ domain/                  # PURE — no I/O, no imports from app.data
│  │  ├─ money.py              # Money(paise); refuses float at construction
│  │  ├─ tax.py  pricing.py  promotions.py  cart.py
│  │  ├─ tender.py             # cash rounding, at the tender boundary only
│  │  ├─ payments.py           # attempt state machine, balance
│  │  ├─ receipt.py            # document model, renderer-independent
│  │  ├─ barcode.py            # parse() → ScanResult; weighed_barcode()
│  │  ├─ permissions.py        # the matrix — generated into SQL and TS
│  │  └─ identity.py  ids.py   # session/snapshot TTLs; uuid7
│  ├─ data/
│  │  ├─ db.py                 # connection mgmt, WAL, write lock
│  │  ├─ migrations/           # 001_init.sql, 002_… forward-only
│  │  └─ repositories/         # base · users · terminal · catalog · sales
│  ├─ security/
│  │  ├─ local_auth.py         # session token, Host/Origin checks, port
│  │  ├─ pins.py               # argon2id
│  │  └─ keychain.py  single_instance.py
│  ├─ sync/                    # phase 5 — outbox drain, pullers, cursors
│  └─ ui/                      # React build output, served static
├─ ui-src/                     # React + TS + Vite (plain CSS; no Tailwind)
│  ├─ src/features/{register,auth}/
│  └─ src/core/{api,rbac,barcode-capture}/
├─ scripts/                    # seeding, code generation, CI gates
├─ tests/                      # tests/domain/ is fixture-free by design
├─ supabase/{migrations,functions,seed,test}/
└─ packaging/{pos.spec,installer.iss,version.json}   # phase 9
```

`app/peripherals/` is absent rather than empty: with printing, drawers, scales and displays all deferred, and the scanner needing no driver, there is nothing for it to hold.

**Dependency rule:** `api → services → domain` and `services → data`. `domain` imports nothing from the other packages. Enforce it in CI with `import-linter`; it is the difference between a testable codebase and a ball of mud.

---

## 4. Local API surface

Commands are HTTP; anything the service initiates goes over WebSocket.

A tick marks what exists today; the rest is the intended surface.

| ✓ | Method | Path | Notes |
|:-:|---|---|---|
| ✅ | `GET` | `/health` | Startup gate; unauthenticated, returns version + migration state |
| ✅ | `POST` | `/auth/login` | Employee code + PIN (online or cached) |
| ✅ | `GET` | `/auth/session` | Who is logged in, and with which permissions |
| ✅ | `POST` | `/auth/logout` | |
| | `POST` | `/auth/lock` `/auth/unlock` | Screen lock between customers |
| ✅ | `GET` | `/catalog/lookup?barcode=` | Parsed scan → resolved line candidate |
| ✅ | `GET` | `/catalog/search?q=` | FTS5 name/SKU search |
| ✅ | `POST` | `/register/carts` | Open cart; server-side cart state |
| ✅ | `GET` | `/register/carts/{id}` | Re-read a cart after a webview reload |
| ✅ | `DELETE` | `/register/carts/{id}` | Abandon cart |
| ✅ | `POST` | `/register/carts/{id}/lines` | Add a scanned or searched line |
| ✅ | `PATCH` | `/register/carts/{id}/lines/{n}` | Qty, discount (permission-checked) |
| ✅ | `DELETE` | `/register/carts/{id}/lines/{n}` | Void a line before posting |
| ✅ | `GET` | `/register/carts/{id}/tender-quote` | What this method would collect — **including rounding, before the money is taken** (§13.4) |
| ✅ | `POST` | `/register/carts/{id}/payments` | Begin an attempt `{method, amount}` → QR payload once UPI exists |
| ✅ | `POST` | `/register/carts/{id}/post` | Commit the sale once the balance is settled |
| ✅ | `POST` | `/register/sales/{id}/receipt.pdf` | Render on demand; the file is written locally, not streamed |
| ✅ | `GET` | `/reports/margin` `/reports/size` | Thin, and mainly there to prove RLS denies a cashier the margin columns |
| | `POST` | `/register/carts/{id}/park` `/resume` | Suspend transaction |
| | `GET` | `/register/payments/{attempt_id}` | Poll attempt state |
| | `POST` | `/register/payments/{attempt_id}/confirm` | Manual attestation (`payment.attest`) |
| | `POST` | `/register/payments/{attempt_id}/cancel` | Abandon attempt; cart stays open |
| | `POST` | `/register/sales/{id}/resolve-review` | Clear a `requires_review` sale (`sale.review.resolve`) |
| | `POST` | `/register/sales/{id}/void` `/refund` | Requires override token |
| | `POST` | `/overrides/authorize` | Supervisor PIN → short-lived grant |
| | `POST` | `/inventory/receipts` `/counts` `/adjustments` | Stock movements |
| | `POST` | `/print/receipt/{sale_id}` `/print/labels` | Reprint, label sheets |
| | `POST` | `/shifts/open` `/shifts/close` | Session + cash reconciliation |
| | `GET` | `/sync/status` | Backlog depth, last push/pull, errors |
| ✅ | `WS` | `/events` | Today: `connectivity`. Reserved: `sync.status`, `catalog.updated`, `auth.revoked` |

Two shapes came out differently from the sketch above, both for the same reason — the cashier needs to know a number *before* committing to it. `tender-quote` is a separate read so the rounding adjustment can be shown while the customer is still holding their money, and posting is its own call rather than a side effect of the final payment, so a settled balance and a committed sale stay distinguishable.

**Every response carries pre-formatted money.** `MoneyOut` ships `{paise, text}` together, so React displays `text` and never divides by 100. This is the same rule as “React never sums a total”, applied one step further out.

**The cart lives server-side.** Adding a line returns fully recalculated totals from Python. React never sums a total — that is how price/tax bugs are avoided, and it means a webview crash mid-sale loses nothing.

---

## 5. Local API security

Binding to a loopback port does not make it private: any process, and any webpage the cashier opens, can reach it.

- **Bind `127.0.0.1` explicitly** — never `0.0.0.0`.
- **Session token** generated per launch, passed to the webview in the URL, held in JS memory (never `localStorage`), and required as `Authorization: Bearer` on every request. **The WebSocket is the exception**: a browser cannot set headers on an upgrade, so the token arrives as the `?t=` query parameter and is checked there. It is compared with `secrets.compare_digest`, and the socket closes with `1008` on failure so the UI can tell “wrong token” from “service went away”.
- **`Host` header allow-list** middleware (`127.0.0.1` / `localhost` only) to defeat DNS-rebinding.
- **`Origin` check** on the WebSocket upgrade.
- **No CORS headers at all.** Same-origin by construction, since React is served by FastAPI.
- **Ephemeral port** rather than a fixed one, so nothing can pre-target it.
- **Local role state is advisory.** The service enforces permissions from the cached snapshot for UX and audit; Postgres RLS re-enforces on sync and is authoritative.

---

## 6. Money and quantities

The most common failure mode in a Python POS is float money surviving into the database.

- **Storage:** money as `INTEGER` minor units (paise/cents). Quantities as `INTEGER` thousandths, so 1.250 kg is `1250`. SQLite has no decimal type; never use `REAL` for either.
- **Domain:** a `Money` value object wrapping minor units, with explicit `Decimal` conversion at the tax boundary and `ROUND_HALF_UP` quantisation.
- **Line totals are computed then rounded once**, at the line, before summing — matching how tax authorities and every audit expect it. Rounding at the invoice total instead produces off-by-one-paise disputes.
- **Prices snapshot onto `sale_lines` at scan time** so a mid-transaction price sync cannot change what the customer was quoted.
- Property-test the pricing engine with Hypothesis; this is the highest-value test suite in the project.

**As built.** `Money` wraps an `int` of paise and rejects `float` — and `bool` — at construction, so a float cannot enter a money path even by accident. Splitting money (CGST/SGST, and any allocation across lines) goes through `allocate()`, which distributes by largest remainder and is property-tested to reconcile exactly against the input. Tax is *extracted* from a GST-inclusive price rather than added: `gross × rate / (10000 + rate)`, with the taxable base then computed **by subtraction**, so base + tax equals the gross to the paise by construction rather than by luck. `tests/domain/` runs fixture-free in under a second.

---

## 7. Local SQLite schema

Mirrors the Postgres reference tables, plus three tables that exist only on the terminal.

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;      -- FULL for the sale-posting connection
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

-- ── Cached reference data (pull-only, server authoritative) ──
products(id TEXT PK, sku, name, short_name, category_id, uom,
         is_weighed INT, track_stock INT, tax_code, is_active INT,
         updated_at TEXT, deleted_at TEXT);
product_barcodes(id TEXT PK, product_id TEXT, barcode TEXT, symbology,
                 pack_size INT DEFAULT 1, is_primary INT,
                 updated_at TEXT, deleted_at TEXT);
product_prices(id TEXT PK, product_id, store_id, price INT, cost INT,
               valid_from TEXT, valid_to TEXT);
promotions(id TEXT PK, type, scope_json, value INT, starts_at, ends_at, priority INT);
tax_codes(code TEXT PK, name, rate_bp INT, is_inclusive INT);
stock_levels(product_id TEXT PK, on_hand INT, reorder_point INT, updated_at TEXT);

-- ── Local identity cache (offline auth) ──
cached_users(user_id TEXT PK, employee_code TEXT, full_name, pin_hash TEXT,
             status TEXT, snapshot_signed_at TEXT, snapshot_expires_at TEXT);
cached_permissions(user_id TEXT, store_id TEXT, permission_key TEXT,
                   PRIMARY KEY (user_id, store_id, permission_key));

-- ── Transactional (push-only, immutable once posted) ──
register_sessions(id TEXT PK, terminal_id, user_id, opened_at,
                  opening_float INT, closed_at, counted_cash INT,
                  expected_cash INT, variance INT, status);
sales(id TEXT PK, store_id, terminal_id, session_id, receipt_no TEXT,
      cashier_id, type, status, subtotal INT, discount_total INT,
      tax_total INT, rounding_adjustment INT DEFAULT 0, grand_total INT,
      original_sale_id, client_created_at TEXT, posted INT DEFAULT 0);
      -- status: parked|completed|voided|requires_review
sale_lines(id TEXT PK, sale_id, line_no INT, product_id, barcode_scanned,
           description, qty_milli INT, unit_price INT, discount_amount INT,
           tax_code TEXT, tax_rate_bp INT,          -- added in 002
           tax_amount INT, line_total INT, overridden_by, override_reason);
payment_attempts(id TEXT PK, sale_id, method, amount INT, state,
                 txn_ref TEXT, qr_payload TEXT, started_at TEXT,
                 expires_at TEXT, resolved_at TEXT, failure_reason);
payments(id TEXT PK, sale_id, attempt_id, method, amount INT, status,
         tender_ref, utr TEXT, confirmation_method, confirmed_by,
         verified INT DEFAULT 0);
      -- method: cash|upi   status: approved|voided
      -- confirmation_method: immediate|manual_attestation|psp_webhook
stock_ledger(id TEXT PK, store_id, product_id, delta_milli INT, reason,
             ref_type, ref_id, occurred_at TEXT, terminal_id, user_id);
cash_movements(id TEXT PK, session_id, direction, amount INT, reason,
               actor_id, approved_by);
audit_log(id TEXT PK, actor_id, approver_id, action, entity, entity_id,
          before_json, after_json, occurred_at TEXT);
unknown_scans(id TEXT PK, barcode, scanned_at, terminal_id, resolved INT);

-- ── Terminal-only plumbing (never synced upward) ──
outbox(id INTEGER PK AUTOINCREMENT, entity TEXT, entity_id TEXT, op TEXT,
       payload_json TEXT, client_seq INTEGER, created_at TEXT,
       attempts INT DEFAULT 0, last_error TEXT, synced_at TEXT);
sync_state(entity TEXT PK, cursor TEXT, last_pulled_at TEXT);
sync_failures(id INTEGER PK, outbox_id INT, payload_json TEXT,
              error TEXT, failed_at TEXT, acknowledged INT DEFAULT 0);

CREATE UNIQUE INDEX ux_barcode ON product_barcodes(barcode)
  WHERE deleted_at IS NULL;
CREATE INDEX ix_outbox_pending ON outbox(synced_at, id);
CREATE VIRTUAL TABLE products_fts USING fts5(
  name, sku, content='products', content_rowid='rowid');
```

`tax_code` and `tax_rate_bp` were added by migration `002` after the receipt renderer was written. A GST rate can change; a reprint of last month's invoice must still show last month's rate, so the rate is snapshotted onto the line exactly as the price is. Deriving it from `tax_codes` at print time would silently rewrite history.

**IDs are UUID v7** (time-sortable, collision-free across terminals). `domain/ids.py` implements RFC 9562 directly and defers to `uuid.uuid7()` when the interpreter has it — 3.14 was not available when this was built, and a twelve-line implementation was preferable to a dependency that exists only to be removed later. **Receipt numbers** are `{STORE}-{TERMINAL}-{SEQ}` from a local monotonic counter — unique without coordination, gaps auditable.

**Migrations** are forward-only numbered SQL files applied inside a transaction at startup, with `user_version` as the marker. Alembic is overkill for an embedded, single-writer database.

---

## 8. Concurrency and SQLite discipline

SQLite in WAL mode allows many readers and exactly one writer. Make that explicit rather than discovering it as `database is locked` at a busy till.

- **One dedicated write connection** guarded by a `threading.Lock`; a read-only connection pool for queries.
- **All writes go through repository functions** that take the lock; no ad-hoc connections anywhere.
- **`sale_service.post_sale()` is one transaction**: sale + lines + payments + stock_ledger deltas + audit row + outbox rows commit together or not at all. A sale that reaches the printer but not the outbox is a lost sale; a sale that reaches the database but not the printer is only a reprint.
- **Blocking peripheral I/O never runs in the event loop.** Printer, scale and display each own a thread fed by a `queue.Queue`; endpoints enqueue and return.
- **Sync tasks are asyncio + `httpx.AsyncClient`**, but their SQLite writes are dispatched to the write lock via `run_in_threadpool`.
- Print **after** commit, and treat printing as retryable — the receipt is a side effect, not part of the transaction.

---

## 9. Sync engine

### 9.1 Data classification

| Class | Tables | Direction | Conflict rule |
|---|---|---|---|
| Reference | products, barcodes, prices, promos, tax, users, roles | Pull | Server wins (LWW on `updated_at`) |
| Transactional | sales, lines, payments, stock_ledger, cash, audit | Push | None — append-only |
| Aggregate | stock_levels, daily summaries | Derived server-side | Never pushed |

No row is written by both sides, so there is effectively nothing to merge.

### 9.2 Push

Outbox drained FIFO in batches of ~200 into one RPC:

```python
async def drain(self) -> None:
    while batch := self.repo.next_pending(limit=200):
        try:
            await self.client.post("/rest/v1/rpc/sync_push",
                                   json={"items": [b.payload for b in batch]})
            self.repo.mark_synced([b.id for b in batch])
            self.backoff.reset()
        except PermanentError as e:        # RLS denial, validation
            self.repo.quarantine(batch, e) # → sync_failures, alert manager
        except TransientError:
            await self.backoff.sleep()     # 1s→2s→4s… cap 5 min
            return
```

- **Idempotency:** client-generated UUID v7 PKs + `ON CONFLICT (id) DO NOTHING` server-side. A dropped ACK never double-posts a sale.
- **Ordering:** `client_seq` per terminal preserves causality (sale before its payment).
- Permanent failures quarantine rather than block the queue, and surface in the UI.

### 9.3 Pull

Per-entity watermark in `sync_state`:

```
GET /rest/v1/products?updated_at=gt.{cursor}&order=updated_at.asc&limit=1000
```

Soft deletes (`deleted_at`) propagate as tombstones. `supabase-py`'s Realtime support is thinner than the JS client's, so **poll every 60–120 s while idle** and treat the watermark pull as the sole reliable path; add Realtime later as an optimisation, never as the mechanism. Price changes propagating in ~1 minute instead of ~1 second is an acceptable trade for a till.

### 9.4 Stock

Sale → `delta = −qty`. Receipt → `delta = +qty × pack_size`. Count → `delta = counted − expected`. `stock_levels` is maintained server-side by trigger; the cached local level is labelled indicative while offline.

**Reporting uses `server_received_at`**, never terminal time — clocks drift and cashiers change them.

---

## 10. Barcode subsystem

### 10.1 Capture — JavaScript side

USB HID wedge scanners type into the focused window, which is the webview. The capture hook therefore lives in React. **A scanner is in use on the target counter** (a Helett HT20 Pro), so this path is load-bearing rather than speculative, and it is covered by 17 Vitest cases.

- Buffer keystrokes; a burst with inter-character gaps under ~30 ms terminated by `Enter` is a scan. A human types at ~120 ms and never trips it.
- A gap mid-burst **discards the partial buffer** rather than gluing the next keystrokes onto it. A refused scan costs a rescan; a silently merged one bills the wrong product.
- Global listener, so scanning works with a dialog open.
- Posts the raw string to `GET /catalog/lookup`; **all parsing happens in Python** so the rules are unit-tested and shared with receiving.
- Later option: claim the device exclusively in Python (`evdev` on Linux, raw HID on Windows) to stop stray scans leaking into text inputs. Platform-specific — defer.

**The register's own barcode field is excluded from the hook, not served by it.** That field is an ordinary input: the scanner types into it and `Enter` submits the form, which routes an all-digit string to a lookup and anything else to a name search. If the global hook also fired there, every scan would travel two paths at once and the winner would depend on the order `preventDefault` happened to run in — and a cashier typing a search term quickly enough could have it swallowed as a barcode. The hook therefore covers exactly what the field cannot: a scan while focus is somewhere else, such as an open tender dialog. That is the entire reason it is global, and inverting this rule is the easiest way to break scanning.

**One device setting is worth checking on every new terminal**: the scanner's terminator suffix. A unit configured to send `Tab` instead of `Enter` will type the digits in and never complete a scan, which looks like a software fault and is not one.

### 10.2 Parsing — Python side

```python
def parse(raw: str, rules: StoreRules) -> ScanResult:
    """raw → product_id, qty, price override, batch, expiry."""
```

| Symbology | Use | Handling |
|---|---|---|
| EAN-13 / UPC-A | Retail units | Check-digit validation, direct lookup |
| Code128 / Code39 | Internal SKUs | Direct lookup |
| ITF-14 | Cases/outers | Resolves to barcode row with `pack_size > 1` |
| GS1-128 / DataBar | Batch, expiry, weight | AI parsing: `01` GTIN, `10` batch, `17` expiry, `310n` net weight |
| EAN-13 prefix 20–29 | In-store codes — including scale-printed labels | Rule engine extracts embedded price or weight |

One product has **many** barcodes — unit, 6-pack, case, legacy supplier codes. `pack_size` drives the multiplier, so scanning a case receives 12 units.

**A code that fails its check digit is never interpreted.** Not as a lookup, not as a partial match, not as a weighed code. A mis-scan that resolves to *something* is worse than one that resolves to nothing, because only the second gets reported.

**Unknown barcode:** never block the queue. With `product.create`, quick-create inline; without it, log to `unknown_scans` for a manager.

### 10.3 Generating weighed codes

The design above assumed weighed goods arrive pre-labelled by a shop scale, in the `20–29` range. The target shops are too small to own one — profiling 32,165 real catalogue rows turned up **no scale labels at all** — and the shopkeeper weighs by hand.

So the POS generates the code instead of only reading it. `weighed_barcode(item_code, qty_milli)` returns:

```
22 | 5-digit item code | 5-digit grams | check digit
```

with `21…` kept for internal codes on products that have no manufacturer barcode. Both are in the reserved in-store range, both carry a valid EAN-13 check digit, and both parse through the same `parse()` as any other code — a generated code and a printed one are indistinguishable to everything downstream.

Grams fall out for free: quantities are already stored as integer thousandths of the selling unit, and for goods sold by the kilo a thousandth *is* a gram. The weight embeds verbatim, with no conversion and therefore no rounding.

**Built in the domain, not yet exposed.** There is no weigh-and-generate screen and nothing prints these — both are deferred with the rest of the peripheral work (execution plan §8). The rule is settled and tested, which is what matters: `parse()` already reads a `22…` code back into a product and a quantity, so the counter flow can be added later without revisiting the format.

**Labels, when printing arrives:** `python-barcode` + `reportlab` to Avery-style PDF sheets, or ZPL for Zebra printers.

### 10.4 Every product carries a code

A product with no barcode cannot be created and cannot be sold. Decided after M1, and it is a constraint on the whole system rather than a validation rule on one form:

* **Catalogue import** assigns an internal `21…` code to anything the manufacturer did not code, and finishes with nothing uncoded.
* **Quick-create** at the counter mints a code as part of creating the product, rather than offering a code-less row to be tidied up later. There is no "add without barcode" path to leave one behind.
* **Weighed goods** get a generated `22…` code (§10.3), which is the same rule applied to items whose quantity is not known until the counter.
* **Name search stays**, as a fallback for a label that will not read — not as a daily path. It is why product names still have to be sane, but it is no longer what the design is optimised around.

The reason to state it here rather than in the catalogue screens is that it is what makes the rest of the subsystem coherent: `unknown_scans` becomes a genuine exception queue instead of a parking space, stock movements always have something to reference, and the register has one way in rather than two.

**What it does not settle.** A code makes a product *sellable*; a printed label makes it *scannable*, and printing is deferred. Anything sold loose or by weight therefore still needs a way to be selected at the counter — a scannable shelf label, a quick-key grid, or the search — and that choice is what decides how much of §10.3 has to surface in the UI. Phase 6 work, and open.

---

## 11. RBAC

### 11.1 Permissions, not role strings

Never `if user.role == "manager"`. Always `require("sale.void")`, backed by `cached_permissions` locally and RLS remotely.

| Permission | Cashier | Supervisor | Inventory | Manager | Admin |
|---|:--:|:--:|:--:|:--:|:--:|
| `sale.create` | ✅ | ✅ | — | ✅ | ✅ |
| `sale.discount.line` (≤10%) | — | ✅ | — | ✅ | ✅ |
| `sale.discount.unlimited` | — | — | — | ✅ | ✅ |
| `sale.void` / `sale.refund` | — | ✅ | — | ✅ | ✅ |
| `price.override` | — | ✅ | — | ✅ | ✅ |
| `product.read` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `product.create` / `edit` | — | — | ✅ | ✅ | ✅ |
| `stock.receive` / `stock.count` | — | — | ✅ | ✅ | ✅ |
| `stock.adjust` | — | — | — | ✅ | ✅ |
| `payment.attest` (UPI received) | ✅ | ✅ | — | ✅ | ✅ |
| `sale.review.resolve` | — | ✅ | — | ✅ | ✅ |
| `cash.payout` | — | ✅ | — | ✅ | ✅ |
| `shift.close` | — | ✅ | — | ✅ | ✅ |
| `report.sales.store` | — | ✅ | — | ✅ | ✅ |
| `report.margin` | — | — | — | ✅ | ✅ |
| `user.manage` | — | — | — | ✅* | ✅ |
| `settings.manage` | — | — | — | — | ✅ |

\* own store only. Roles are assigned **per store** via `user_store_roles`.

Enforced in three places: React hides controls (`<PermissionGate>`), a FastAPI dependency rejects the request, and RLS rejects the sync. Only the third is security.

```python
def require(permission: str):
    def dep(session: Session = Depends(current_session)) -> Session:
        if permission not in session.permissions:
            raise HTTPException(403, "permission_denied")
        return session
    return Depends(dep)
```

### 11.2 Supabase side

A **custom access token hook** stamps `store_ids` and `permissions` into the JWT at login, so RLS policies need no per-row joins:

```sql
create or replace function auth.has_perm(p text) returns boolean
language sql stable as $$
  select coalesce(auth.jwt() -> 'app_metadata' -> 'permissions' ? p, false);
$$;

create policy sales_insert on sales for insert to authenticated
  with check (auth.in_store(store_id)
              and auth.has_perm('sale.create')
              and cashier_id = auth.uid());

-- No UPDATE or DELETE policy exists. Corrections are compensating rows.
```

JWT TTL of 1 hour keeps revocations timely. Refresh tokens go in the OS keychain via the `keyring` package — Windows Credential Manager, macOS Keychain, Secret Service on Linux — never in SQLite or a config file.

### 11.3 Manager override

The most-used RBAC flow in a real shop: a cashier needs a void, the supervisor authorises at the same terminal without logging the cashier out.

```
blocked action → override modal → supervisor employee code + PIN
   ONLINE : Edge Function /authorize-override verifies argon2id PIN
            and permission, returns a 90-second signed grant, audits
   OFFLINE: verify against cached_users.pin_hash + cached_permissions
            (snapshot ≤14 days old, signed by server)
        ↓
action executes · sale_line stamped overridden_by + reason
        ↓
audit row → outbox → synced on reconnect
```

### 11.4 Offline authentication

First online login caches the user, an argon2id PIN hash, a permission snapshot and a signed expiry. Offline login is employee code + PIN against that cache, with a hard TTL (default 14 days) after which one online authentication is required. This bounds how long a dismissed employee can open a till. Deactivation purges the snapshot on next sync and emits `auth.revoked` over the WebSocket.

**Tune argon2id to ~100 ms** on target hardware, not the library default — a 500 ms PIN check is felt at a queue.

---

## 12. Peripherals

| Device | Library / interface | Notes |
|---|---|---|
| Receipt printer | `python-escpos` (USB, serial, network, CUPS, Win32Raw) | Queue + retry thread; reprint-last always available |
| Cash drawer | Kick pulse via printer | ~10 lines; every open audited with actor and reason |
| Scale | `pyserial` (Toledo/CAS continuous protocol) | Streams `scale.weight` over WebSocket for `is_weighed` lines |
| Pole display | `pyserial`, or a second pywebview window | Second window needs no driver code |
| **Scanner** | **HID wedge — no driver. In v1 and in use** (Helett HT20 Pro) | See §10.1 |
| Card terminal | *Deferred past v1* — semi-integrated, vendor SDK | See §13.6 |

Every row above except the scanner is deferred past v1 (execution plan §8). The scanner is in because it costs nothing: an HID wedge is indistinguishable from a keyboard, so supporting it is a capture hook that had to exist anyway for typed entry — no driver, no transport, no worker thread, no per-model certification pass. That reasoning does not extend to any other row.

`python-escpos` covering five transports out of the box is the strongest single argument for this stack — it is the hardest part of the Rust variant, largely solved, and it is what makes the deferral cheap to reverse.

**Customer-facing QR display:** the second pywebview window doubles as the UPI QR surface, so no additional hardware is needed. Thermal printers also render QR natively via ESC/POS `GS ( k`, which is the fallback when there is no second screen.

Payment handling is in §13. **No card reader in v1 means no PCI scope at all** — the application never sees card data because there is no card path.

---

## 13. Payments and tender

**v1 scope: cash only, UPI only, and cash + UPI split.** Card is deferred (§13.6).

### 13.1 One lifecycle for every method

Cash resolves instantly; UPI does not. Modelling cash as synchronous bakes an assumption that a later method breaks, so both go through the same states:

```
PENDING ──→ APPROVED ──→ (counts toward balance)
   │
   ├──→ DECLINED / CANCELLED / EXPIRED  (cart stays open, retry)
   └──→ UNKNOWN  →  sale posted as requires_review
```

```python
class PaymentProvider(Protocol):
    async def begin(self, attempt: PaymentAttempt) -> AttemptState: ...
    async def poll(self, attempt_id: str) -> AttemptState: ...
    async def cancel(self, attempt_id: str) -> None: ...

class CashProvider:   # returns APPROVED immediately, same lifecycle
class UpiQrProvider:  # PENDING until confirmed or expired
```

Adding a provider later is a registry entry plus a tender button, not a change to the register's state machine.

**As built:** the state machine is a transition table with terminal states that are genuinely terminal — an APPROVED attempt cannot be walked back to PENDING — and the registry currently holds cash alone. A tender request for `upi` is refused with “no provider” rather than quietly succeeding, which is the behaviour a phase-4 regression should trip over.

### 13.2 Split-tender loop

The sale closes on balance, not on a single payment:

```
balance = grand_total − Σ(approved payments)
while balance > 0:
    choose method → begin attempt → resolve
post sale when balance ≤ 0;  change due = −balance  (cash only)
```

Because UPI amounts are embedded in the QR, only cash can overpay — so change is computed only for a cash-final tender. A UPI attempt is always for the exact outstanding balance.

### 13.3 UPI QR flow

The QR is generated locally from a UPI deep link; no PSP, no internet, no vendor account beyond a VPA:

```
upi://pay?pa={vpa}&pn={payee}&am={amount}&cu=INR&tn={note}&tr={txn_ref}
```

Rendered with `qrcode` to the customer-facing window, or printed via ESC/POS. `txn_ref` is the `payment_attempts.id`, which is what makes later settlement reconciliation possible.

**Confirmation in v1 is manual attestation.** The cashier sees the notification on the merchant phone or soundbox and taps "Received"; optionally captures the UTR. Recorded as `confirmation_method='manual_attestation'` with `confirmed_by`, `verified = 0`, and an audit row. This is how most small shops already operate, and it works fully offline — UPI needs the *customer's* connectivity, not the terminal's.

QR attempts expire (default 5 min) and auto-cancel, so an abandoned scan cannot leave a cart wedged.

### 13.4 Rounding is tender-dependent

If you don't stock coins, a ₹123.40 total is collected as ₹123 in cash but ₹123.40 over UPI. Rounding therefore cannot be applied to the line total — it is applied **after method selection**, as `sales.rounding_adjustment`, and shown on the receipt as its own line. On a split tender, round only the cash portion. Getting this wrong produces a permanent, unexplainable variance at every shift close.

### 13.5 Reconciliation and refunds

- **Shift close:** cash counted against expected float, and UPI attested totals listed separately with their `txn_ref`s for manual comparison against the bank or PSP statement. Unverified UPI is a distinct figure on the Z-report, not folded into takings.
- **`requires_review` queue:** for the "customer insists they paid, no notification arrived" case. A supervisor resolves it with `sale.review.resolve` — never the cashier, and never by voiding a possibly-real payment.
- **UPI refunds are out of band in v1.** The POS records the refund with `refund_method` and an external reference; the actual money moves through the merchant app. Alternatively refund UPI sales in cash, if store policy allows. Do not build a UPI refund button that cannot actually move money.

### 13.6 Path to card and automated UPI

Both slot into `PaymentProvider` without touching the register:

- **PSP-verified UPI** — the webhook lands in the cloud, not on the terminal (which is behind NAT and often offline). An Edge Function writes a confirmation row; the terminal picks it up through the existing sync pull and flips `verified = 1`. Offline still falls back to attestation.
- **Card** — a semi-integrated terminal, at which point PCI scope returns (SAQ-C-VT/P2PE) and the vendor's SDK shape matters. Get those docs before building it, not before building the rest.

---

## 14. Packaging, updates, supervision

This is the weakest area of the stack and needs deliberate engineering.

- **PyInstaller** one-folder build (faster start, easier patching than one-file), React built to static assets bundled alongside. Expect 90–160 MB and 2–4 s cold start; the splash screen absorbs it.
- **Code-sign the executable and installer** on Windows. Unsigned PyInstaller binaries draw antivirus false positives, which across a store estate becomes the top support cost. Budget for an EV certificate and submit builds to Microsoft for reputation.
- **Installer:** Inno Setup on Windows, `.dmg` on macOS. Install per-machine, run per-user.
- **Updates are yours to build:** a signed `version.json` manifest, background download, signature verification, apply on next launch, staged rollout by store, and a rollback to the previous folder. No mature Python equivalent of Tauri's updater exists.
- **Supervision:** the webview polls `/health` behind a splash and shows a clear diagnostic if the service never comes up. Install a Windows service or scheduled task to restart the app on crash, and ship structured logs to a rotating file with an operator-facing diagnostics screen.
- **Single-instance lock** via a lock file plus port probe — two tills on one register would duplicate receipt sequences.
- **Bundle the SQLite database outside the install directory** (`%PROGRAMDATA%` / `~/Library/Application Support`) so updates never touch it, and back it up on every shift close.

---

## 15. Testing

| Layer | Approach |
|---|---|
| `domain/` | pytest + Hypothesis. Pricing, tax rounding, promotion stacking, barcode parsing. No fixtures, no DB, milliseconds to run. |
| `services/` | pytest against a temp SQLite file; assert transaction atomicity by injecting failures mid-post. |
| `sync/` | `respx` to mock Supabase; test retry, idempotent replay, quarantine, cursor advance. |
| `api/` | FastAPI `TestClient`; permission matrix as a parametrised table. |
| RLS | pytest against **`pgserver`** — a real Postgres bundled as a wheel, so the suite runs with no Docker Desktop on a developer's Windows machine. 27 tests asserting each role is denied what it should be. |
| UI | Vitest for the barcode capture timing hook — 17 cases, with a manually advanced clock, since jsdom does not advance `timeStamp` and timing is the hook's entire job. Playwright for the tender flow: not yet written. |
| Payments | `FakeProvider` driving every terminal state including `UNKNOWN` and `EXPIRED`; split-tender balance loop and cash rounding as parametrised tables. |
| Peripherals | Fake transports; one manual certification pass per supported printer model. |

Three CI gates are worth naming because they catch classes of error rather than instances: **`import-linter`** fails the build if `domain/` grows an import from `data/`; a **no-float check** walks the AST of every `domain/` module and rejects `float` outright; and a **generated-files check** re-runs the code generators and fails if the permission matrix in Postgres or the TypeScript API types have drifted from the Python source they come from.

The pure-Python domain is the payoff here: the rules most likely to be wrong are also the cheapest to test, with no window and no printer attached.

---

## 16. Non-functional targets

| Metric | Target |
|---|---|
| Scan → line on screen | < 150 ms (local HTTP round trip included) |
| Cold start to sellable | < 5 s (splash covers Python import time) |
| Sale commit, local | < 80 ms |
| Offline operating window | ≥ 14 days |
| Sync catch-up, 1 day backlog | < 90 s |
| Local catalog capacity | 200k products, FTS search < 100 ms |
| Resident memory | < 300 MB including webview |

Slightly looser than the Rust variant on start-up and scan latency. Both remain comfortably below the threshold a cashier notices.

---

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Float money reaching the database | Integer minor units in SQLite; `Money` type in the domain; lint against `float` in `domain/` |
| `database is locked` at a busy till | Single write connection + lock, WAL, `busy_timeout`, all writes via repositories |
| Antivirus quarantines the build | Code signing, EV cert, reputation submission, allow-list guidance for IT |
| No auto-updater out of the box | Signed manifest + staged rollout built in phase 7; treat as a first-class feature |
| Blocking peripheral I/O stalls the API | Dedicated threads + queues; nothing blocking in the event loop |
| Malicious page hits the local API | Loopback bind, session token, Host and Origin checks, ephemeral port |
| Sync loop double-posts sales | UUID v7 PKs + `ON CONFLICT DO NOTHING` |
| Inventory drift across terminals | Ledger deltas, server-side aggregation, periodic cycle counts |
| Ex-employee uses cached login | Snapshot TTL, revocation on sync, 1-hour JWT |
| `service_role` key extracted from bundle | Never bundled; privileged work in Edge Functions |
| Schema migration on offline terminals | Forward-only local migrations; payloads carry a schema version, server rejects mismatch with a forced-update prompt |
| Terminal clock tampering | Server timestamps authoritative for reporting |
| Cashier attests UPI that never arrived (theft or error) | `verified = 0` until settlement match; UPI reported separately on Z-report; attestation audited with actor |
| Cash rounding applied before tender choice | `rounding_adjustment` computed post-method, cash portion only (§13.4) |
| Abandoned QR wedges the cart | Attempt expiry (5 min) with auto-cancel |
| A scan is handled twice, or a typed search is eaten as a barcode | The global capture hook **excludes** every text field, including the register's own; the field handles scans natively. Asserted in the UI suite (§10.1) |
| A mis-scan resolves to the wrong product | Check digit validated before any interpretation; a failing code is refused outright, never partially matched |
| Wrong GST rate on a reprint after a rate change | `tax_code` and `tax_rate_bp` snapshotted onto `sale_lines` at post time (§7) |
| Register rewritten when card is added | Every method goes through `PaymentProvider` and the same state machine from day one |

---

## 18. Delivery phases

1. ✅ **Foundation** — Postgres schema, RLS, roles/permissions seed; SQLite migrations; shell bootstrap with health gate and local auth token.
2. ✅ **Domain core** — `Money`, cart, pricing, tax, barcode parser **and generator**, all under property tests, before any UI exists.
3. ✅ **Register MVP** — scan capture, cart endpoints, `PaymentProvider` + attempt state machine with cash as the first provider, split-tender balance loop, tender-dependent rounding, `post_sale` transaction, receipt on screen with PDF on demand. Printing and drawer deferred with the rest of the peripherals.
4. **UPI tender** — QR generation and customer display, manual attestation with audit, attempt expiry, `requires_review` queue, UPI totals on the Z-report.
5. **Sync engine** — outbox, pusher with backoff and quarantine, watermark pullers, stock ledger, sync-status UI.
6. **Inventory** — receiving against POs, counts, adjustments, label printing, low-stock alerts.
7. **RBAC completion** — override flow, offline snapshots with TTL, audit log viewer.
8. **Shifts and reporting** — sessions, cash movements, cash-vs-UPI reconciliation, X/Z reports.
9. **Fleet operations** — signed installer, updater with staged rollout, crash supervision, remote diagnostics.

*Later:* PSP-verified UPI, then card (§13.6).

Phases 1–4 give a sellable single-terminal till taking cash and UPI. Phase 5 is what makes it a system.

---

## 19. Decisions

**Closed** (execution plan §2), and all of them are now built into the code rather than merely agreed:

- **Single store.** `store_id` still scopes every table and every policy, so multi-store is a data question later, not a rewrite.
- **GST-inclusive pricing.** Tax is extracted, never added (§6).
- **Cash rounding to the nearest ₹1**, applied at the tender boundary only, recorded as its own figure, never touching a line total or the GST base (§13.4).
- **UPI VPA per terminal**, for settlement matching.
- **Receipt delivery: on screen, with PDF on demand.** Both render from one document model. WhatsApp sharing is wanted but unbuilt — it needs a customer phone number captured at the till, which is a question about what the shop stores, not about rendering.
- **Windows**, running on Python 3.10+ (§7).
- **The scanner is in scope** and in use; every other peripheral is deferred (§12).

**Still open:**

- **Whether a customer-facing screen exists** — if not, the UPI QR needs a printer instead, which changes the tender flow. Blocks part of phase 4.
- **PSP for verified UPI later** — not needed for v1, but the choice determines the webhook payload the Edge Function will parse.
- **Real per-product GST rates.** The development catalogue's tax codes are *inferred* from `main_group`, with 19,614 products defaulted to 18%. This is a development convenience and must not reach a pilot; the owner has to review them during the week-18 catalogue migration.
- **The code-signing certificate**, unstarted. Pure calendar time, and it blocks phase 9 rather than being blocked by anything.

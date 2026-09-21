# Phase 7 acceptance — run sheet

The automated half is done: **1065 Python, 103 RLS, 217 UI tests.** This is the
half a person has to do, because it needs a screen, a network cable and a real
Supabase project.

**Budget ninety minutes.**

---

## Read these three before you start

1. **§D2 fails by passing.** The proof written in the plan for the snapshot TTL
   no longer works — editing the expiry now breaks the seal instead of ageing
   the snapshot, and the login is refused for the wrong reason. Read D2 before
   you begin Part D.
2. **A stale bundle wastes the afternoon.** Every screen assertion below is
   meaningless if you are looking at yesterday's JavaScript. S2 is the check.
3. **PowerShell 5.1 has no `&&`.** Use `;`. A parser error on step one is a
   dispiriting way to begin.

---

## The run at a glance

| Part | Proves | Needs | Time |
|---|---|---|---|
| **S** Setup | You are testing today's code | — | 10 min |
| **A** Audit log viewer | The screen answers "who did that?" without SQL | nothing deployed | 25 min |
| **B** Deploy | Two Edge Functions exist in the project | `supabase` CLI | 5 min |
| **C** Override, offline | A supervisor lends a key for 90s and the log names both | B, a cable to pull | 30 min |
| **D** Revocation + snapshot | A leaver stops working; the cache cannot be edited | B | 20 min |

Part A can be done right now. C and D need B first.

## Progress

- [ ] S — Setup
- [ ] A — Audit log viewer
- [ ] B — Deploy the Edge Functions
- [ ] C — Override, offline
- [x] D — Revocation and the snapshot
- [ ] Results written into the plan, including what this run *cannot* prove

---

## What you are proving

Four claims, in order of how much they would hurt if false:

1. A cashier cannot discount a line, a supervisor can lend them the key for
   ninety seconds, and the log names **both** people.
2. A dismissed employee stops being able to open this till.
3. The offline snapshot cannot be extended by editing the database.
4. The audit screen answers "who did that?" without SQL.

---

## Reference card

### Who is who

From `scripts/seed_local.py`:

| Code | Name | Role | PIN | Holds |
|---|---|---|---|---|
| **C001** | Anita Rao | cashier | `4913` | neither key below |
| **S001** | Vikram Shah | supervisor | `7261` | `sale.discount.line` |
| **M001** | Priya Nair | manager | `5820` | `sale.discount.line`, `user.manage` |

Two things to keep straight:

- **S001's seeded PIN is `7261`**, not the `7241` the test suite uses.
- **M001 can discount without an override** — she holds the key outright — so
  she is the wrong person to test the override with, and the only person who
  can open the audit screen.

### Where things are

| | |
|---|---|
| Repo | `D:\Github\pos` |
| Terminal database | `C:\ProgramData\RetailPOS\pos.sqlite3` |
| Log file | `C:\ProgramData\RetailPOS\logs\pos.log` |
| Built UI | `app\ui\assets` |

---

# S — Setup

## S1. Pull and build

**Do**

```powershell
cd D:\Github\pos
git pull                       # 13c1716 or later
.venv\Scripts\python -m pytest -q
cd ui-src; npm run build; cd ..
```

**Expect**

- [x] Tests green.
- [x] The build writes into `app\ui`.

## S2. Stale-bundle check

**Do**

```powershell
Get-ChildItem app\ui\assets | Select-Object Name, LastWriteTime
```

**Expect**

- [x] Both files timestamped **within the last few minutes**.

> If they are not, the build went somewhere else and nothing below this line
> means anything.

## S3. Launch

**Do**

```powershell
.venv\Scripts\python -m app.main
```

---

# A — The audit log viewer

Nothing needs deploying. This reads Supabase directly through PostgREST.

## A1. The tab is absent for a cashier

**Do** — sign in as **C001 / 4913**, open the admin screen.

**Expect**

- [x] There is **no** "Audit log" tab.

> Do this *before* A2. A tab that is present for everyone still looks correct
> when you are signed in as the person who should see it. This is the only one
> of the three enforcement points (§11.1) you can see with your eyes.

## A2. The tab is present for a manager

**Do** — sign out, sign in as **M001 / 5820**, open admin.

**Expect**

- [x] An "Audit log" tab appears.
- [x] Opening it lists rows without asking for anything.

## A3. The counts

**Do** — clear all filters.

**Expect**

- [x] **34 rows.**
- [x] The action dropdown offers **exactly seven** options:

| Action | Rows |
|---|---|
| `scan.resolved` | 11 |
| `sale.post` | 8 |
| `product.updated` | 6 |
| `barcode.added` | 4 |
| `barcode.withdrawn` | 3 |
| `product.created` | 1 |
| `scan.dismissed` | 1 |

- [x] **No eighth option.** No `price.opened`, no `stock.reorder_point_set`,
      no `override.granted`.

> Make this assertion slowly. The menu is built from the data, and a hardcoded
> one — offering filters that return nothing — is the bug that check exists
> for. None of those three actions has ever been written to this project.

## A4. The acceptance line

**Do** — filter the action to **`scan.dismissed`**.

**Expect**

- [x] One row.
- [x] "Who" says **Priya Nair (M001)** — not "System", not blank.
- [x] Every row's "Authorised by" column is an **em dash**.

> That is the criterion met: yesterday's dismissal and the person who wrote it,
> two clicks from opening the screen, no SQL. The em dash matters too — nothing
> in this project was ever an override, which is why that column needed a third
> sentence, distinct from the two kinds of missing name.

## A5. The date filter — the regression check

The target row happened at **18:35 UTC on the 15th**, which is **00:05 IST on
the 16th**. The table renders local time, so it shows on the **16th**.

**Do** — set **From = 16 September 2026**, **To = 16 September 2026**.

**Expect**

- [x] The row appears. *A single-day range must contain its own day.*

**Do** — set both to **15 September 2026**.

**Expect**

- [x] The row does **not** appear.
- [x] The filter row says "Times shown in this terminal's timezone."

> You look for it on the day you can see it on. Before the fix, **both** of
> those returned nothing — `lte` against a bare date made a one-day range a
> zero-width instant. So an empty list from the 16th means you are on a stale
> bundle: go back to S2.

## A6. The chain, and the question the screen cannot ask

**Do** — filter `product.created`.

**Expect**

- [x] One row at **00:08** IST. "What" shows `products`, **not** a barcode.

> That is correct, not a rendering bug. The plan's step said to find the same
> barcode here; it is not there, because that row carries the product's own
> columns and no code.

**Do** — filter `barcode.added`, then `scan.resolved`.

**Expect**

- [x] `barcode.added`: four rows, one at **00:08:50**.
- [x] `scan.resolved`: a row at **00:08:50**.

> **Identify these by timestamp, not by code.** The What column shows
> `product_barcodes` and `catalogued` for those rows — the barcode
> `7622202819933` lives in `after_json`, which the table does not render. You
> cannot see it, let alone search it.

> So the same barcode was **dismissed at 00:05:43 and resolved at 00:08:50** —
> three minutes apart, same person. The whole story is in the log.
>
> **And you cannot ask for it.** Those three rows have three different
> `entity_id`s and the filter cannot search `after_json`, so "what happened to
> this code?" takes three filters and a good memory. Worth a card rather than a
> fix here: the screen answers "who did that?", which is what it was built for.

---

# B — Deploy the migrations and the two Edge Functions

`authenticate-pin` is live at version 3. `authorize-override` and
`check-revocations` are written, tested, and **have never run against real
Supabase**.

> **Found the hard way on the first run:** this section deployed the functions
> and not migrations `0022`/`0023`. Without them the cloud's `sync_push` has
> never heard of an `override` entity — C3 passed, and C4 quarantined the
> override **and the sale travelling with it** as "unknown entity override".

**Do**

```powershell
supabase db push
supabase migration list
supabase functions deploy authorize-override
supabase functions deploy check-revocations
supabase functions list
```

**Expect**

- [ ] `migration list` shows **0022** and **0023** applied remotely.

**Expect**

- [x] All three show **ACTIVE**.

> **Do not chase the `verify_jwt` flag.** `authenticate-pin` is deployed with
> it *on* despite its own header saying `--no-verify-jwt`, and it works — the
> anon key is itself a signed project JWT, so a gateway demanding one lets it
> straight through. The flag decides nothing here. `check-revocations` refuses
> the anon key by name, in code, which is the check that matters.

---

# C — The override, offline

**This is slice 3's exit criterion.** It needs no deployment — the offline path
verifies against the local cache. Part B matters for C4 only.

## C1. Cable out

**Do** — physically unplug the network, or disable the adapter.

> Not airplane mode with a captive portal. The till must not be able to reach
> Supabase at all.

## C2. A cashier is refused

**Do** — sign in as **C001 / 4913**. Get a line in the basket. Press **%** on
that line, enter **2**, press Apply.

**Expect**

- [x] The supervisor dialog opens.
- [x] It names the act in words: *"take 2.00 off line 1"*.

## C3. A supervisor authorises, and does not take over the till

**Do** — in the dialog, enter **S001** and PIN **7261**.

**Expect**

- [x] The discount applies — basket total drops by ₹2.00.
- [x] The header still says **Anita Rao**.
- [x] Within ninety seconds, a second discount on another line needs **no** PIN.
- [x] After ninety seconds, it asks again.

> The header is the whole property: the cashier was not signed out, the basket
> survived, and the supervisor walked away. *Verify a second person without
> becoming them.*

**Do** — still offline, check the trail. Save this as `trail.py` in the repo
root, run `.venv\Scripts\python trail.py`, delete it afterwards:

```python
import json

from app.config import get_settings
from app.data.db import Database

s = get_settings()
print("reading", s.db_path)

db = Database(s.db_path)
rows = db.query(
    "select action, approver_id, after_json "
    "from audit_log order by occurred_at desc limit 3"
)
for action, approver, after in rows:
    print(action, approver, json.loads(after or "{}"))

pending = db.query("select entity, entity_id from outbox where entity = 'override'")
print("outbox override rows:", pending)
```

> A file, not a `python -c` one-liner: PowerShell strips the escaped quotes
> before Python sees them. The first line prints which database it read —
> if `POS_DATA_DIR` is still set from a D2 rehearsal, this is where you find out.

**Expect**

- [x] One `override.granted` row whose `after_json` names **C001 and S001**,
      and whose `approver_id` is set.
- [x] The outbox holds a row with `entity = 'override'` waiting to be pushed.

> The terminal's `audit_log` has `actor_id` and `approver_id` and **no code
> columns** — the readable codes ride in `after_json`, because a till has no
> employees table to join against.

## C4. Reconnect, and the row leaves

**Do** — plug the cable back in and wait for a sync cycle (90 seconds idle, or
post a sale to nudge it).

**Expect**

- [x] In Supabase, `select * from audit_log where action = 'override.granted'`
      returns the row, with both `actor_id` and `approver_id` set.
- [x] Re-opening the audit screen as M001 shows an **eighth** option in the
      action dropdown.
- [x] "Authorised by" on that row shows **Vikram Shah (S001)**.

> The eighth option appearing is the data-driven filter proving itself. Nobody
> added it anywhere.

**If the sync indicator says rows were refused:** the message points at "the
failures list", which **does not exist as a screen** — the route and client
function are there, nothing renders them. Read the table directly. Save as
`refused.py`, run, delete:

```python
from app.config import get_settings
from app.data.db import Database

db = Database(get_settings().db_path)
rows = db.query(
    "select id, outbox_id, failed_at, error, substr(payload_json, 1, 300) "
    "from sync_failures where acknowledged = 0 order by id"
)
for fid, oid, at, err, payload in rows:
    print(f"#{fid} outbox={oid} at {at}\n  {err}\n  {payload}\n")
```

Quarantined rows leave the outbox, so querying `outbox` shows nothing. Fix the
cause, then **Try again** on the sync indicator as M001 requeues them.

## C5. The scanner guard

**Do** — open the override dialog again. With the PIN box focused, **scan an
item across the counter**.

**Expect**

- [x] The PIN box is empty and says *"That looked like a scan, not a PIN."*
- [x] The scanned item did **not** land in the basket.
- [x] `select consecutive_pin_failures from cached_users where employee_code =
      'S001'` is **0** — the scan did not spend an attempt.

> No scanner? This one stays test-only. Typing fast by hand will not reach
> machine speed, and should not.

---

# D — Revocation and the snapshot

> **Rehearse D2 and D3 against a throwaway database first.** It costs nothing
> and every command below was checked this way before this guide was written:
>
> ```powershell
> $env:POS_DATA_DIR = "$env:TEMP\pos-rehearsal"
> # ...run D2 and D3 here, then:
> Remove-Item Env:\POS_DATA_DIR
> ```
>
> With that set, `get_settings()` points at a fresh database and your real
> terminal is untouched. **Unset it before the real run.**

## D1. A deactivated employee stops working

**Do** — in Supabase:

```sql
update public.employees set status = 'suspended' where employee_code = 'S001';
```

Sign in at the till as **C001**, open a basket with a line in it, wait for a
sync cycle.

**Expect**

- [x] The basket is **untouched** — no sign-out, no lost lines.
- [x] `select * from cached_users where employee_code = 'S001'` locally returns
      **nothing**. The row is purged.
- [x] Trying the override with S001 / 7261 is refused with *"has not signed in
      on this terminal before"* — a **503**, not a wrong-PIN error.

> A deactivation is a leaver processed at 11:40, not an emergency. The customer
> in front of you finishes.

**Do** — now deactivate the person actually signed in:

```sql
update public.employees set status = 'suspended' where employee_code = 'C001';
```

**Expect**

- [x] The open basket still works, all the way to taking payment.
- [x] Starting a **new** sale is refused: *"This account has been deactivated.
      The last sale was completed; please sign in with another account."*

**Do** — put them both back:

```sql
update public.employees set status = 'active'
 where employee_code in ('C001', 'S001');
```

## D2. The snapshot TTL — the plan's proof no longer works

> **The trap.** The plan says: *"set a snapshot back 15 days and confirm the
> offline login refuses."* Since `5b477a3` every cached row carries a MAC over
> the columns that decide access, `snapshot_expires_at` among them. So editing
> that column now **breaks the seal** rather than expiring the snapshot. The
> login is refused — for the wrong reason, with the TTL never consulted. It
> looks like a pass and proves nothing.
>
> This is the exact failure this phase kept finding, arriving in its own
> acceptance script.

The two answers are distinguishable if you read the message:

| What you did | Message | Status |
|---|---|---|
| Broke the seal | "has not signed in on this terminal before" | **503** |
| Genuinely expired | "This terminal has been offline too long" | **401** |

**Test the TTL by re-sealing, not by editing.** Seed a user whose snapshot was
signed sixteen days ago — the repository computes the MAC, so the row is valid
and the snapshot is genuinely old.

**Do** — save this as `seed_stale.py` in the repo root and run
`.venv\Scripts\python seed_stale.py`:

```python
from datetime import timedelta

from app.config import get_settings
from app.data.db import Database
from app.data.repositories.users import CachedUserRepository
from app.domain import permissions as p
from app.domain.identity import utcnow
from app.security.snapshot_mac import SnapshotSealer, mac_key
from app.services.auth_service import AuthService, SessionStore

s = get_settings()
db = Database(s.db_path)
users = CachedUserRepository(
    db, sealer=SnapshotSealer(mac_key(s.store_code, s.terminal_code))
)
auth = AuthService(
    users=users,
    sessions=SessionStore(),
    cloud=None,
    store_code=s.store_code,
    terminal_code=s.terminal_code,
    settings=s,
)
auth.seed_local_user(
    user_id="018f0000-0000-7000-8000-0000000000ff",
    employee_code="X001",
    full_name="Stale Person",
    store_id="018f0000-0000-7000-8000-000000000100",
    pin="1234",
    roles=frozenset({p.CASHIER}),
    permissions=p.permissions_for(frozenset({p.CASHIER})),
    signed_at=utcnow() - timedelta(days=16),
)
print("seeded X001 with a snapshot signed 16 days ago")
```

**Do** — with the cable out, sign in as **X001 / 1234**.

**Expect**

- [x] Refused with *"This terminal has been offline too long. Connect to the
      internet once to sign in."* — as a **401**.

> The row is intact, the seal verified, and the date is what refused. That is
> the TTL doing its job.

Delete `seed_stale.py` when you are done — it is not part of the repo.

## D3. The seal — edit the file and watch it stop working

**Do** — *now* edit the column, and expect the other message:

```sql
-- sqlite3 C:\ProgramData\RetailPOS\pos.sqlite3
update cached_users set snapshot_expires_at = '2030-01-01T00:00:00+00:00'
 where employee_code = 'X001';
```

**Expect**

- [x] Signing in as X001 offline is refused with *"has not signed in on this
      terminal before"* — **503**, not 401.
- [x] `pos.log` carries an ERROR: *"cached identity for X001 failed its seal
      and will not be used"*.

> Ten years of extra access, granted with one UPDATE — and the row stopped
> being readable instead.

**Do** — clear up:

```sql
delete from cached_users where employee_code = 'X001';
```

---

# What this run cannot prove

Write the result beside these, because a green run gets quietly credited with
covering them and it does not.

| Not proven | Why it cannot be |
|---|---|
| **"System"** in the Who column | Needs a row with a null `actor_id`. All 34 rows have an actor. |
| **"Someone outside this store"** | Needs a second store. There is one. |
| **`store_id is null` rows** | The branch 0019 widened the policy for is exercised by no row in this data, catalogue edits included. |
| **The online override** | Part C tests the offline path deliberately, because that is the exit criterion. |

All four stay proven by test only.

To exercise the online override as well, repeat **C2–C3 with the cable in** and
a supervisor who has never signed in on this till.

# Running phase 7's acceptance by hand

The automated half is done: 1065 Python tests, 103 RLS tests, 217 UI tests.
This is the half a person has to do, because it needs a screen, a network
cable and a real Supabase project.

Budget ninety minutes. Part A needs nothing deployed and can be done now.
Part B deploys two Edge Functions. Parts C and D need B first.

**Read the trap in §D2 before you start D.** The proof written in the plan for
the snapshot TTL no longer works, and it fails by *passing*.

---

## What you are proving

Four claims, in order of how much they would hurt if false:

1. A cashier cannot discount a line, a supervisor can lend them the key for
   ninety seconds, and the log names **both** people.
2. A dismissed employee stops being able to open this till.
3. The offline snapshot cannot be extended by editing the database.
4. The audit screen answers "who did that?" without SQL.

## Before anything

```powershell
cd D:\Github\pos
git pull                       # 98d9989 or later
.venv\Scripts\python -m pytest -q
cd ui-src; npm run build; cd ..
```

`;` rather than `&&` — Windows PowerShell 5.1 has no pipeline chain operators
and `&&` is a parser error, which is a dispiriting way to fail step one.

**Stale-bundle check.** The one failure that wastes an afternoon is testing
yesterday's JavaScript:

```powershell
Get-ChildItem app\ui\assets | Select-Object Name, LastWriteTime
```

Both files must be from the last few minutes. If they are not, the build went
somewhere else and nothing below means anything.

**Who is who**, from `scripts/seed_local.py`:

| Code | Name | Role | PIN | Holds |
|---|---|---|---|---|
| C001 | Anita Rao | cashier | 4913 | neither key below |
| S001 | Vikram Shah | supervisor | **7261** | `sale.discount.line` |
| M001 | Priya Nair | manager | 5820 | `sale.discount.line`, `user.manage` |

Two things to keep straight. S001's seeded PIN is **7261**, not the 7241 the
test suite uses. And **M001 can discount without an override** — she holds the
key outright — so she is the wrong person to test the override with and the
only person who can open the audit screen.

The terminal database is `C:\ProgramData\RetailPOS\pos.sqlite3`.

---

# Part A — the audit log viewer

Nothing needs deploying. This reads Supabase directly through PostgREST.

## A1. Launch

```powershell
.venv\Scripts\python -m app.main
```

## A2. The tab is not there for a cashier

Sign in as **C001 / 4913**, open the admin screen.

- [ ] There is **no** "Audit log" tab.

This is the first of the three enforcement points (§11.1) and the only one you
can see. Do it before A3, because a tab that is present for everyone still
looks correct when you are signed in as the person who should see it.

## A3. It is there for a manager

Sign out, sign in as **M001 / 5820**, open admin.

- [ ] An "Audit log" tab appears.
- [ ] Opening it lists rows without asking for anything.

## A4. The counts

With no filters set:

- [ ] **34 rows.**

The action dropdown offers exactly seven, and this is the assertion worth
making slowly — it is built from the data, and a hardcoded menu is the bug the
check exists for:

| Action | Rows |
|---|---|
| `scan.resolved` | 11 |
| `sale.post` | 8 |
| `product.updated` | 6 |
| `barcode.added` | 4 |
| `barcode.withdrawn` | 3 |
| `product.created` | 1 |
| `scan.dismissed` | 1 |

- [ ] Seven options and no eighth. No `price.opened`, no
      `stock.reorder_point_set`, no `override.granted` — none has ever been
      written to this project.

## A5. The acceptance line

Filter the action to **`scan.dismissed`**.

- [ ] One row.
- [ ] "Who" says **Priya Nair (M001)** — not "System", not blank.

That is the criterion: yesterday's dismissal and the person who wrote it, two
clicks from opening the screen, no SQL.

- [ ] Every row's "Authorised by" column is an **em dash**. Nothing in this
      project was ever an override, which is why that column needed a third
      sentence distinct from the two kinds of missing name.

## A6. The date filter — the regression check

This is new since `98d9989` and it is the part most likely to be wrong again,
because the bug was invisible: the filter answered, and the answer was empty.

The target row happened at **18:35 UTC on the 15th**, which is **00:05 IST on
the 16th**. The table renders local time, so it shows on the 16th.

- [ ] Set **From = 16 September 2026** and **To = 16 September 2026**. The row
      appears. *A single-day range must contain its own day.*
- [ ] Set both to **15 September 2026**. The row does **not** appear.

You look for it on the day you can see it on. Before the fix, both of those
returned nothing — `lte` against a bare date made a one-day range a
zero-width instant — so if you get an empty list from the 16th, you are on a
stale bundle. Go back to the stale-bundle check.

- [ ] The filter row says "Times shown in this terminal's timezone."

## A7. The chain, and the question the screen cannot ask

The plan's step said to filter `product.created` and find the same barcode.
**It is not there** — that row carries the product's own columns and no code.

- [ ] Filter `product.created`: one row at **00:08** IST, "What" shows
      `products` rather than a barcode. That is correct, not a rendering bug.
- [ ] Filter `barcode.added`: four rows, one of them **7622202819933** at
      00:08:50.
- [ ] Filter `scan.resolved`: the same code appears again, at 00:08:50.

So the same barcode was **dismissed at 00:05:43 and resolved at 00:08:50**,
three minutes apart, by the same person. The whole story is in the log.

**And you cannot ask for it.** Those three rows have three different
`entity_id`s and the filter has no way to search `after_json`, so "what
happened to this code?" takes three separate filters and a good memory. Worth
noting on the board rather than fixing here — the screen answers "who did
that?", which is what it was built for.

---

# Part B — deploy the two Edge Functions

`authenticate-pin` is live at version 3. `authorize-override` and
`check-revocations` are written and tested and **have never run against real
Supabase**. Nothing in Part A needed them; everything in C and D does.

```powershell
supabase functions deploy authorize-override
supabase functions deploy check-revocations
```

- [ ] `supabase functions list` shows all three ACTIVE.

**Do not chase the `verify_jwt` flag.** `authenticate-pin` is deployed with it
*on* despite its own header saying `--no-verify-jwt`, and it works — the anon
key is itself a signed project JWT, so a gateway demanding one lets it
straight through. The flag decides nothing here. `check-revocations` refuses
the anon key by name, in code, which is the check that matters.

---

# Part C — the override, offline

**This is slice 3's exit criterion**, and it needs no deployment: the offline
path verifies against the local cache. Part B matters for C4 only.

## C1. Cable out

Physically unplug the network, or disable the adapter. Not airplane mode with
a captive portal — the till must not be able to reach Supabase at all.

## C2. A cashier is refused

Sign in as **C001 / 4913**. Scan or type a barcode to get a line in the
basket. Press the **%** button on that line, enter **2**, press Apply.

- [ ] The supervisor dialog opens. It names the act in words: "take 2.00 off
      line 1".

## C3. A supervisor authorises, and does not take over the till

In the dialog, enter **S001** and PIN **7261**.

- [ ] The discount applies — the basket total drops by ₹2.00.
- [ ] The header still says **Anita Rao**. The cashier was not signed out, the
      basket survived, and the supervisor walked away. *That is the whole
      property: verify a second person without becoming them.*
- [ ] Within ninety seconds, a second discount on another line needs **no**
      PIN. After ninety seconds it asks again.

Then, still offline, check the trail:

```powershell
.venv\Scripts\python -c "import json; from app.config import get_settings; from app.data.db import Database; db = Database(get_settings().db_path); rows = db.query(\"select action, approver_id, after_json from audit_log order by occurred_at desc limit 3\"); print([(r[0], r[1], json.loads(r[2] or '{}')) for r in rows])"
```

The terminal's `audit_log` has `actor_id` and `approver_id` and **no code
columns** — the readable codes ride in `after_json`, because a till has no
employees table to join against.

- [ ] One `override.granted` row whose `after_json` names **C001 and S001**,
      and whose `approver_id` is set.
- [ ] The outbox holds a row with `entity = 'override'` waiting to be pushed.

## C4. Reconnect, and the row leaves

Plug the cable back in and wait for a sync cycle (90 seconds idle, or post a
sale to nudge it).

- [ ] In Supabase, `select * from audit_log where action = 'override.granted'`
      returns the row, with both `actor_id` and `approver_id` set.
- [ ] Re-open the audit screen as M001. The action dropdown now offers an
      eighth option, and the "Authorised by" column shows **Vikram Shah (S001)**
      on that row.

That eighth option appearing is the data-driven filter proving itself: nobody
added it anywhere.

## C5. The scanner guard

Still at the till, open the override dialog again. With the PIN box focused,
**scan an item across the counter**.

- [ ] The PIN box is empty and says "That looked like a scan, not a PIN."
- [ ] The scanned item did **not** land in the basket.
- [ ] `select consecutive_pin_failures from cached_users where employee_code =
      'S001'` is **0** — the scan did not spend an attempt.

If you have no scanner, this one stays test-only; typing fast by hand will not
reach machine speed and should not.

---

# Part D — revocation and the snapshot

## D1. A deactivated employee stops working

In Supabase:

```sql
update public.employees set status = 'disabled' where employee_code = 'S001';
```

Sign in at the till as **C001**, open a basket with a line in it, and wait for
a sync cycle.

- [ ] The basket is **untouched** — no sign-out, no lost lines. A deactivation
      is a leaver processed at 11:40, not an emergency, and the customer in
      front of you finishes.
- [ ] `select * from cached_users where employee_code = 'S001'` in the local
      database returns **nothing**. The row is purged.
- [ ] Try the override again with S001 / 7261: refused with "has not signed in
      on this terminal before" — a 503, not a wrong-PIN error.

Now deactivate the person actually signed in:

```sql
update public.employees set status = 'disabled' where employee_code = 'C001';
```

- [ ] The open basket still works, all the way to taking payment.
- [ ] Starting a **new** sale is refused: "This account has been deactivated.
      The last sale was completed; please sign in with another account."

Put them both back:

```sql
update public.employees set status = 'active'
 where employee_code in ('C001', 'S001');
```

## D2. The snapshot TTL — **the plan's proof no longer works**

The plan says: *"set a snapshot back 15 days and confirm the offline login
refuses."* Since `5b477a3` every cached row carries a MAC over the columns that
decide access, `snapshot_expires_at` among them.

**So editing that column now breaks the seal rather than expiring the
snapshot.** The login is refused — and it is refused for the wrong reason,
with the TTL never consulted. It looks like a pass and proves nothing. This is
the exact failure this phase kept finding, arriving in its own acceptance
script.

The two answers are distinguishable if you read the message:

| What you did | Message | Status |
|---|---|---|
| Broke the seal | "has not signed in on this terminal before" | 503 |
| Genuinely expired | "This terminal has been offline too long" | 401 |

**Rehearse against a throwaway database first.** Every command in D2 and D3
was checked this way before this guide was written, and it costs nothing:

```powershell
$env:POS_DATA_DIR = "$env:TEMP\pos-rehearsal"
# ...run D2 and D3 here, then:
Remove-Item Env:\POS_DATA_DIR
```

With that set, `get_settings()` points at a fresh database and your real
terminal is untouched. Unset it before doing the run for real.

**Test the TTL by re-sealing, not by editing.** Seed a user whose snapshot was
signed sixteen days ago; the repository computes the MAC, so the row is valid
and the snapshot is genuinely old:

```powershell
.venv\Scripts\python -c "from datetime import timedelta; \
from app.config import get_settings; from app.data.db import Database; \
from app.data.repositories.users import CachedUserRepository; \
from app.services.auth_service import AuthService, SessionStore; \
from app.security.snapshot_mac import SnapshotSealer, mac_key; \
from app.domain import permissions as p; from app.domain.identity import utcnow; \
s = get_settings(); db = Database(s.db_path); \
users = CachedUserRepository(db, sealer=SnapshotSealer(mac_key(s.store_code, s.terminal_code))); \
auth = AuthService(users=users, sessions=SessionStore(), cloud=None, store_code=s.store_code, terminal_code=s.terminal_code, settings=s); \
auth.seed_local_user(user_id='018f0000-0000-7000-8000-0000000000ff', employee_code='X001', full_name='Stale Person', store_id='018f0000-0000-7000-8000-000000000100', pin='1234', roles=frozenset({p.CASHIER}), permissions=p.permissions_for(frozenset({p.CASHIER})), signed_at=utcnow() - timedelta(days=16)); \
print('seeded X001 with a snapshot signed 16 days ago')"
```

With the cable out, sign in as **X001 / 1234**:

- [ ] Refused with **"This terminal has been offline too long. Connect to the
      internet once to sign in."**

Verified: that exact message, as a **401**. The row is intact, the seal
verified, and the date is what refused.

That is the TTL doing its job — the row is intact, the seal verified, and the
date is what refused.

## D3. The seal — edit the file and watch it stop working

*Now* edit the column, and expect the other message:

```sql
-- sqlite3 C:\ProgramData\RetailPOS\pos.sqlite3
update cached_users set snapshot_expires_at = '2030-01-01T00:00:00+00:00'
 where employee_code = 'X001';
```

- [ ] Signing in as X001 offline is refused with **"has not signed in on this
      terminal before"** (503, not 401).
- [ ] `pos.log` carries an ERROR: *"cached identity for X001 failed its seal
      and will not be used"*.

Ten years of extra access, granted with one UPDATE, and the row stopped being
readable instead. Clear up:

```sql
delete from cached_users where employee_code = 'X001';
```

---

# What this run cannot prove

Write the result beside these, because a green run gets quietly credited with
covering them and it does not:

- **"System"** in the Who column needs a row with a null `actor_id`. All 34
  rows in the project have an actor.
- **"Someone outside this store"** needs a second store. There is one.
- **`store_id is null`** — the branch 0019 widened the policy for — is
  exercised by no row in this data, catalogue edits included.
- **The online override** (`authorize-override`) is only reached when the
  terminal has a network. Part C deliberately tests the offline path, because
  that is the exit criterion. To exercise the other one, repeat C2–C3 with the
  cable **in** and a supervisor who has never signed in on this till.

All four stay proven by test only.

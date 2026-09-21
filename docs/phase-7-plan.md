# Phase 7 — RBAC completion

*Execution plan §3, weeks 16–17.*

**Exit criteria:** the permission matrix from architecture §11.1 exists as a
parametrised test that passes at all three layers — UI, FastAPI dependency,
RLS. Disconnect the network and confirm override still works; advance the
clock past the TTL and confirm it stops.

Four deliverables:

- Manager override flow: modal, Edge Function, 90-second grants
- Offline permission snapshots with signed TTL and revocation on sync
- `<PermissionGate>` audit across every privileged control
- Audit log viewer

> **Closed 21 September 2026.** Exit criteria met, all six definition-of-done
> boxes ticked. See [Closed](#closed) at the end of this document.

---

## What already exists

Checked, not assumed.

| | Where | State |
|---|---|---|
| `OVERRIDE_GRANT_TTL = 90s` | `app/domain/identity.py:20` | Exists. Three unit tests exercise the rule. |
| `Session.overrides` (key → expiry) | `app/domain/identity.py:48` | Exists. **Nothing ever writes it** — in production the dict is always empty. |
| `Session.allows()` honouring grants | `app/domain/identity.py:51` | Done. |
| `require(permission)` | `app/api/deps.py:84` | Already calls `session.allows(..., now=utcnow())`, not `in session.permissions`. **The API layer is ready for grants today.** |
| Override columns on the data | `sale_lines.overridden_by`, `.override_reason`, `cash_movements.approved_by`, `audit_log.approver_id` | Columns exist in both schemas and the sync RPCs carry them. Nothing writes them. |
| `SNAPSHOT_TTL = 14 days` | `app/domain/identity.py:17` and `authenticate-pin/index.ts:28` | Two independent constants, **no parity test** (contrast the argon2 pair, which has one). |
| Snapshot expiry enforcement | `auth_service.py:178` | One place: `_login_offline`. An already-open session is never re-checked. |
| `CachedUserRepository.revoke()` | `app/data/repositories/users.py:107` | Exists. One caller, on a **failed login**. Never called from sync. |
| `auth.revoked` broadcast | promised in `users.py:110` | **Does not exist.** The WebSocket hub (`app/api/events.py:58`) works and has no caller for it. |
| `<PermissionGate>` | `ui-src/src/core/rbac/PermissionGate.tsx` | Sound component. Three uses, all navigation. `useHasPermission` exported, zero callers. |
| Ad-hoc gating | `AdminScreen.tsx`, `StockroomScreen.tsx` | Seven more places gate with `session.permissions.includes(...)`, bypassing the component. |
| Audit log viewer | — | **Nothing.** No route, no screen, no client function. Writers and RLS are in place. |
| `pos.has_perm(p)` | `0003_rls.sql:33` | Reads `auth.jwt() -> 'app_metadata' -> 'permissions'`. **A claim, never a table.** |
| Token hook | `0004_access_token_hook.sql` | Mints the claim at login; a non-`active` employee gets an empty array. Revocation latency is the 1-hour token TTL. |
| Matrix tests | `tests/test_permissions.py:39`, `tests/test_rls.py:798` | Python layer: all 20 keys × 5 roles. RLS: a handful of tables. **UI layer: nothing.** |

Eleven of the twenty permission keys have no UI surface at all:
`sale.discount.line`, `sale.discount.unlimited`, `sale.void`, `sale.refund`,
`price.override`, `sale.review.resolve`, `cash.payout`, `shift.close`,
`report.margin`, `user.manage`, `settings.manage`.

---

## The decision that shapes this phase

**A 90-second grant lives on the terminal. RLS only ever sees the JWT.**

`pos.has_perm` reads a claim minted at login. A supervisor override writes
`Session.overrides` in this process's memory. Postgres cannot see it and never
will, because there is no way to mint a new JWT offline — which is precisely
the case the exit criteria insist must work.

So for every overridable permission, the same act passes FastAPI and then
meets Postgres at the sync boundary. Three different things happen, and the
difference is not obvious from the code:

1. **The write is local-first and pushes under a key the cashier already
   holds.** A voided or discounted line rides the outbox into `sync_push`,
   which is `security invoker`, and `sale_lines` is accepted because the
   cashier holds `sale.create`. Works — but only because the policy never
   asks *which* of the four stock/sale keys justified the row.
2. **The write is local-first and pushes under a key the cashier does not
   hold.** RLS refuses at push time. The row quarantines into the failures
   queue, minutes or hours after the customer left. **Silent, and late** —
   the worst shape of failure this project keeps producing.
3. **The write is cloud-direct** (anything on the admin path, phase 6
   decision 2). RLS refuses immediately and the screen says so. Loud, and
   therefore fine.

Decide and write down, before building the modal, **which keys are
overridable** and what each one's sync path does. The honest options are: keep
overrides to permissions whose cloud write is already accepted under the
cashier's own claim; or carry `approver_id` into the push and widen the policy
to accept a row that names an approver; or refuse to override anything that
cannot be pushed. Do not discover this per-key during slice 3.

~~A related finding worth fixing while you are in there: `stock_ledger_insert`
(`0003_rls.sql:229`) accepts a row if the caller holds **any** of
`sale.create`, `stock.receive`, `stock.count`, `stock.adjust`. It never
compares the row's `reason` to the key. A cashier can already push an
adjustment today.~~ **Half wrong, and corrected by the matrix** — 0012 had
already tied the `sale.create` disjunct to `ref_type = 'sale'`, so a cashier
is refused. The hole moved rather than closing; see
[What slice 1 found](#corrected). Struck here rather than deleted, because a
finding this document got wrong is worth leaving visible.

---

## Slice 1 — Write the exit criterion first

Phase 6's own lesson, recorded in its plan: *reconciliation is the exit
criterion, so write that query first.* Here the exit criterion **is** a test.

Build `tests/test_permission_matrix.py` (or extend `test_permissions.py`) as
one parametrised table — 20 keys × 5 roles — asserting at three layers:

- **Python:** `permissions_for(role)` — already passing.
- **FastAPI:** for each key that has a route, the route 403s for a role
  without it and admits one with it. Reuse the `client` fixture and the
  `seeded_*` users.
- **RLS:** for each key that has a policy, `rowcount`/exception under a JWT
  carrying that role's claims. Extend the existing `tests/test_rls.py`
  helpers rather than inventing new ones.
- **UI:** a Vitest matrix over `PermissionGate` and the screens, asserting a
  control is absent for a role without the key.

It will fail immediately and in specific places. That failure list is the
work order for slices 2–5, and it is more trustworthy than this document.

**Prove it:** the test names a key and a layer in every failure message. A
failure that says "cashier can reach /inventory/adjustments" is worth more
than a red dot.

---

## Slice 2 — The `<PermissionGate>` audit

Cheapest slice, and it makes slice 1's UI layer meaningful.

1. Replace the seven ad-hoc `session.permissions.includes(...)` checks in
   `AdminScreen.tsx` and `StockroomScreen.tsx` with `PermissionGate` or
   `useHasPermission`, so an audit by grep finds every gated control.
2. Gate the controls the audit found rendering unconditionally — the queue's
   "Existing product" and "Dismiss", the UPI attest button, and the sync
   "Send now".
3. **Fix `POST /sync/push`** (`app/api/sync.py:56`), which has no `require()`
   at all while its two siblings require `report.sales.store`. Any cashier
   can force a drain. Decide the key deliberately — a cashier plugging the
   network back in and wanting the backlog to empty is a reasonable act — but
   decide it rather than leaving it unstated.

**Prove it:** slice 1's UI matrix goes green, and `grep -c PermissionGate`
matches the number of privileged controls on each screen.

---

## Slice 3 — The override flow

The biggest slice. Order matters, because the offline path is the one the
exit criteria actually test and the one most likely to be left until last.

1. **`SessionStore.grant(permission, until)`.** `Session` is frozen
   (`identity.py:35`), so this is `dataclasses.replace` into the store under
   its existing lock. The docstring at `auth_service.py:51` already commits
   to the shape: *a supervisor authorising a void never signs the cashier
   out.*
2. **Supervisor PIN verification that does not replace the session.**
   `pins.verify_pin` has exactly one caller today — `_login_offline`, which
   swaps the session. Phase 7 needs a second path that verifies a *second*
   person against `cached_users` and returns a grant. Offline, this only
   works if that supervisor has signed in on this terminal before. Say so in
   the UI rather than failing with "wrong PIN".
3. **`POST /overrides/authorize`** (architecture §199): supervisor code +
   PIN + the permission key → a grant, or 403. Online it calls the Edge
   Function; offline it verifies locally. Same response shape either way.
4. **Edge Function `authorize-override`** (architecture §528), alongside
   `authenticate-pin`. It verifies against the cloud so a supervisor who has
   never used this terminal can still authorise when there is internet.
5. **The audit row is the point.** `audit_log.approver_id` exists and nothing
   has ever written it. An override that leaves no trail is worse than no
   override, because it launders an escalation into ordinary work. Write the
   row where the grant is minted, not where it is spent — a grant that is
   never used is still a fact about the shop.
6. **The modal.** Third dialog pattern in `RegisterScreen.tsx`; follow
   `TenderDialog`. It must name the permission in words a cashier
   understands, and it must not be dismissible into a state where the caller
   believes it succeeded.

**Prove it:** with the network cable out, a cashier takes a void, a
supervisor authorises it at the modal, the line voids, and `audit_log` has a
row naming both people. Then wait 91 seconds and confirm the next void asks
again.

---

## Slice 4 — Sign the snapshot, and revoke on sync

Three separate holes; do them in this order.

1. **Re-check the TTL on an open session.** Today the expiry is tested once,
   at `_login_offline`. A till signed in on day 13 and left running trades
   for ever. `require()` is the natural place, and it already takes `now`.
2. **Sign it.** `snapshot_expires_at` comes verbatim from the response body
   (`supabase_auth.py:73`), is stored in an unencrypted SQLite file with no
   MAC, and the JWT it came from is decoded with
   `verify_signature=False` (`supabase_auth.py:164`). Editing one column in
   the local database extends a dismissed employee's access indefinitely. Two
   fixes, both worth doing: verify the JWT signature properly, and store a MAC
   over the cached row keyed from the OS keychain (`app/security/keychain.py`
   already exists).
3. **Revocation on sync.** `0004_access_token_hook.sql:32` asserts that "the
   terminal's next sync purges the offline snapshot"; nothing implements it.
   The puller's `ENTITIES` (`app/sync/puller.py:69`) does not include
   `employees`, so a deactivation never reaches the terminal. Add the pull,
   call the `revoke()` that already exists, and finally emit the
   `auth.revoked` broadcast that `users.py:110` has been promising — the
   WebSocket hub is built and waiting.
4. **Add the parity test** the two `14`s have never had.

**Prove it:** deactivate an employee in Supabase, let the terminal sync, and
watch the open till drop them. Then set a snapshot back 15 days and confirm
the offline login refuses with the sentence about being offline too long.

---

## Slice 5 — The audit log viewer

Read-only, gated on `user.manage`, and the first screen in this product whose
whole job is to answer "who did that?".

- Cloud-direct like the rest of admin (decision 2), so it reaches PostgREST
  under the user's own token and `audit_log_select` does the work.
- Filters that match how the question actually arrives: a date range, an
  action, and a product or sale. "Who changed this price?" is the question;
  make that two clicks.
- It should show the phase 6 seam honestly: rows written before 0021 carry a
  null `resolution`, and rows from seeding carry a null `actor_id`. Do not
  render either as a blank cell that looks like a bug.

**Prove it:** find yesterday's `scan.dismissed` row and the person who wrote
it, without writing SQL.

---

## What to watch

**The grant/RLS asymmetry above is the whole phase.** Everything else here is
ordinary work; that one is a design decision with a silent failure mode
waiting behind it.

**An override is an escalation, so the audit row is not optional.** Phase 6
shipped the trigger that made the catalogue's trail honest. This phase adds
the one path in the system where a person deliberately exceeds their
permissions. If that path can happen without a row naming both people, the
audit log becomes a document that is true except where it matters.

**Time-boxed, not use-boxed.** 90 seconds is a window, not a token — a
supervisor who authorises one void authorises every void in that window.
That is the architecture's choice and it is defensible at a counter, but it
should be a sentence someone wrote down rather than an accident of the data
structure.

**The three-layer matrix will find things that are not phase 7's fault.**
`stock_ledger_insert`'s four-key OR is the one already visible. Log them,
fix the cheap ones, and do not let the test's red turn into background noise.

**Carry phase 6's lesson.** Four defects in that phase's last week, none
found by a test suite, all the same shape: the system reported success for
something that had not happened. Phase 7's version of that bug is an override
that appears to work and quarantines at 2am, or a revoked employee who keeps
trading because nothing re-checked. Ask of every control here: *if this
silently did nothing, how would anyone find out?*

---

## Before starting

Nothing is blocking. Phase 6 is closed, the packaged build works, and
`require()` already honours grants — which means slice 3 has less plumbing
ahead of it than the deliverable list suggests.

Start at slice 1. The failing matrix is the map.

---

## What slice 1 found

*17 September 2026. Written by the matrix, not by reading the code.*

The matrix now exists at all three layers and runs green, with two `xfail`
markers holding the two open findings. Green was not the expected outcome —
the plan predicted "it will fail immediately and in specific places" — and the
reason it is green is worth stating: the FastAPI layer already enforces §11.1
exactly, for all eleven keys that have a route. `require()` was applied
consistently as each router was built, so there was nothing to fix there.

Where the document and the code disagreed, the code won three times out of
four.

### Confirmed

**`POST /sync/push` has no `require()`.** Its two siblings in the same router
both require `report.sales.store`. Held as
`test_sync_push_is_gated`, `xfail(strict=True)` — slice 2 decides the key and
the marker comes off with the fix.

### Corrected

**The `stock_ledger_insert` finding is half out of date.** The plan says "a
cashier can already push an adjustment today", citing `0003_rls.sql:229`.
`0012_stock_ledger_by_document.sql` has since tied the `sale.create` disjunct
to `ref_type = 'sale'`, so a cashier is refused — asserted now by
`test_a_cashier_cannot_write_an_adjustment`, because a closed hole is worth a
regression test.

The hole moved rather than closing. The other three disjuncts are still
unconditional, so **`stock.receive` alone admits a row of any `ref_type`** —
the inventory role can write a `reason = 'adjustment'` row that §11.1 reserves
for manager and admin. `0012`'s own comment says "a receipt, a count, an
adjustment: a stock permission each"; the SQL under it is a flat OR of the
three. The comment describes the policy that was intended and the code does
not implement it. Held as
`test_receiving_stock_is_not_permission_to_adjust_it`, `xfail(strict=True)`.

**Five ad-hoc gating checks, not seven** — four in `AdminScreen.tsx`, one in
`StockroomScreen.tsx`. Two of the five are `.filter()` calls over a tab list,
which is a reasonable shape and still invisible to an audit by grep. The count
is in `AD_HOC_PERMISSION_CHECKS` and may only go down.

**Nine keys have no API surface**, against the plan's eleven with no UI
surface. The two that differ — `sale.review.resolve` and `report.margin` —
have routes and no screen. `NO_API_SURFACE` lists the nine.

### Found on the way, and not phase 7's

**`POST /inventory/adjustments` accepts a `product_id` that does not exist**,
and writes a ledger row for it. `InventoryService.adjust` never checks, and
local `stock_ledger.product_id` carries no foreign key — `cash_movements`
right beside it does. `POST /inventory/counts` behaves the same way. The cloud
schema *does* have the FK, so such a row would push, be refused, and quarantine
in the failures queue hours later: the silent-and-late shape the plan warns
about. Not fixed here — it is a data-integrity decision, not a permission one,
and it deserves its own commit.

### What the matrix is made of

| Layer | Where | Cases |
|---|---|---|
| Python | `tests/test_permission_matrix.py` | 100 — 20 keys × 5 roles |
| FastAPI | same file | 55 — 11 keys with routes × 5 roles |
| RLS | `tests/test_rls.py` | 9 key-gated tables × 5 roles, discovered from `pg_policies` |
| UI | `ui-src/src/core/rbac/permissionMatrix.test.tsx` | 100 — 20 keys × 5 roles |

Three things are discovered rather than listed, because a hand-written copy of
any of them is a copy that falls behind: the route→permission map is read off
the FastAPI dependency graph, the RLS tables off `pg_policies`, and the UI's
`ROLE_PERMISSIONS` is generated from `app/domain/permissions.py` by
`scripts/gen_ts_types.py`. Each has a guard asserting the discovery still
finds something, because every one of them passes vacuously when it stops
matching — which is this project's recurring bug wearing a test's clothes.

---

## What slice 2 settled

*17 September 2026.*

### `POST /sync/push` requires nothing, and that is the decision

The plan asked for this to be decided rather than inherited. It is decided:
**no permission**, recorded in the endpoint's own docstring, in the button's
comment, and in `test_sync_push_is_deliberately_ungated`.

Draining the queue early is the same act as waiting ninety seconds for the
next cycle. Every row in the outbox was written under a permission that was
checked when it was written; the push runs under the terminal's own
credentials; RLS refuses each row on its own merits either way. There is
nothing to escalate to, so a gate would buy nothing.

It would cost something, though. The person who has just plugged the cable
back in is usually the cashier, and the two keys that were available both
exclude someone with a real claim on the button: `sale.create` leaves out the
inventory role, whose received delivery is sitting in the same queue, and
`report.sales.store` leaves out the cashier the docstring was written for.

The two neighbours stay gated on `report.sales.store` and should. They show
what was quarantined and put it back in the queue — reading refused sales and
deciding a refusal no longer applies are judgements about other people's work.
This one only changes *when*.

The slice 1 `xfail` is gone, replaced by a test that asserts the decision in
three directions: push is ungated, both neighbours still require the key, and
**an unauthenticated caller still gets 401**.

That third one matters more than it looks. *Ungated* is not *unauthenticated*.
`push_now` declares `session: CurrentSession` and never mentions it again — the
argument exists only to make FastAPI resolve the dependency, and it sits
directly under a docstring that now says no permission is required. That is an
unused parameter with a written invitation to delete it, and without the
assertion every test in the suite would have stayed green while the outbox
drain became reachable by anything that could reach the port. Checked by
deleting the parameter: the call returns 503 instead of 401, because it sails
past the guard and reaches the engine.

Removing it from `UNTESTED` was not planned. Asserting that push still needs a
session meant calling it over HTTP, which is the one thing
`tests/test_route_coverage.py` had been asking for since phase 6 — so the
route-coverage debt went from ten to nine as a side effect. A route whose
rules nobody could state turned out to be a route nobody had called, which is
probably not a coincidence.

### The audit list is empty

All five ad-hoc `session.permissions.includes(...)` checks are now
`useHasPermission`, so every gated control in the UI is findable by grepping
for the component or the hook. `AD_HOC_PERMISSION_CHECKS` is `{}` and a file
goes back on it only with a reason.

Emptying it cost the audit its own proof. While the list had entries, "did the
search find them?" was self-evident; now both audit tests assert that a search
finds nothing, which is also what a search that has quietly stopped working
reports. So the pattern is now tested against a sample rather than against the
tree — rename `SessionResponse.permissions` and the test fails instead of the
audit going green on an empty set.

**The general rule, worth carrying past phase 7: any check whose passing state
is "found nothing" needs a positive control.** The check and its own failure
look identical from the outside, so something has to prove the instrument
still works. This project has paid for the lesson four times already — the
route-coverage walker that checked 1 of 46 operations and called the other 45
gone, audit tests counting rows they had not created, a `.gitignore` rule in
UTF-16 matching nothing, and a six-day-old UI bundle serving a screen that no
longer existed in source. Each was a search that found nothing and reported
success. Slice 1's three discovery guards and this one are the same defence in
four places.

### Three controls gated, and one of them is a no-op today

- **The UPI "Received" button** now needs `payment.attest`, the key
  `POST /register/payments/{id}/confirm` checks. Whoever lacks it keeps
  "Can't tell", which was already the path that holds the sale for a
  supervisor — so the dialog stays usable rather than becoming a dead end.
- **The queue's "Existing product" and "Dismiss"** now need `product.edit`.
  **This changes nothing today**, because the Unknown scans tab already
  requires the same key, and it is worth being plain about that rather than
  counting it as a fix. The tab's permission is about navigation and these
  are about the act; if the tab is ever opened up so a cashier can see what
  is pending, the buttons that close an entry must not come with it.

Because that gate is unreachable today it cannot be proven by behaviour, so
its test runs the other way: with `product.edit`, both buttons are present.
A redundant gate's only available failure is hiding a control from someone
entitled to it, and that is the half that is testable.

The UPI gate *is* reachable, so it was checked by removing it and confirming
the test failed. A test that asserts a button is absent passes just as well
when the dialog never opened, and this project has shipped that mistake in
other forms.

---

## Slice 3 — the decision, made before the modal

*17 September 2026. The plan's instruction: decide and write down which keys
are overridable and what each one's sync path does. Do not discover this
per-key during slice 3.*

### The rule

**A permission is overridable only if the write it authorises is already
accepted under the cashier's own claim.**

Stated in `app/domain/permissions.py` as `OVERRIDABLE`, and justified against
the real policies in `tests/test_rls.py` — each key is a real write, run under
a cashier's claim, which does not carry it. Declaring the set is unavoidable,
because a terminal at a counter cannot read `pg_policies`; taking the
declaration on trust is not.

### What the policies actually say

The plan set out three scenarios and worried about the second — a write that
pushes under a key the cashier does not hold, quarantining silently hours
later. Reading the policies, scenario 2 does not arise for the sale path at
all:

| Override | Lands in | Policy asks for | Under a 90s grant |
|---|---|---|---|
| `sale.discount.line` | `sale_lines` | `sale.create`, via the parent sale | pushes |
| `sale.discount.unlimited` | `sale_lines` | `sale.create` | pushes |
| `price.override` | `sale_lines` | `sale.create` | pushes |
| `sale.void` | `sales` | `sale.create` + `cashier_id = auth.uid()` | pushes |
| `cash.payout` | `cash_movements` | **`cash.payout`** + `actor_id = auth.uid()` | **refused** |

`sale_lines_insert` never asks which key justified the row — it asks whether
the caller may take a sale. So the grant never has to reach Postgres, which is
exactly what makes the offline case work: the supervisor's authority is spent
on the terminal, and what arrives in the cloud is an ordinary line a cashier
was entitled to write, carrying `overridden_by` and `override_reason` as facts
rather than as credentials.

`cash.payout` is the counter-example, and the reason this is a rule rather
than a list. Its policy names the key, so a granted payout would be refused at
push time — the silent-and-late failure this phase is most afraid of. It is
not overridable. `shift.close` and `sale.refund` have no write path yet; when
they get one, the test decides rather than a judgement call.

### Why not the other two options

**Widening RLS to accept a row naming an approver** would make every key
overridable uniformly, at the cost of a migration and of widening the only
layer that is actually security. `approver_id` is written by the terminal into
an unencrypted SQLite file with no MAC — slice 4 exists because that file is
editable — so a policy trusting it would convert a local file edit into real
cloud access. The asymmetry the plan calls "the whole phase" is better left
in place than papered over.

**An explicit list of four** is what `OVERRIDABLE` looks like today, and would
read the same. The difference is that nothing would tie it to the policies, so
a future migration that made one of the four ask for its own key would move
the failure from the test suite to a counter at 2am. This project has twice
watched a hand-maintained list fall behind.

### No schema change

`sale_lines.overridden_by`, `sale_lines.override_reason` and
`audit_log.approver_id` already exist in both schemas, and `sync_push` already
carries all three. Slice 3 writes them; it does not add them.

### What this settles, and what it does not

Settled: which keys the modal may offer, and that a grant never needs to reach
Postgres. Still open, in slice 3's own order — `SessionStore.grant()`,
supervisor PIN verification that does not replace the session,
`POST /overrides/authorize`, the `authorize-override` Edge Function, the audit
row at the point the grant is minted, and the modal.

The audit row remains the part worth not getting wrong. An override that
leaves no trail launders an escalation into ordinary work, and the whole
reason the sale path is safe to override is that what reaches the cloud looks
ordinary.

---

## The house rule, found in slice 3 and worth keeping

*17 September 2026.*

Three bugs in one commit, and each answer was already in the repository —
forty lines up in the file being copied from, or two migrations back. The
diagnosis is not distance between layers. It is **distance between the file
being edited and the file being referenced**: the authoring context drifts
from the reference context, and everything each one says stays locally true.

- `audit_log.entity_id` is a `uuid`. `0010` exists because that exact column
  was once missing that exact cast.
- `on conflict` cannot be used on `audit_log` by the cashier writing the row,
  because detecting a conflict is a read and `audit_log_select` wants
  `user.manage`. The sale branch handles it with `exception when
  unique_violation` and says why — "same meaning as do nothing, reached
  without a read" — forty lines above where the new branch went.
- The writer cannot read back what it wrote, for the same reason.

**The rule: when the new thing is an extension of an existing thing, do not
copy it into a new buffer. Generate it from the original and assert what
carried over.**

`0022` was derived from `0014` by script and asserts all four original
branches survived. `sync_push` has now been rewritten four times — 0008, 0010,
0014, 0022 — and every rewrite carries the whole function, so every rewrite is
a chance to silently drop a branch. Retyping it is the risk; the script is the
countermeasure, and `scripts/` is where the next one belongs if there is a
fifth.

The same rule covers whitelists. `SUPPORTED_ENTITIES` was declared and then
cross-checked against the builder table on every call, and a test reads the
migration to confirm the other side accepts everything this side can queue.
What made the old list look complete was that nothing ever asked it whether it
was — the same "found nothing needs a positive control" from slice 2, pointed
at a whitelist instead of a search.

### Two questions answered before the Edge Function

**A 422 echoed the submitted PIN.** FastAPI's default handler puts the
offending value in `input`, so a PIN one character short came back in the
response body of `/auth/login`. Nothing read it and nothing logged it — the UI
only looks at `detail` when it is a string, and uvicorn runs `access_log=False`
— which is the absence of a mistake rather than a defence against one, with a
diagnostics screen that reads log files due in phase 9. A handler now drops
`input` for `SENSITIVE_FIELDS` and keeps the location and the reason, so a 422
still says which field was wrong. Checked by deleting the handler: the PIN test
goes red and the positive control, which asserts a *harmless* field is still
echoed, stays green.

**The Edge Function directory had one duplicated constant with no parity
test.** `SNAPSHOT_TTL_DAYS = 14` in `authenticate-pin`, independent of
`SNAPSHOT_TTL` in `identity.py`; the argon2 parameters beside it have one and
the file says so. `tests/test_edge_function_parity.py` now scans *every*
`supabase/functions/*/index.ts` rather than a named file, so
`authorize-override` is covered the day it lands rather than the day somebody
remembers. Checked twice: drifting the value to 30 fails, and renaming the
constant — which would otherwise silence the check — fails too.

### The offline PIN had no throttle, and the bypass was already live

`authenticate-pin` has rate-limited the cloud path since it was written. The
terminal had no counterpart: every offline PIN check went straight to argon2
and back, as often as anybody liked. The measurement is the argument —
argon2id at the shipped parameters verifies in **22.8 ms natively**, so a
four-digit space is four minutes with the cable out. The comfortable "hours to
guess" intuition comes from the Edge Function's WebAssembly, which takes about
1.8 s for the same work and is on the other side of the wire.

`app/domain/lockout.py` escalates rather than rolling: five failures cost
fifteen minutes, then thirty, then an hour, capped. A rolling window resets
itself, which is exactly what an attacker with all night wants. Per approver,
never per terminal — a supervisor's bad morning may not stop the till selling
(§1.1).

**The gate runs before the transport choice, and that is where the real bug
was.** `login()` went straight to the cloud whenever one was configured and
never consulted the local lock, so a locked-out account could sign in by
having a network. A lockout a person can clear by changing the network
conditions is not a lockout, and the attacker is the one holding the cable.
The order is now: lock, then transport. Being online changes *who verifies the
PIN*, not *whether this approver may be asked*. A correct PIN accepted by the
server still clears the local count, because the server saying yes means
somebody was getting it wrong rather than guessing.

The test for this asserts *ordering* rather than outcome — both orderings
refuse a locked account, and differ only in what they touched on the way — so
the cloud stub raises if it is consulted at all.

### Queue the lockout, not the attempts

Refusals are generated by whoever is typing; lockouts are generated by the
throttle. So the lockout is the bounded event, and the one that leaves the
terminal: at most one row per lockout, and the next one costs the attacker
fifteen minutes before it costs the outbox a row. Individual refusals stay
local, where the sequence that makes them evidence lives.

It has to be pushed, not merely recorded, because slice 5's viewer reads the
cloud. A supervisor's PIN locking out at 11pm is exactly what the screen
built to answer "who did that?" exists to show.

`0023_push_audit_rows.sql` widens the branch to `when 'override', 'audit'
then` rather than adding a copy. The names stay apart on the wire because
`override` was already sitting in terminals' outboxes, and renaming a value
inside somebody's queue is how a queue stops draining. Generated from 0022 by
script — fifth rewrite of `sync_push`, second not retyped.

The cross-check found its own bug on the way: the parser matched `when 'x'
then` and a widened branch is `when 'x', 'y' then`, so it reported `audit` as
unaccepted when it was not. Wrong in the safe direction, and only visible
because the check existed.

### The cloud's throttle is the weaker half, which is the reverse of the usual

`attempts` in `authenticate-pin` is a `Map` in the memory of one Deno isolate.
The platform cold-starts, recycles and scales these horizontally, so ten per
five minutes is a **per-instance nominal figure, not a property of the
system**: spread the attempts across instances, or just arrive after a
recycle, and the counter is fresh.

It has never mattered much there, because that path needs a reachable network
and leaves server-side traces. It matters now for two reasons. The terminal's
counter is persisted and genuinely binding, so
`test_the_local_throttle_is_at_least_as_strict_as_the_cloud` compares a real
counter against a hopeful one — still worth asserting, because it holds in the
direction that protects the shop, but it is a floor on the cloud's behaviour
rather than a description of it. And `authorize-override` will be written
beside that `Map`, where the cheapest thing to do is copy it.

So the caveat is a comment in the Edge Function rather than a third copy of
the pattern, and a test asserts that any function keeping an in-memory attempt
counter says out loud that it is per instance. Its positive control is
`authenticate-pin`, which declares one — the check cannot pass by matching
nothing.

---

## Slice 3 — `POST /overrides/authorize`

The only route in the application whose purpose is to hand out a permission.

**It carries no `require()`, by decision.** A gate would be circular: the
cashier who needs an override is by definition the one without the key, so any
permission strict enough to matter would refuse exactly the people the
endpoint exists for. It is in `UNGATED` beside `POST /sync/push`, with the
reason written down — and ungated still does not mean unauthenticated. It
requires a *session*, because a grant is lent to somebody and there has to be
a somebody.

That dependency looks redundant, since the service raises its own
`NotSignedIn` and the router answers both with 401. It is not: without it, a
request naming a non-lendable key gets 400 before anyone checks for a session,
which tells an unauthenticated caller which keys exist. The test asserts the
401 for that request, and removing the dependency turns it red.

**The refusals are deliberately distinguishable**, because the person reading
them is a supervisor at a till who needs to know what to do next:

| Status | Condition | What the screen says to do |
| --- | --- | --- |
| 401 | wrong PIN | retype |
| 401 | nobody signed in | sign in first |
| 403 | authorising yourself | fetch somebody else |
| 403 | approver lacks the key | fetch somebody more senior |
| 409 | the session already holds it | nothing — go ahead |
| 400 | the key is not in `OVERRIDABLE` | it needs the role, not a PIN |
| 503 | approver unknown to this terminal | sign in once here, or reconnect |
| 423 | the approver is locked out | wait, and the row says until when |

That 503 discloses whether an employee code is cached on this terminal. It is
the same disclosure `/auth/login` already makes, for the same reason, and it
says nothing about anybody's credential. 423 rather than 429 because this is a
fact about the account, not about the caller: the supervisor who has typed a
PIN twice is not the person who spent the afternoon guessing at it.

A wrong PIN costs the same 0.4 s delay as a failed login, so a stopwatch
cannot tell a wrong PIN from an unknown-but-cached code. A lockout does not
get the delay — it is meant to be discoverable, because waiting is the only
thing that will help.

### The modal owns the whole act

The design question the endpoint had to settle: what happens when a grant
expires between minting and spending. Ninety seconds is generous for a void
and thin for a supervisor who gets interrupted mid-authorisation. If the modal
mints a grant and hands control back, the cashier can land on a 403 with no
visible cause — the permission they just watched somebody grant is gone, and
nothing on screen explains why.

So the modal performs the action rather than enabling it. Authorising *is* the
void; the ninety seconds covers the round trip rather than the cashier's next
decision. The grant stays time-boxed in the architecture and use-boxed in
practice, and nothing about `Session.overrides` changes.

The API already matches that shape without being made to. `GET /auth/session`
reports `permissions`, not grants, so a live override is invisible to the UI
except in the response that minted it — which is precisely the window in which
the modal is meant to act. `expires_in_seconds` is on the response for the
same reason: a countdown computed from two clocks can start at a negative
number, so the service that owns the window says how much of it is left.

### What is still open in slice 3

The Edge Function and the modal. The endpoint is offline-only today, which
means an approver who has never signed in on this terminal cannot authorise —
a real limitation of a real shop, answered with a 503 that says what to do
rather than "wrong PIN".

---

## Slice 3 — `authorize-override`, and what it deliberately is not

The Edge Function answers one question — *is this PIN right, and does this
person hold this key in this store?* — and returns that and nothing else. The
grant is minted on the terminal, into the session the cashier already has.

It exists for the supervisor who has never signed in on a particular till.
Offline, the hash being checked is the one this terminal remembers, so that
person cannot authorise here; the 503 says so in a sentence they can act on.

### The half that must never be copied

`authenticate-pin` sits in the next directory and ends in about forty lines of
`generateLink` + `verifyOtp` that mint a real Supabase session. Copying that
file and deleting the wrong half would hand the till a supervisor's session
every time a cashier voided a line — and it would look like it worked, because
the grant would be minted too. The property has a name from the commit that
built the offline path: *verify a second person without becoming them*.

`test_only_the_login_function_mints_a_session` asserts it against the source.
`SESSION_MINTING` is a set rather than an equality, because the interesting
failure is a second entry appearing. Its positive control is the inclusion of
`authenticate-pin`: the markers must still match the function that really does
mint a session, so a typo in the pattern fails rather than passing everything.

Its first run failed — on `authorize-override`, for a comment saying in as
many words that it must never call `generateLink`. A text search cannot tell a
warning from the thing it warns about, and a check that punishes the
explanation teaches people to delete the explanation. It now matches code with
whole-line comments stripped.

`CloudApprover` sits directly above `CloudSession` in `supabase_auth.py` for
the same reason. That one carries an access token, a refresh token and a PIN
hash; this one carries none of the three. The difference between the two
dataclasses is the entire security property.

### The permission is read, never taken on trust

The body names a permission because the terminal has to say what is being
asked for. Whether the approver *holds* it is decided in the function, from
`role_permissions`, exactly as the offline path decides it from the cached
snapshot. Trusting the client's key — or returning the whole permission list
for the terminal to check — would make the online path looser than the
offline one.

It buys something the cache cannot: freshness. A supervisor demoted this
morning is refused online even though the cached snapshot still trusts them
for the rest of its fortnight.

Two things are deliberately *not* duplicated into Deno:

* **`OVERRIDABLE`.** Whether a key is lendable at all is checked on the
  terminal before anybody is asked for a PIN, and again in `SessionStore.grant`
  where the grant is minted. A copy in the function would be a fourth
  duplicated constant with nothing holding it in step, guarding a decision
  already made twice on the only machine that can act on it.
* **`OVERRIDE_GRANT_TTL`.** The terminal owns the window because the terminal
  mints the grant. A second authority on the same number is how two clocks
  start disagreeing. The entry stays in `DUPLICATED` as a trap: if anybody
  ever does declare it there, it has to match.

No `pin_hash` comes back either, so authorising does not cache the approver.
An online override works for somebody who has never touched this till; an
offline one still does not. Those stay different acts.

### The mirror image of the transport bug

The earlier finding was that the lock was consulted inside the offline branch,
so being online *cleared* it. Wiring the online path surfaced the half that
survived that fix: with a network up, every wrong PIN went to the Edge
Function, came back 401, and **the local counter was never touched**. An
attacker at the till could guess all night without ever accumulating towards a
lockout, simply by leaving the cable in. Being online was not just failing to
clear the lock — it was never reaching one.

The cloud's own limiter does not cover for that, and this is where the
per-instance caveat stops being a footnote: a `Map` in one Deno isolate,
nominal, reset by a cold start. Online was the cheaper way to guess.

A PIN the *server* rejected now counts against the local throttle, on both
paths. The counter is a fact about attempts arriving at this terminal, whoever
adjudicated them. One boundary, stated rather than hidden: it can only count
against somebody this till has cached — an employee code it has never seen has
no row to increment, and no cached hash to guess at either, so what is
unthrottled there is the cloud's rate limit rather than anything on this
machine.

### Asked twice, because one question could not cover both cases

`CannotAuthoriseSelf` is now checked in two places, and the second one exists
because the first is unprovable on its own.

Before a PIN is spent, all the terminal can compare is the code — the id
comparison needs a cached identity, and the online path may not have one. That
is enough for the ordinary attempt and it costs nothing. But a till whose
cached row for the cashier has been revoked mid-shift, which `_revoke_cached`
does when the server reports an account disabled, has nothing to compare ids
against. So the identity the verifier actually returned is compared again
after verification, where it cannot be fooled by the state of the cache.

Isolating the first check needed the lesson from the endpoint's 401: both
refusals raise the same exception, so the test asserts the *cost* instead —
with the cashier's cached row revoked, a self-authorisation must not reach the
Edge Function, because an attempt that does is a free PIN oracle for whoever
is holding the till.

### Two layers, one status

Generalised from the 401: **when two layers answer with the same status for
different reasons, a test that asserts the status cannot tell you which layer
answered.** The way out is to find a request the inner layer rejects earlier
and differently — or, failing that, to assert on what the request touched
rather than what it returned.

That matters for the whole `require()` family, because the matrix asserts
"403 or not 403" for every gated operation and concludes something about a
layer from it. Audited: `app/api/deps.py` is the only file in `app/api` that
produces a 403, and the admin router had already chosen 422 for "RLS refused
it", with a comment saying exactly why. So the matrix's 403 means what it says
today.

`test_require_is_the_only_thing_that_forbids` keeps it that way. `deps.py` is
its positive control; `NON_REQUIRE_403` lists the exceptions with reasons, and
`overrides.py` is the only entry — an ungated route whose 403s are about the
approver rather than the caller.

### The modal reads `status`, not just the message

Decided before the modal is written, because the shortest path to a working
one is the wrong one. `useCloudCall` collapses everything that is not
`isUnavailable` into a single `error` string. The *sentences* would survive
that, since `ApiError.message` is the server's `detail` and those are written
for the counter — but the *behaviour* would not, and three of the eight
outcomes need behaviour rather than wording:

* **409** — the session already holds the key. Nothing is wrong: the modal
  should close and let the act proceed, not show an error about it.
* **423** — locked until a stated time. Retrying now cannot work, so the
  submit button should be disabled until then rather than inviting another
  attempt.
* **503** — an approver this till has never seen, or no network. The remedy is
  somebody else, or a cable; not a retry with the same PIN.

So the modal reads `ApiError.status` and maps it to an outcome, rather than
rendering a message and hoping. Written down now; the mapping itself lands
with the modal that calls it, because a helper with no caller is precisely the
shape of dead code this repository has a check for.

---

## Slice 3 — the modal, and the act it needed first

The modal had nothing to authorise. All four keys in `OVERRIDABLE` were in
`NO_API_SURFACE`, so a dialog that performs the act it authorises would have
been a dialog with no act — and `CartService.apply_discount` had been sitting
there since the pricing work with no route and no caller, for a reason worth
stating rather than fixing quietly: discounting needs a permission nobody at
this screen holds, so the route would have been unreachable for every role
that uses it. **The override is what makes it reachable.**

So `POST /register/carts/{cart_id}/lines/{line_no}/discount` lands with the
modal, gated on `sale.discount.line`, and `NO_API_SURFACE` shrinks by one for
the first time. It is also what makes slice 3's acceptance criterion testable
end to end rather than in pieces: refused, authorised, performed, and refused
again once the window closes.

One thing found while testing it and left alone: an amount larger than the
line clamps to zero rather than going negative, because `price_line` caps each
discount at what is left to discount. That is the boundary keeping a discount
from becoming a refund — `sale.refund` is deliberately not overridable, and a
negative line total would route straight round that decision. Worth flagging
separately: a mistyped amount therefore gives the item away silently, which is
a product question (refuse, or confirm) rather than a fault in the clamp.

### A scanner is a keyboard, and this is the first dialog where that is wrong

`RegisterScreen` captures scans globally, deliberately and with a comment
saying so: a cashier scanning the next item while the tender dialog is open
should get it in the basket. That is right for all three dialogs that existed.

It is wrong for this one, in two directions at once.

**Into the PIN field.** A wedge scanner types digits and appends Enter. With
the PIN box focused, an item scanned across the counter mid-authorisation
fills the field and submits it, spending an attempt against a supervisor's
PIN. Attempts are now persisted and escalating, so a cashier could lock their
own supervisor out by accident, with the shop open, and the audit row would
say `pin.locked` and name the supervisor.

**Into the basket.** A supervisor is authorising an act against *this* basket.
An item arriving between the asking and the doing changes what they approved.

These need two different fixes, which is the part worth writing down.
`useBarcodeCapture` already skips every `INPUT`, so while the PIN box has
focus the global hook is inert — suppressing it does nothing for the first
problem, because that problem is not the hook's doing. The field needs its own
guard, and the basket needs the hook turned off:

* `useBarcodeCapture({ enabled })` — no listener at all while the dialog is
  open, rather than an early return inside one, so the buffer cannot survive
  the dialog and fire a code assembled from somebody's PIN afterwards.
* `useScanShield` on the PIN input — the same timing rule, importing
  `MACHINE_GAP_MS` rather than retyping it. The first character of a burst is
  indistinguishable from a keystroke and lands; the second gives it away, and
  from then on the field is cleared and the trailing Enter is swallowed. The
  cashier is told what happened, because the alternative is a supervisor
  watching their PIN vanish and typing it again faster.

**The basket test was vacuous on its first run**, and the mutation check is
what said so: deleting `enabled` entirely left all ten tests green. The scan
had been typed with the approver-code box focused, where `respectFocus`
already makes the hook inert — so it was asserting a protection that was not
the one under test. Exactly the shape of the 401 that two layers could
produce. Moving focus off the text fields first makes `enabled` the only thing
that can refuse the scan, and the mutation now fails.

All four mutations are caught by the intended test: capture never suppressed,
the field unshielded, the Enter not swallowed, and a shield so aggressive that
nobody could type a PIN — that last one fails five tests, including the
human-speed positive control that exists to catch it.

### The modal, as decided last slice

It performs the act rather than enabling it, and it reads `ApiError.status`
rather than only rendering the message. Three outcomes need behaviour:

* **409** — the session already holds the key, so the dialog does the thing
  instead of complaining about a permission the person already has.
* **423** — the approver is locked out, so the button stops inviting a retry
  that cannot work, and the message carries the unlock time.
* **503** — this terminal has never seen them; another person can fix it from
  the same dialog, so the button stays live.

The discount button is offered to every cashier rather than hidden behind a
`PermissionGate`. Hiding it would leave them telling a customer to come back
when a supervisor is free; showing it is how they ask.

One bug the tests caught on the way: the discount dialog stayed open behind
the override dialog, putting two Cancel buttons on screen. A test that could
not tell them apart either is how it surfaced.

### The discount boundary — refuse strictly greater, allow exactly equal

Settled rather than left as a product question, and the reasoning is the one
that keeps recurring in this phase: **a refusal does not eliminate an act, it
relocates it.**

Refusing everything above the line total would mean a cashier who needs to
give an item away — damaged stock, a goodwill gesture, a regular who is short
— deletes the line instead. That is `sale.create` work: no supervisor, no
grant, no audit row. A recorded act performed by two people becomes an
unrecorded act performed by one.

A confirmation step was rejected outright. A confirm inside a modal that has
already confirmed is a second control costing nothing to press, sitting where
somebody has already committed to pressing things.

So the split is at the boundary:

* **equal to the line total → allowed.** "Free" is expressible, and arrives in
  the log as an authorised discount naming a cashier and a supervisor.
* **above it → 422**, with the line total in the message, because there is no
  reading of ₹99,999 off a ₹10 line that anybody meant.

`price_line`'s clamp stays exactly where it is, as domain defence-in-depth,
and is now unreachable through the API. It carries a comment saying so —
unreachable from one caller is not the same as unnecessary, and the domain
does not get to assume its callers checked. A test asserts the unreachability
rather than the claim being left to rot in a comment.

**One ordering point follows from the modal performing the act.** The amount
is validated against the line *before* a supervisor is summoned. Otherwise a
typo mints a grant, writes an audit row naming an authorisation, and only then
fails at the server — leaving a record that a supervisor approved something
that never happened, which is the exact row `AuditRepository` exists not to
produce. The server still refuses the same amount and has to; the client check
is about who gets asked, not about what is allowed.

---

## Slice 4 — what the audit found before any of it was written

The plan describes four items. Two of them cannot be done the way it says, one
is already done, and the ordering of the remaining two is backwards. Read the
section above as history; this is what is actually there.

### 1. `require()` cannot re-check the TTL — the session has no expiry on it

The plan says *"`require()` is the natural place, and it already takes `now`."*
It does take `now`, and that is misleading rather than helpful: `now` is there
for override expiry. `Session` (`identity.py:52`) carries `user_id`,
`employee_code`, `full_name`, `store_id`, `roles`, `permissions`,
`authenticated_at`, `offline` and `overrides` — and **no
`snapshot_expires_at`**. There is nothing in the session for `now` to be
compared against.

So the re-check needs one of two things first, and they are different
decisions:

* carry `snapshot_expires_at` on `Session`, copied at login; or
* read `cached_users` on every gated request, which puts a database hit on the
  hot path of a screen whose whole design is "one hand on a keyboard and a
  queue waiting".

The first is right. It also means the TTL travels with the thing it bounds.

The hole itself is real, and the description of it is right: a till signed in
offline holds that session until logout or restart, and nothing re-examines
it.

### 2. The expiry is now checked in two places, not one

The plan says *"the expiry is tested once, at `_login_offline`"*. Slice 3 added
the second: `_authorize_offline` calls `identity.is_usable(now=now)` before
letting somebody authorise an override. Both are entry checks — neither
re-examines a session already open — so the hole stands, but a reader looking
for "the one place" will find two and wonder which is stale.

### 3. Revocation on sync cannot work under the cashier's token

This is the one that changes the design rather than the wording.

The plan says: *"Add the pull, call the `revoke()` that already exists."* The
puller runs under the signed-in user's own access token
(`server.py`, `token_provider=lambda: sessions.access_token`), which is
deliberate — §11.2, a pull carries the cashier's rights and nothing more. And
`0003_rls.sql:119`:

```sql
create policy employees_select_self_or_manager on public.employees
    for select to authenticated
    using (
        user_id = auth.uid()
        or exists (... and pos.has_perm('user.manage'))
    );
```

**A cashier can read exactly one row: their own.** So adding `employees` to
`ENTITIES` would pull one row and revoke, at most, the person already signed
in — whose deactivation is the one case already handled, at the next login,
through `AccountDisabled`.

Every *other* cached identity would be untouched. That is the population that
matters: `cached_users` holds supervisors precisely so their PINs can
authorise overrides offline, and a dismissed supervisor's cached row is the
thing slice 4 exists to purge. The plan's fix protects the person it does not
need to and misses everyone it does.

It would also fail quietly. A keyset pull that returns one row looks exactly
like a keyset pull that worked, so this would ship green — the same
"found nothing needs a positive control" shape as slice 2, pointed this time
at an RLS policy.

**Three ways out, and they are not equivalent.**

* **An Edge Function**, as `authenticate-pin` and `authorize-override` already
  are. The till sends the user ids it has cached and gets back the ones that
  are no longer active. Minimal disclosure — it answers only about ids the
  terminal already holds — and it reuses the one pattern in this architecture
  entitled to privileged reads. Costs a network call the puller does not
  currently make.
* **A narrow in-store view**, `(user_id, revoked_at)` and nothing else,
  readable by anyone in the store. Cheapest to build and it rides the existing
  pull. It does disclose that *some* id in this store was revoked, to any
  signed-in cashier — no name, no code, no reason.
* **Widening `employees_select`** so colleagues can see each other's status.
  Rejected: that hands every cashier the staff roster, which is the exact
  thing `authenticate-pin`'s single rejection shape exists to withhold.

### 4. The parity test already exists

Plan item 4 — *"add the parity test the two `14`s have never had"* — was
written in slice 3. `SNAPSHOT_TTL_DAYS` is in `DUPLICATED`, the scan covers
every function directory rather than a named file, and
`test_the_snapshot_ttl_is_actually_duplicated_somewhere` is its positive
control against a rename silencing it.

### The ordering is backwards

The plan says re-check the TTL first and sign the snapshot second. A re-check
reads `snapshot_expires_at` out of an unencrypted SQLite file with no MAC, so
until signing lands, the check consults a number its attacker can edit. That
does not make the re-check worthless — it closes the "till left running for a
fortnight" case, which is carelessness rather than attack — but it is not a
control until item 2 is done, and shipping it first invites it being described
as one.

The same coupling runs the other way: if `Session` gains
`snapshot_expires_at`, the value is copied at login from a row the MAC covers,
so the two fixes land as one property rather than two features.

### Slice 4, built: the seal and the re-check, in that order

**The seal.** `app/security/snapshot_mac.py` — HMAC-SHA256 over the fields
that decide access, keyed from the OS credential store, one key per terminal.
Migration `007` adds `row_mac`; the repository seals on every write and
verifies on every read.

What it does *not* do is in the module docstring and in the test file, because
a defence described as more than it is gets relied on for more than it does:
anyone who can edit this file can also read `pin_hash` and grind it elsewhere
with no lockout. Argon2id costs real time per guess; editing a column costs
none. The seal closes the free path, which is worth doing on its own terms.

Three decisions worth keeping:

* **The lockout counters are inside the seal.** Leaving them out was cheaper —
  no re-seal on a wrong PIN — and would have left `pin_locked_until = NULL` as
  a free edit, which is the single edit most useful to whoever is guessing a
  supervisor's PIN at that terminal. An HMAC is microseconds in a path that
  has just spent 22.8ms on argon2.
* **A failed verification reads as no cached identity**, not an exception. The
  remedy is the same as for an employee this till has never seen, and the
  likely cause is a moved Windows profile rather than an attack. Logged at
  ERROR because the two causes are very different.
* **The sealer is required, not optional.** An optional key is a bypass that
  ships: every test would take the unsealed path and production would be the
  one nobody exercised. It is injected into `build_app` for the same reason
  `db` is — a test that let the real credential store decide would seal with
  one key and read with another.

The import contract caught the first attempt: `app/data` may not import
`app/security`. It was right, so the dependency is inverted — the repository
takes a `Sealer` protocol and never learns where the key came from.

**The re-check.** `Session` now carries `snapshot_expires_at`, required with
no default, and `allows()` checks it before anything else. The plan proposed
doing this in `require()`, which could not work: `require()` receives a
`Session`, and the session had no expiry on it to compare against.

It bounds a live override too. Ninety seconds against fourteen days means the
grant nearly always expires first, and nearly always is not a control: a grant
minted seconds before the snapshot lapsed must not outlive the authority it
was borrowed from.

**A mutation survived the first pass**, and it was the one that mattered:
widening the expiry inside `to_session` — the single line deciding how long an
offline session lasts — changed nothing any test could see. Every test set the
expiry by hand, so they proved the comparison worked and nothing proved the
value came from the verified row. Two tests now assert the provenance, one per
transport. Same shape as everything else this phase: a control that reads a
number, and nothing checking where the number came from.

### The house rule, third instance: a vacuous pass wearing RLS

Slice 2 found it in a walker that checked 1 of 46 operations and reported the
other 45 as absent. Slice 3 found it in whitelists nothing asked to justify
themselves. Slice 4 found it in a policy:

> Adding `employees` to the puller's `ENTITIES` would have shipped green,
> pulled one row, revoked the person whose deactivation was already handled at
> next login, and missed every cached supervisor — the entire population
> `cached_users` exists to hold. **A keyset pull returning one row is
> indistinguishable from one that worked.**

The general form: *any check whose passing state is "found nothing" needs a
positive control* — and RLS is a new way to produce "found nothing" that has
nothing to do with the code being wrong. The query is correct, the pagination
is correct, the terminal is correctly authenticated, and the answer is one row
because the database decided the caller may not see the rest.

### And the same rule, turned on the tests

The mutation that survived slice 4's first pass is the sharper version.
Widening the expiry inside `to_session` changed nothing any test could see,
because every test set the expiry by hand. The suite proved the *comparison*
and never proved the *provenance* — and provenance was the entire point of
sealing the row.

**A test that constructs its own input cannot tell you where production gets
one.** Worth saying separately from the positive-control rule, because a suite
can be full of positive controls and still have this hole: all of them build
their own fixtures.

## Slice 4, built: `check-revocations`

### What the function will not become

* **It answers about ids the caller already has, and returns only the revoked
  subset.** A status per id would make it an existence oracle: post any UUID,
  learn whether it belongs to that shop. The response is a subset of the
  input, so a caller learns which of its own rows to drop and nothing else.
* **It verifies the caller's JWT**, and refuses the anon key explicitly — that
  key is on every request this project makes and proves nothing. This is the
  first place a cashier's token is exchanged for privileged reads, so the
  exchange has to be earned. Unlike its two neighbours it keeps `--verify-jwt`
  on, because they are called before anybody has a session and this one is not.
* **It scopes to the caller's own store**, checked against `user_store_roles`
  server-side rather than trusted from the body.

### The open basket finishes

The obvious implementation of "the till drops them" is to clear the session on
`auth.revoked`. At a counter that means a customer with eleven items scanned
watches the screen drop to a login prompt because somebody in an office
processed a leaver at 11:40. Deactivation is almost never an emergency — a
leaver, or a role change — and finishing the sale in front of you is nearly
always right. "Revoke immediately" reads as the responsible choice and
produces the irresponsible outcome.

So `mark_revoked()` is a fact about the session, not a sign-out, and the
refusal lands at `POST /register/carts` — the next sale. By then the cached
row is already gone, so they cannot sign in again or authorise anything; what
they can still do is take the money for the basket already on the screen.

### Fail open, said where somebody will look

An unreachable `check-revocations` purges nothing. A shop cannot be locked out
of its own till by a network blip, and this runs on a background cycle where
nobody is watching. The consequence is that **the sealed snapshot TTL becomes
the only bound on a dismissed employee while this path is down** —
load-bearing rather than belt-and-braces — and that sentence is at the call
site in `app/sync/revocations.py` rather than only here, because a plan
document is not what anybody opens during an incident.

### Which layer refused, and a path that does not exist

The acceptance step — "deactivate, sync, watch the till drop them" — passes on
the local purge alone and says nothing about the server. The honest pair:

* **The terminal refuses immediately.** The cached row is purged.
* **The cloud refuses within the hour.** Permissions live in the JWT and the
  TTL is an hour, so a revoked cashier's existing access token satisfies RLS
  until it expires. Documented trade, not a gap.

Found while writing the second of those: **nothing in this application calls
`SupabaseAuthClient.refresh`, and `keychain.load_refresh_token` has no caller
either.** The refresh token is written at login and never read. So "the server
refuses at token refresh" describes a path that does not exist — the access
token simply expires, every push gets a 401, the pusher correctly reads that
as transient, and it retries for ever with a credential that can never work
again. Nothing is lost, because the queue keeps everything; nothing leaves
either, until somebody signs in again. Safe direction, wrong for how long, and
the next thing worth fixing.

### Fifth instance, and the first inside a PostgREST embed

`audit_log?select=...,actor:employees!fk(employee_code,full_name)` returns
`actor: null` for a row nobody performed **and** for a row whose actor works in
another store. RLS does not refuse an embed it disallows — it omits it — so
"not there" and "not yours" arrive byte-identical.

The fix is one column: select `actor_id` as well as the embed. No id means
nobody did this; an id with no readable row means somebody did and this caller
may not see who.

**The second is the one the screen exists for.** A manager investigating a
price change who reads "System" concludes a machine made the change and stops.
"Someone outside this store" names a person-shaped gap and points at the next
step — ask the owner, who can read that row. Conflating them turns a lead into
a dead end, quietly, in the one screen built to prevent that.

### The rule caught the fake this time

The mutation for it — drop `actor_id` from the select, watch the test go red —
**failed to fail.** `FakePostgrest` replied with whatever the test had lined up
regardless of the query, so the test was asserting on a field the service
would never have received.

That is *a test that constructs its own input cannot tell you where production
gets one*, written down two slices ago, arriving through the fake rather than
the test. The fake now projects its reply to the columns the `select` asked
for, which makes every assertion in that file about a payload PostgREST could
actually produce — and three mutations now fail on the intended test.

## Slice 5 — the acceptance run, and the two bugs it found before it started

### The date filter could never match a single day

The acceptance script warned that the target row — `scan.dismissed`,
2026-09-15 18:35 UTC — is 00:05 IST on the **16th**, so filtering "the 15th"
by local date would return nothing and look like a broken screen. Reading that
before running it turned up something worse.

`<input type="date">` yields `YYYY-MM-DD`, which was sent as-is and read by
Postgres as **UTC midnight**. The table renders `toLocaleString()`. So:

* **Display and filter disagreed by a day.** The row shows on the 16th and
  answered to a search for the 15th.
* **`lte.<date>` made a single-day range zero-width.** "From the 15th to the
  15th" is `>= midnight` and `<= midnight` — an instant, not a day. **Every
  same-day filter returned nothing, on any date, in any timezone.**

Verified against the real row: before the fix, filtering the 15th *and*
filtering the 16th both found nothing. After it, the 16th finds it and the
15th does not, which is the day it is displayed on.

The fix is a local-day conversion in the UI, because only the screen knows
which day the person meant, and an exclusive upper bound in the service,
because a range naming a day should contain it. A line on screen says the
times are this terminal's, so nobody compares a timestamp here with one from
elsewhere and reads a five-and-a-half-hour gap as a discrepancy.

A viewer whose date filter can never match is worse than one with no filter at
all, because it answers.

### What the live data confirms

Read directly from the project rather than by clicking:

| | |
|---|---|
| Rows | 34, exactly as the script predicted |
| Actions | 7: `scan.resolved` 11, `sale.post` 8, `product.updated` 6, `barcode.added` 4, `barcode.withdrawn` 3, `product.created` 1, `scan.dismissed` 1 |
| Target row | `scan.dismissed`, M001 Priya Nair, barcode 7622202819933, `resolution: dismissed` |
| Approvers | **none** — every row's `approver_id` is null, so the column is an em dash throughout |
| Actors | **all 34 present** — no row exercises "System" |
| Stores | **all 34 have a `store_id`** — no row exercises the null-store branch |
| Range | 2026-08-23 to 2026-09-16 IST, so a default window must reach back weeks |

### Two corrections to the script, and one gap in the trail

**`product.created` does not carry the barcode.** Its `after_json` holds the
product's own columns and nothing else, so filtering to it and expecting
7622202819933 shows an empty "What". The link is in `barcode.added` and
`scan.resolved`, both at 00:08:50 — the code appears in three rows, not two.

**The story in the data is better than "dismissed, then catalogued".** The
same code was dismissed at 00:05:43 and resolved at 00:08:50, three minutes
apart, by the same person. The log carries every step.

**But the screen cannot ask the question that finds it.** "What happened to
this barcode?" spans three rows with three different `entity_id`s, and the
filter has no way to search `after_json`. A manager can answer "who did that?"
and cannot yet answer "what happened to this code?" — which is the next
increment for this screen, not a fault in it.

### `store_id is null` is dead in practice

0019 widened `audit_log_select` because "a catalogue edit belongs to no store",
and the viewer's query and its test both cover that case. **No row in the live
data has a null `store_id`**, catalogue edits included. Either the trigger sets
one or the rationale was aspirational; the branch is right to keep, but it is
carrying no weight today and nobody should read the live pass as exercising it.

### What the live run cannot prove

* **"System"** needs a row with a null `actor_id`. None exists.
* **"Someone outside this store"** needs a second store. None exists.
* **The override flow, online.** `audit_log` holds no `override` rows at all.

All three stay proven by test only. The first two are the sentences that
distinguish three different absences, which is exactly the kind of thing a
green acceptance run gets quietly credited with covering.

### Only one Edge Function is deployed

`authenticate-pin` is live at version 3. `authorize-override` and
`check-revocations` are written, tested and **not deployed**, so slice 3's
online path and slice 4's revocation sweep have never run against real
Supabase. Slice 3's own exit criterion is the *offline* override, which needs
no deployment; the deployed function is what makes the online half real.

Order: slice 5 now, deploy the two functions, then slice 3's and slice 4's
acceptance runs.

**And the deployment falsified a comment.** `authenticate-pin` is deployed with
`verify_jwt: true` even though its own header says `--no-verify-jwt`, and it
works — because the anon key *is* a signed project JWT, so a gateway that
demands one lets it straight through. `check-revocations` claimed to differ
from "its two neighbours" on that flag; the claim was wrong and the distinction
was never the flag. What actually separates it is in its code: the anon key
refused by name, and `auth.getUser()` under the caller's own token.

### The acceptance steps that need a person

`docs/phase-7-acceptance.md` — 34 checkboxes in four parts. Part A (the audit
viewer) needs nothing deployed; B deploys `authorize-override` and
`check-revocations`; C is slice 3's exit criterion offline; D is revocation and
the snapshot.

**It corrects this document's own proof for the snapshot TTL.** The plan says
"set a snapshot back 15 days and confirm the offline login refuses". Since the
MAC landed, editing `snapshot_expires_at` breaks the seal rather than expiring
the snapshot, so the login is refused for the wrong reason and the TTL is never
consulted. It looks like a pass. The guide seeds a backdated-but-sealed row
instead, and the two failures are distinguishable by message and status:

| What you did | Message | Status |
|---|---|---|
| Broke the seal | "has not signed in on this terminal before" | 503 |
| Genuinely expired | "This terminal has been offline too long" | 401 |

Both verified against a throwaway database before the guide was written.

---

## Closed

**21 September 2026.** Weeks 16–17 of the plan.

**Exit criteria met.** The permission matrix exists as one parametrised test
at all three layers — 100 Python cases, 55 FastAPI, 9 RLS tables discovered
from `pg_policies`, 100 UI — and passes. With the network cable out, a cashier
was refused a discount, a supervisor lent the key from the override dialog,
the line discounted, the cashier stayed signed in, and the audit row named
both people. Ninety seconds later it asked again. A snapshot signed sixteen
days ago was refused at offline login with the TTL's own sentence, as a 401;
the same row with its expiry edited was refused by the seal, as a 503, and
`pos.log` said why.

**Definition of done (plan §5), all six:**

- [x] **Tests pass in CI, including the permission matrix.** 1065 Python,
      103 RLS, 217 UI; seven gates. The matrix is the phase's first commit,
      not its last, and the two `xfail(strict=True)` markers it planted both
      came off with their fixes.
- [x] **It runs from the packaged build.** `packaging/pos.spec` from phase 6,
      rebuilt for this phase's UI.
- [x] **It works with the network disconnected, or fails with a message a
      cashier can act on.** The override's exit criterion is the offline
      path; every one of its eight refusals is a different sentence.
- [x] **No new `float` in money paths.** `check_no_float` caught one this
      phase — a measured argon2 cost in a docstring — and it moved to the
      test that argues with it.
- [x] **Audit rows exist for anything a manager would need to investigate
      later.** `override.granted` is the first row ever to carry
      `approver_id`, written where the grant is minted rather than where it
      is spent, and it now leaves the building (0022, 0023).
- [x] **A non-developer has used it for 15 minutes.** The acceptance run in
      `docs/phase-7-acceptance.md` — 42 boxes, four parts, run by a person
      against the real project.

**Migrations 0022–0023, Edge Functions `authorize-override` and
`check-revocations`, local migrations 005–007.** All deployed; all three
functions ACTIVE.

### What the acceptance run found that the suite did not

The runbook was itself wrong in four places, and every one was a claim the
document could make without checking:

- **Part B deployed the functions and not the migrations.** C3 passed and C4
  quarantined the override — and the sale travelling beside it — as
  "unknown entity override". A runbook that lists two of three deploy steps
  looks complete.
- **`status = 'disabled'`** is a value `employees_status_check` has never
  accepted. The schema has `active`, `suspended`, `terminated`.
- **Three SQL snippets against `cached_users`** were indistinguishable from
  the Supabase ones beside them, and the table only exists on the terminal.
- **A `python -c` one-liner** that PowerShell could never have passed to
  Python intact.

And three defects in the product:

- **A refused batch quarantines innocent rows with the wrong error.** Sale
  `ST01-T1-000008` was recorded as failing for an entity it does not
  contain. Card on the board.
- **"A manager can see why in the failures list" — there is no failures
  list.** The route and the client function exist; no screen renders them.
  The sentence shown at the moment a manager most needs a next step points
  at a place that was never built. Card on the board.
- **The audit screen cannot show a barcode.** `after_json` is not rendered,
  so "what happened to this code?" takes three filters and a memory for
  timestamps.

### One thing to check before trusting the cache

`pos.log` line 3803: at 17:36 on 19 September, **M001's** cached identity
failed its seal — before Part D began and with nobody editing her row. The
message offers two causes, tampering or a lost keychain entry, and it was the
second: something between her last cache write and that read changed the
per-terminal key. The D2 rehearsal against a throwaway `POS_DATA_DIR` is the
likely suspect. She is believed to have signed in online since, which
re-seals the row and is the designed recovery — but *believed* is the word,
and the check is one grep for a later `M001` login in the log. If a rehearsal
in a scratch directory can rotate the real terminal's key, that is a bug and
wants a card; if it cannot, the log should say what did.

### The rule this phase added to the house

**A check whose passing state is "found nothing" needs a positive control**,
because the check and its own failure are indistinguishable from outside.
Five instances by the end: the route walker that checked 1 of 46 operations,
audit tests counting rows they never created, the UTF-16 ignore rule, the
stale bundle serving a deleted screen, and a keyset pull returning one row
under RLS that omits rather than refuses. And its sharper sibling, found when
a fake PostgREST answered a column the query never asked for: **a test that
constructs its own input cannot tell you where production gets one.**

Slice 1's three discovery guards, slice 2's pattern sample, slice 3's
`SESSION_MINTING` set and the `checked=False` guard in `check-revocations`
are the same defence in six places. The phase 6 lesson — look for actions
that are destructive by omission — turned out to have a testing-shaped twin.

### Debt carried forward, recorded not hidden

- **Sync stops an hour after sign-in.** `SupabaseAuthClient.refresh` has no
  caller; the token expires and the pusher retries a dead credential for
  ever. Card.
- **The dead-code guard watches one language.** `check_dead_client.py` walks
  TypeScript; the refresh bug is the same shape in Python. Card.
- **`stock.receive` alone admits an adjustment** — the one `xfail` still
  standing from slice 1. `0012`'s comment describes the policy that was
  intended; the SQL is a flat OR of three.
- **`auth.revoked` is broadcast and nothing listens.** A cashier deactivated
  mid-shift learns from the 401 at the next sale, not from the screen.
- **Stock movements accept a product that does not exist.** Local
  `stock_ledger.product_id` has no FK; the cloud's does. Card from slice 1.
- **The audit screen cannot search `after_json`.** See above.
- **The cloud PIN throttle is per-instance and in-memory.** Said in the
  function; the terminal's persisted counter is the binding one.

**Next: phase 8 — Shifts and reporting (weeks 18–20).**

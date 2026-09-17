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

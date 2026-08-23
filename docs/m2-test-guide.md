# Testing M2 — "offline-capable"

M2 is the point where the till stops being a program and becomes a system: it
takes sales whether or not the internet is there, and everything it took
arrives in the cloud once the internet comes back.

The automated half is done. This is the other half — running it against the
real Supabase project on the real machine, which is the only place the last
seven bugs were found.

Budget an hour of attention spread over a day. Step 0 is a pre-flight,
1–3 are a warm-up, 4–8 are the test, 9 is the verdict.

---

## What you are proving

Five claims, in order of how much they would hurt if false:

1. Nothing sold offline is lost.
2. Nothing sold offline arrives **twice**.
3. Stock in the cloud moves by exactly what was sold — not double.
4. The cashier never waits on the network to finish a sale.
5. When something is genuinely refused, it is visible, and the queue keeps
   moving.

## Reading the numbers

Everything is an integer. Money is **paise** — ₹37.00 is `3700`. Quantities are
**thousandths** — 1.250 kg is `1250`, one unit is `1000`. There is no decimal
column in either database, and a test asserts there never will be.

---

## 0. Pre-flight

Setting the project up is `docs/existing-project-setup.md`. This checks it took.

**Schema.** In the Supabase SQL editor:

```sql
select count(*) as tables from information_schema.tables
 where table_schema = 'public' and table_type = 'BASE TABLE';
```

Expect **24**.

```sql
select proname from pg_proc where proname = 'sync_push';
```

Expect one row. Without it every push is a 404 and the backlog just grows.

**Migrations.** `supabase db push` should say *"Remote database is up to date"*
with `0000` … `0010` applied.

**Login function.** Dashboard → Edge Functions → `authenticate-pin` exists and
is deployed.

**JWT hook.** Dashboard → Authentication → Hooks → *Customize Access Token
(JWT) Claims* → `public.custom_access_token_hook`. Without it, tokens carry no
permissions and every RLS policy denies — which looks exactly like a sync bug.

**Environment.** `.env` has `POS_SUPABASE_URL` and `POS_SUPABASE_ANON_KEY` —
the `POS_` prefix matters, `app/config.py` reads nothing else. The anon key,
never `service_role`.

**Tests green.**

```powershell
pytest
python scripts\run_rls_tests.py
cd ui-src ; npm test ; cd ..
```

## 1. Start clean and sign in **online**

```powershell
python -m app.main
```

Sign in with the network **connected**. A push carries the cashier's own JWT,
so RLS applies to sync exactly as it applies to everything else. An offline
login has no token and the queue will sit there reporting *"no cloud session
yet"* however good the network is.

Watch the badge settle on **Synced**. No badge at all means no Supabase project
configured — back to step 0.

## 2. Let the catalogue arrive

Watch the console for `pulled N products`, repeating until it stops. Then check
the terminal has the whole catalogue, not one page of it:

```powershell
python -c "import sqlite3, os; c=sqlite3.connect(os.path.join(os.environ['PROGRAMDATA'],'RetailPOS','pos.sqlite3')); print('products', c.execute('select count(*) from products').fetchone()[0]); print('barcodes', c.execute('select count(*) from product_barcodes').fetchone()[0])"
```

```sql
select 'products' as t, count(*) from public.products
union all select 'barcodes', count(*) from public.product_barcodes;
```

The pairs must match. A round number like exactly 1000 means the pull stalled.
No `FOREIGN KEY constraint failed` in the console.

## 3. Record your starting point

```sql
select count(*) as sales_before from public.sales;
```

```sql
select product_id, on_hand from public.stock_levels
 order by product_id limit 20;
```

Write both down. Everything in step 7 is a comparison against these.

## 4. Sell online first

Ring up three or four baskets with the network up — cash, and at least one UPI
marked **Received**. Each should reach the cloud within about five seconds; the
badge flicks through *Sending* and back to **Synced**.

Check one sale end to end:

```sql
select s.receipt_no, s.grand_total,
       (select count(*) from sale_lines where sale_id = s.id) as lines,
       (select count(*) from payments   where sale_id = s.id) as payments
from public.sales s
order by s.server_received_at desc
limit 5;
```

No zeros in `lines` or `payments`. On the UPI sale, check the UTR reached
`payments.utr` — if the reference is missing here it is missing from your audit
trail, and that is worth catching before the day of offline selling rather than
after it.

## 5. Pull the plug, and sell for a day

Disconnect the network — physically, or disable the adapter. **Do not stop the
app.**

Now sell. Real baskets if you can, invented ones if not, but volume is the
point: aim for at least 50, and include

- a cash sale with change and a rounding adjustment,
- a UPI sale attested **Received**,
- a UPI sale marked **Not sure** — this one lands in the review queue,
- a voided line,
- a sale taken after restarting the app mid-day.

The last one matters: the queue must survive a restart, and that is exactly the
kind of thing that otherwise only gets tested by accident.

While offline, confirm **claim 4** by feel. No sale should pause, spin, or show
a dialog about the network. The badge counts up — *"12 sales waiting"* — and
that is the only place the cashier hears about it.

You do not need to keep a tally. The terminal is the tally:

```powershell
python -c "import sqlite3, os; c=sqlite3.connect(os.path.join(os.environ['PROGRAMDATA'],'RetailPOS','pos.sqlite3')); print('sales on this terminal:', c.execute('select count(*) from sales').fetchone()[0])"
```

## 6. Plug it back in

Reconnect, and **sign in again** if the app was restarted or the session
lapsed. No token, no push.

The queue drains on the next cycle (within ~90s), or immediately if you press
**Send now** on the badge. That button appears only while sales are queued and
the terminal is online. Watch it count down to **Synced**.

## 7. Count

**Claims 1 and 2, together:**

```sql
select count(*) as sales_now from public.sales;
```

`sales_now` minus `sales_before` must equal the terminal's own count from
step 5, minus the sales you made online in step 4.

One short means something was lost. One over means a replay duplicated, which
is the more dangerous of the two because it also doubles the money.

**Claim 3, stock:**

```sql
select product_id, on_hand from public.stock_levels
 order by product_id limit 20;
```

Compare against step 3. Each product must have moved down by exactly what you
sold — remember thousandths, so one unit is `1000`. *Twice* what you sold is
the specific bug a duplicate trigger causes; it was found and fixed during the
build, and the real database is the only place a regression would show.

**And that every sale travelled whole:**

```sql
select s.receipt_no, s.grand_total,
       (select count(*) from sale_lines where sale_id = s.id) as lines,
       (select count(*) from payments   where sale_id = s.id) as payments
from public.sales s
order by s.server_received_at desc
limit 20;
```

No zeros in either column.

> `public.sales` has **no `created_at`**. It has `client_created_at` (terminal
> time) and `server_received_at` (cloud time). Report on the latter: clocks
> drift and cashiers change them.

## 8. Check what got set aside

**Empty is the pass.** If the badge is green and says **Synced**, there is
nothing here.

If it is red and says *"N sales need a manager"*, sign in as the manager
(`M001` / `5820`) and read the list:

```
GET /sync/failures
```

Each row shows the payload that was actually sent and why it was refused —
enough to tell a schema mismatch from an RLS denial from bad data. **Write the
error down before doing anything else.** Anything here is a real finding.

Once you have fixed the cause, **Try again** on the badge re-queues them and
pushes immediately. It is manager-only and deliberately manual: a sale the
cloud keeps refusing should keep being refused visibly, not cycle through the
queue burying the reason.

**Then the review queue.** The "Not sure" UPI sale from step 5 should be
sitting in it with `requires_review` set, resolvable to **paid** or **not
paid**:

```sql
select count(*) from public.sales where status = 'requires_review';
select * from public.sale_reviews order by resolved_at desc limit 5;
```

Resolving writes an append-only `sale_reviews` row — it does not edit the sale,
and it does not yet move money. That is phase 7.

## 9. The verdict

M2 passes when all five claims hold. Partial passes are worth recording as they
are — a fail on 5 is a very different problem from a fail on 2.

| # | Claim | How you checked | Pass? |
|---|-------|-----------------|-------|
| 1 | Nothing lost | step 7 count matches the terminal's | |
| 2 | Nothing duplicated | step 7 count is not *over* | |
| 3 | Stock moved once | step 7 stock diff matches sales | |
| 4 | No sale waited on the cloud | step 5, by feel | |
| 5 | Refusals visible, queue moving | step 8 failures list | |

---

## Reading the badge

| It says | It means |
|---|---|
| **Synced** | Everything is upstairs. |
| **Sending N sales** | Online, draining. |
| **N sales waiting** | Offline. Nothing is lost. **Send now** appears once online. |
| **N sales need a manager** | Something was refused. Step 8. |
| **Update needed** | The server is on a newer schema than this build. |

Hover it for the detail, including the last error.

## When something looks wrong

**Badge stuck on "N sales waiting" with the network up.** Almost always the
token: sign in again while online. The tooltip says *"no cloud session yet"*
when that is it. An offline sign-in produces a working till with no token, and
nothing else on screen distinguishes the two.

**"Update needed".** The server is on a newer schema than this build. Waiting
will not fix it; the terminal needs the newer build.

**No badge where the badge should be.** `/sync/status` is returning 503 — no
Supabase project configured. Check the `POS_` prefix on the `.env` keys.

**`HTTP 400` in the failures list.** A payload the server will not accept.
The message names the column. This is how the missing `entity_id` cast was
found; the same shape would catch the next one.

**`HTTP 403: new row violates row-level security policy`.** The caller is not
in the store, or lacks `sale.create`. Check the JWT actually carries claims —
if `permissions` is empty, the access token hook is not enabled.

**`pull failed: FOREIGN KEY constraint failed`.** The catalogue arrived
incomplete: barcodes for products the terminal was never sent. Check step 2's
counts match. The cause is a stalled pull, not the barcodes.

**A sale is in SQLite but not in the cloud, and not in the failures list
either.** The interesting one. Check `POS_TERMINAL_ID` is set in the `.env` the
app actually read, then look at the outbox row's attempt count and last error:

```powershell
python -c "import sqlite3, os; c=sqlite3.connect(os.path.join(os.environ['PROGRAMDATA'],'RetailPOS','pos.sqlite3')); c.row_factory=sqlite3.Row; [print(dict(r)) for r in c.execute('select id, entity, attempts, last_error, synced_at from outbox where synced_at is null')]"
```

Whatever you find, note the sale id. The local database keeps everything, so
nothing here is unrecoverable, and a reproducible case is worth more than a
clean run.

---

## What the last run turned up

Seven bugs, none of which the test suite could have caught as it stood. Kept
here because they are the shapes to watch for on the next terminal, not
because they are still open — each has a test now.

| Symptom | Cause |
|---|---|
| `permission denied for schema auth` on `db push` | Supabase reserves `auth`; helpers moved to a private `pos` schema |
| `Request failed (500)` on sign-in | A 404 from an undeployed Edge Function escaped unhandled |
| `employee_not_provisioned`, with healthy-looking rows | Seeded `auth.users` left GoTrue's token columns NULL |
| Backlog frozen, retried forever, failures list empty | `cashier_id = auth.uid()` made a mixed-cashier batch unpushable by anyone |
| `column "entity_id" is of type uuid` | A missing cast in the audit branch took the whole sale with it |
| Catalogue stopped at exactly 1000 products | Every row shared one `updated_at`; the watermark could not advance |
| `table stock_levels has no column named store_id` | The cloud keys it on the pair; the terminal does not carry a store id |

# Testing M2 — "offline-capable"

M2 is the point where the till stops being a program and becomes a system: it
takes sales whether or not the internet is there, and everything it took
arrives in the cloud once the internet comes back. The automated half is done
(the four chaos-test criteria under Phase 5 of the execution plan pass as
tests). This guide is the other half — running it against the real Supabase
project on the real machine.

Budget about an hour of attention, spread over a day. Steps 1–3 are setup,
4–8 are the test, 9 is the verdict.

---

## What you are trying to prove

Five claims, in order of how much they would hurt if false:

1. Nothing sold offline is lost.
2. Nothing sold offline arrives **twice**.
3. Stock in the cloud moves by exactly what was sold — not double.
4. The cashier never waits on the network to finish a sale.
5. When something is genuinely refused, it is visible, and the queue keeps moving.

---

## 1. Apply the migrations to the live project

`sync_push` does not exist server-side until `0008` is applied. Without it
every push comes back 404 and the backlog just grows.

```bash
supabase link --project-ref <your-project-ref>
supabase db push          # applies 0001 … 0008
```

Verify in the SQL editor:

```sql
select proname from pg_proc where proname = 'sync_push';
select count(*) from public.sale_reviews;   -- 0 rows, but the table must exist
```

Both must answer. If `sync_push` is missing, stop here — nothing below will
work.

## 2. Give the terminal a cloud identity

Local rows carry the terminal *code* (`T1`), because that is what a receipt
number is built from and what a person at the counter recognises. The cloud
keys terminals by UUID. Until you bridge the two, sales queue locally and
**deliberately refuse to push** rather than sending a sale the server cannot
attribute to a machine.

Find the UUID:

```sql
select id, code, name from public.terminals;
```

Put it in `.env`, along with the project details:

```
POS_SUPABASE_URL=https://<ref>.supabase.co
POS_SUPABASE_ANON_KEY=<anon key — never the service_role key>
POS_TERMINAL_ID=<the uuid from the query above>
POS_STORE_CODE=ST01
POS_TERMINAL_CODE=T1
```

On the demo seed the UUID is `018f0000-0000-7000-8000-000000000200`.

> **Anon key only.** If a `service_role` key ever reaches this file, the till
> can read every store's margins and RLS stops meaning anything.

## 3. Start clean, and sign in **online**

```bash
python -m app.main
```

Sign in with the network **connected**. This matters more than it looks: a
push carries the cashier's own JWT, so RLS applies to sync exactly as it
applies to everything else. An offline login has no token, and the queue will
sit there reporting *"no cloud session yet"* however good the network is.

Watch the badge in the register header settle on **Synced**. If no badge
appears at all, there is no Supabase project configured — go back to step 2.

Then record your starting point, so you have something to compare against:

```sql
select count(*) from public.sales;
select product_id, qty_thousandths from public.stock_levels
order by product_id limit 20;
```

Write both down.

## 4. Sell online first

Ring up three or four baskets with the network up — cash, and at least one
UPI marked **Received**. Each should reach the cloud within about five seconds
(the busy interval); the badge flicks through *Sending* and back to *Synced*.

Check one sale end to end in Supabase: the `sales` row, its `sale_lines`, its
`payments`, and the UTR on the UPI one. If the reference is missing here it is
missing from your audit trail, and that is worth catching before the day of
offline selling rather than after it.

## 5. Pull the plug

Disconnect the network — physically, or disable the adapter. Do not stop the
app.

Now **sell for a day**. Real baskets if you can, invented ones if not, but
volume is the point: aim for at least 50, and include

- a cash sale with change and a rounding adjustment,
- a UPI sale attested **Received**,
- a UPI sale marked **Not sure** (this one lands in the review queue),
- a voided line,
- a sale taken after restarting the app mid-day.

The last one matters: the queue must survive a restart, and that is exactly
the kind of thing that otherwise only ever gets tested by accident.

While offline, confirm **claim 4** by feel: no sale should pause, spin, or
show a dialog about the network. The badge counts up — *"12 sales waiting"* —
and that is the only place the cashier hears about it.

Keep a tally of how many sales you took. You will need the exact number.

## 6. Plug it back in

Reconnect, and **sign in again** if the app was restarted or the session
lapsed. No token, no push.

The queue drains on the next cycle (within ~90s), or immediately if you press
**Send now** on the badge. Watch it count down to **Synced**.

## 7. Count

The whole test lives in this query:

```sql
select count(*) from public.sales;
```

Subtract your step-3 starting count. It must equal exactly the number of sales
you took — **claims 1 and 2 together**. One short means something was lost;
one over means a replay duplicated, which is the more dangerous of the two
because it also doubles the money.

Then **claim 3**, stock:

```sql
select product_id, qty_thousandths from public.stock_levels
order by product_id limit 20;
```

Compare against your step-3 snapshot. Each product must have moved down by
exactly what you sold. *Twice* what you sold is the specific bug a duplicate
trigger causes — it was found and fixed during the build, and the real
database is the only place a regression would show.

Spot-check that a sale's parts all travelled together:

```sql
select s.id, s.total_minor,
       (select count(*) from sale_lines where sale_id = s.id) as lines,
       (select count(*) from payments   where sale_id = s.id) as payments
from public.sales s order by s.created_at desc limit 10;
```

No zeros in either column.

## 8. Check what got set aside

Sign in as the manager (`M001`) and open the failures list, or call it
directly:

```
GET /sync/failures
```

**Empty is the pass.** If it is not, each row shows the payload that was
actually sent and why it was refused — enough to tell a schema mismatch from
an RLS denial from bad data. Anything here is a real finding; write it down
rather than retrying it away.

Also check the review queue: the "Not sure" UPI sale from step 5 should be
sitting in it with `requires_review` set, resolvable to **paid** or **not
paid**. Resolving writes an append-only `sale_reviews` row — it does not edit
the sale, and it does not yet move money. That is phase 7.

## 9. The verdict

M2 passes when all five claims hold. Partial passes are worth recording as
they are — a fail on 5 is a very different problem from a fail on 2.

| # | Claim | How you checked | Pass? |
|---|-------|-----------------|-------|
| 1 | Nothing lost | step 7 count matches your tally | |
| 2 | Nothing duplicated | step 7 count is not *over* | |
| 3 | Stock moved once | step 7 stock diff matches sales | |
| 4 | No sale waited on the cloud | step 5, by feel | |
| 5 | Refusals visible, queue moving | step 8 failures list | |

---

## When something looks wrong

**Badge never leaves "N sales waiting" with the network up.** Almost always
the token: sign in again while online. If it persists, `GET /sync/status` —
`last_error` names the actual cause.

**"Update needed".** The server is on a newer schema than this build. Waiting
will not fix it; the terminal needs the newer build.

**No badge where the badge should be.** `/sync/status` is returning 503 — no
Supabase project configured. Step 2.

**A sale is in SQLite but not in the cloud, and not in the failures list
either.** That is the interesting one. Check `POS_TERMINAL_ID` is actually set
in the `.env` the app read, then look at the outbox row's attempt count and
last error.

Whatever you find, note the sale id. The local database keeps everything, so
nothing here is unrecoverable, and a reproducible case is worth more than a
clean run.

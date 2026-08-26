# Re-tuning argon2 for the Edge Function

A real sign-in against the hosted project took **3,750–5,274 ms** inside
`authenticate-pin`. Locally the same check is ~87 ms. Supabase caps CPU time
per invocation, and a busy counter will eventually cross it — at which point
nobody can sign in.

This is the last thing on the list before a pilot.

---

## The fact that decides how to do this

**The cost is stored in the hash, not in the configuration.**

An argon2 hash is self-describing:

```
$argon2id$v=19$m=65536,t=12,p=4$Szb2KNf5F61GESkNsgVKGw$ULij2XFBbN7...
                └── memory ──┘ └t┘ └p┘
```

`PasswordHasher.verify()` and hash-wasm's `argon2Verify({password, hash})` both
read `m`, `t` and `p` **out of the hash string** and ignore whatever the caller
is configured with. Two consequences, and they run opposite ways:

**The good one.** Lowering the parameters cannot break a single existing
account. Every stored hash keeps verifying exactly as before. There is no
flag day, no forced PIN reset, no coordination.

**The awkward one.** Lowering the parameters also does not make anything
faster. `authenticate-pin` will still spend four seconds, because it is still
verifying a hash that says `t=12`. Nothing improves until the stored hashes
are **re-minted**, and a hash can only be minted by someone who knows the PIN.

So this is two jobs: choose numbers, then re-mint. Most of the work is the
second.

---

## 1. Measure the till

```powershell
python scripts\tune_argon2.py
```

On the actual till hardware, not a laptop. It prints parameters worth roughly
100 ms there. Write them down — but they are the *floor*, not the answer.

## 2. Measure the real constraint

The till is not what is slow. The Edge Function is, because argon2 compiled to
WebAssembly in a Deno isolate is roughly 50× slower than the native library.

Get the current figure from your own project:

**Dashboard → Edge Functions → `authenticate-pin` → Invocations.** Read
`execution_time_ms` on a successful sign-in. That is the number to beat.

Supabase's per-invocation CPU limit is the ceiling. Aim for **under 1 second**,
which leaves room for the two database round-trips either side of the verify.

## 3. Choose the parameters

Three knobs, and they are not equal.

| | Now | Move it? |
|---|---|---|
| `memory_cost_kib` | 65536 (64 MiB) | **Keep.** This is what stops GPU cracking, and the Edge Function has the RAM. |
| `time_cost` | 12 | **This one.** Cost is roughly linear in `t`, so `t=3` is about a quarter of the time. |
| `parallelism` | 4 | Keep. |

Hold the memory and cut the passes. A four-digit PIN is low-entropy, so keep
as much cost as the runtime affords: pick the largest `t` that lands the Edge
Function under a second.

`t=3` is the sensible first guess from 5.3s — expect roughly 1.3s, then adjust.

**Test the guess before committing to it.** In the SQL editor, against a hash
minted at the candidate parameters, or simply deploy the change and read the
invocation time on the next sign-in. One deploy is cheaper than arithmetic.

## 4. Set them on the terminal

`.env`, read by `app/config.py` via the `POS_` prefix:

```
POS_ARGON2_TIME_COST=3
POS_ARGON2_MEMORY_COST_KIB=65536
POS_ARGON2_PARALLELISM=4
```

Change the defaults in `app/config.py` too, so a fresh checkout does not
inherit the slow ones.

## 5. Fix the decoy hash — this is a bug, not tuning

`authenticate-pin` verifies an unknown employee code against a hard-coded
decoy so that "no such employee" and "wrong PIN" cost the same wall-clock time:

```ts
const hash = employee?.pin_hash ??
  "$argon2id$v=19$m=65536,t=2,p=4$AAAAAAAAAAAAAAAAAAAAAA$" + "AAAA...";
```

The decoy says **`t=2`**. Real hashes say **`t=12`**. So an unknown code
returns in a fraction of the time a known one takes, and anyone can enumerate
valid employee codes with a stopwatch — which is precisely what the decoy
exists to prevent.

Whatever `t` you settle on, **the decoy must carry the same parameters**. Mint
a fresh decoy rather than editing the digits by hand:

```powershell
python -c "from argon2 import PasswordHasher; from argon2.low_level import Type; print(PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID).hash('no-such-employee'))"
```

**`scripts/remint_pin_hashes.py` does this**, along with the seed, and
`tests/test_pins.py` fails if either drifts from the shipped parameters. The
decoy test asserts `needs_rehash(decoy) is False`, which is only true of a
well-formed hash at the current cost — so it catches a placeholder as well as a
stale one. A malformed decoy is the worse failure: `argon2Verify` throws on it
immediately, so an unknown code returns in no time at all and the oracle is
wider than it was before.

## 6. Re-mint the stored hashes

Nothing above changes a single sign-in until this happens.

**The development accounts.** `supabase/seed/seed.sql` carries three literal
hashes at `t=12`. Regenerate them:

```powershell
python -c "from argon2 import PasswordHasher; from argon2.low_level import Type; h=PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID); [print(c, h.hash(p)) for c,p in [('C001','4913'),('S001','7261'),('M001','5820')]]"
```

`scripts/remint_pin_hashes.py` rewrites all three in place. Do **not** apply the
new seed to a project you are about to test on: leaving the live accounts at the
old cost is what proves rehash-on-login works.

**Real staff.** Their PINs are not in the repository and cannot be. Two routes:

*Rehash on successful login.* **Built.** `authenticate-pin` holds the plaintext
PIN at the moment it verifies — the only moment anyone does — so if the stored
hash does not carry the current parameters it mints a replacement and writes it
back. Every account migrates itself on next sign-in, with no announcement and
no forced reset. Failures are logged and never fatal: a cashier whose rehash
failed is a cashier who signed in.

The Python side already had `needs_rehash()` in `app/security/pins.py`, written
and tested with no caller.

*Or reset the PINs.* Fine for three staff in one shop, not for a fleet.

## 7. Verify

**Sign-in speed:**

```powershell
python -m app.main
```

Sign in, then read the invocation time in the dashboard. Under a second.

**Both roles still work.** Sign in as C001 and M001 — a hash minted at the old
parameters must still verify, since old and new coexist during a rehash-on-login
migration.

**The timing oracle is closed.** Time an unknown code against a known one with
a wrong PIN. The two should be within noise of each other:

```powershell
Measure-Command { curl.exe -s -o NUL -X POST "$url/functions/v1/authenticate-pin" -H "apikey: $key" -H "Authorization: Bearer $key" -H "Content-Type: application/json" -d '{\"employee_code\":\"ZZZZ\",\"pin\":\"0000\",\"store_code\":\"ST01\",\"terminal_code\":\"T1\"}' }
Measure-Command { curl.exe -s -o NUL -X POST "$url/functions/v1/authenticate-pin" -H "apikey: $key" -H "Authorization: Bearer $key" -H "Content-Type: application/json" -d '{\"employee_code\":\"M001\",\"pin\":\"0000\",\"store_code\":\"ST01\",\"terminal_code\":\"T1\"}' }
```

**Tests still pass** — and one of them will need attention:

```python
# tests/test_pins.py, marked slow
assert 20 < elapsed_ms < 600, f"argon2id took {elapsed_ms:.0f} ms"
```

That measures the *shipped* parameters natively. At `t=12` it is ~87 ms,
comfortably inside. At `t=3` it lands near **~22 ms** — a hair above the lower
bound of 20, and below it on a faster machine. The lower bound exists to catch
parameters accidentally set to nothing, so it should move down with the
parameters rather than be deleted. Pick a floor that still fails on `t=1`.

```powershell
pytest
```

---

## What this costs in security

Dropping `t` from 12 to 3 makes an offline attack on a stolen database four
times cheaper. On a four-digit PIN that is 10,000 guesses either way: about 17
minutes per account at `t=12`, about 4 at `t=3` — both trivially affordable to
anyone who has the database.

The honest reading is that **argon2 parameters are not what protects a
four-digit PIN.** What protects it is that the hashes live behind RLS in
Postgres, and that the terminal's local snapshot expires after fourteen days.
The cost parameter buys time against a database leak, and it is buying less
than the four-second sign-in suggests.

If PIN strength is the real worry, six digits buys 100× more than any
parameter here will.


---

## What it measured

Settled at **`t=3`**, memory and parallelism unchanged.

| | Before | After |
|---|---|---|
| `authenticate-pin`, `execution_time_ms` | 3,750–5,274 ms | **1,808 ms** |
| Unknown code vs known code | ~6× apart | 1,106/1,469/1,205 vs 1,279/865/739 — overlapping |

Working back from those two points: argon2 costs roughly **385 ms per pass** in
that isolate, and about **650 ms is fixed** — the two database round-trips
either side. So `t=2` would buy ~390 ms for a third of the work factor, and
`t=1` still could not get under a second. The floor is not argon2.

The CPU share is now ~1.2 s, comfortably inside Supabase's cap. That was the
actual risk: not that sign-in felt slow, but that it would start failing.

This is the *online sign-in* path — once at shift start. Anything verifying a
PIN locally uses the native library at the same parameters, a few milliseconds,
so nothing at the counter inherits it.

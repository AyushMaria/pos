"""How long a PIN stays locked after consecutive failures — architecture §11.4.

Pure, like the rest of `domain/`: "how long is this account locked?" is a unit
test with no clock, no database and no network.

## Why this exists

`authenticate-pin` rate-limits in the cloud — ten attempts per code per five
minutes — and its comment says what for: a till types a PIN wrong
occasionally, a script types thousands. The terminal had no counterpart. Every
offline PIN check went straight to argon2 and back, as often as anybody liked.

That was survivable while the cloud was the only way in. It is not survivable
offline, and the measurement is the argument: argon2id at the shipped
parameters (m=65536, t=3, p=4) verifies in **22.8 ms natively**. The same
parameters inside the Edge Function's WebAssembly take roughly 1.8 seconds,
which is where the comfortable "hours to guess" intuition comes from — and it
is wrong by two orders of magnitude on this side. Ten thousand guesses is four
minutes, and half that expected. A four-digit shop PIN does not survive one
tea break with the network unplugged.

Two paths reach a cached hash, and the worse one is the older:

* `authorize_override` yields a ninety-second grant naming a supervisor, which
  the audit log would then record as a legitimate authorisation.
* `_login_offline` yields **that person's whole session**. A cashier who
  guesses a supervisor's PIN offline becomes the supervisor, which is not
  something they could have had by asking.

## The shape

A rolling window like the cloud's is the wrong instrument here. It resets
itself, so an attacker waits it out and grinds at a fixed rate for ever. This
escalates instead: each batch of failures costs longer than the last, so the
honest fat-finger costs a wait and the unattended grinder falls off a cliff.

Lockouts are per **approver**, not per terminal. A supervisor who mistypes
must not stop the cashier selling — selling is the one thing an outage may
never interrupt (§1.1).
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: Consecutive failures tolerated before the first lockout. Generous enough
#: for a wrong keypad, a sticky key and a genuinely forgotten digit.
FAILURES_BEFORE_LOCK = 5

#: What the first lockout costs, and how it grows. Doubling means the second
#: batch costs 30 minutes and everything after that an hour, so an attacker
#: with all night gets tens of guesses rather than thousands.
FIRST_LOCKOUT = timedelta(minutes=15)
MAX_LOCKOUT = timedelta(minutes=60)

#: Enough doublings to pass MAX_LOCKOUT several times over, and few enough
#: that the arithmetic cannot overflow however long somebody grinds.
_MAX_DOUBLINGS = 8


def lockout_after(consecutive_failures: int) -> timedelta | None:
    """How long to lock after this many consecutive failures, or None.

    Returns None below the threshold: four wrong PINs in a row is a person
    having a bad morning, and locking them would cost the shop more than it
    protects.
    """
    if consecutive_failures < FAILURES_BEFORE_LOCK:
        return None

    batch = consecutive_failures // FAILURES_BEFORE_LOCK

    # The exponent is capped before it is used, not after. `timedelta`
    # overflows somewhere around 2**40 of these, and the moment that would
    # happen is a terminal that has seen a few hundred consecutive failures —
    # which is to say, precisely the attack this function exists for. Raising
    # there would turn the throttle into an unhandled exception on the auth
    # path, and an exception is not a refusal.
    doublings: int = min(batch - 1, _MAX_DOUBLINGS)
    grown: timedelta = FIRST_LOCKOUT * (2**doublings)
    return min(grown, MAX_LOCKOUT)


def locked_until(consecutive_failures: int, *, now: datetime) -> datetime | None:
    """When the lock lifts, given the failure count and the current time."""
    window = lockout_after(consecutive_failures)
    return None if window is None else now + window


def is_locked(locked_until_value: datetime | None, *, now: datetime) -> bool:
    """A lock that has expired is not a lock.

    Nothing clears the column on a timer, so this is the only thing that
    decides. Written as its own function rather than inline so the comparison
    exists in exactly one place — an inverted sign here would unlock every
    account in the shop and read as ordinary.
    """
    return locked_until_value is not None and now < locked_until_value


def guesses_per_hour() -> int:
    """What an attacker gets once the escalation is at its cap.

    Not used by the application. It exists so the argument for these constants
    can be re-run rather than re-remembered — loosen `FIRST_LOCKOUT` and the
    tests that compare this against the cloud's rate will say what was sold.

    Integer arithmetic on purpose. `scripts/check_no_float.py` bans binary
    floating point in this package, and the first draft of this function put a
    `float` annotation and a `22.8` literal in `domain/` for the sake of a
    comment. The ban was right: the measured verify cost belongs in the test
    that uses it, not in the domain.
    """
    # Each batch is FAILURES_BEFORE_LOCK free guesses followed by a lockout.
    # At the cap that is one batch per MAX_LOCKOUT.
    batches_per_hour = 3600 // int(MAX_LOCKOUT.total_seconds())
    return FAILURES_BEFORE_LOCK * batches_per_hour

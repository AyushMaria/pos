"""Offline PIN guessing — phase 7 slice 3.

`authenticate-pin` has rate-limited the cloud path since it was written: ten
attempts per code per five minutes, with a comment naming the enumeration risk
it exists for. The terminal had no counterpart, and the measurement is the
argument for building one.

Argon2id at the shipped parameters (m=65536, t=3, p=4) verifies in **22.8 ms
natively**. The same parameters inside the Edge Function's WebAssembly take
roughly 1.8 seconds, which is where "hours to guess" comes from — and it is
wrong by two orders of magnitude on this side of the wire. Ten thousand
guesses is four minutes, half that expected.

Two paths reach a cached hash, and the older one is worse:

* `authorize_override` yields a ninety-second grant that the audit log would
  record as a legitimate supervisor authorisation.
* `_login_offline` yields *that person's whole session*, and has since phase 1.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain import lockout
from app.domain import permissions as perms
from app.security.snapshot_mac import SnapshotSealer
from app.services.auth_service import LoginFailed, PinLocked
from tests.conftest import TEST_MAC_KEY, TEST_STORE_ID

NOW = datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc)

#: Measured on this machine with the shipped parameters (m=65536, t=3, p=4).
#: Lives here rather than in `domain/`, which bans binary floating point —
#: and rightly: this is evidence for a decision, not a rule of the domain.
MEASURED_VERIFY_MS = 22.8


# ── The policy, with no clock and no database ───────────────────────────────


@pytest.mark.parametrize("failures", range(lockout.FAILURES_BEFORE_LOCK))
def test_below_the_threshold_nothing_locks(failures: int) -> None:
    """Four wrong PINs in a row is a person having a bad morning."""
    assert lockout.lockout_after(failures) is None


def test_the_threshold_locks_for_the_first_window() -> None:
    assert lockout.lockout_after(lockout.FAILURES_BEFORE_LOCK) == lockout.FIRST_LOCKOUT


def test_each_batch_costs_more_than_the_last() -> None:
    """Escalation is the whole reason this is not a rolling window.

    A window resets itself, so an attacker waits it out and grinds for ever at
    a fixed rate. Doubling means all night buys tens of guesses, not thousands.
    """
    first = lockout.lockout_after(lockout.FAILURES_BEFORE_LOCK)
    second = lockout.lockout_after(lockout.FAILURES_BEFORE_LOCK * 2)
    third = lockout.lockout_after(lockout.FAILURES_BEFORE_LOCK * 3)

    assert first is not None and second is not None and third is not None
    assert second > first
    assert third >= second


def test_the_lockout_is_capped() -> None:
    """Unbounded growth would lock a supervisor out for days over a typo."""
    assert lockout.lockout_after(lockout.FAILURES_BEFORE_LOCK * 50) == lockout.MAX_LOCKOUT


def test_an_expired_lock_is_not_a_lock() -> None:
    """Nothing clears the column on a timer, so the comparison is the only
    thing that decides. An inverted sign here would unlock the whole shop and
    read as entirely ordinary."""
    assert lockout.is_locked(NOW + timedelta(minutes=1), now=NOW)
    assert not lockout.is_locked(NOW, now=NOW)
    assert not lockout.is_locked(NOW - timedelta(seconds=1), now=NOW)
    assert not lockout.is_locked(None, now=NOW)


def test_the_escalation_buys_what_it_claims() -> None:
    """The constants are a judgement; this is the arithmetic behind it.

    Unthrottled, 22.8 ms a guess walks a four-digit space in about four
    minutes. At the cap the attacker gets five guesses an hour, which turns
    the same space into weeks. If someone loosens these numbers, this is the
    line that says what was sold.
    """
    per_hour = lockout.guesses_per_hour()
    assert per_hour <= 10, f"{per_hour} guesses/hour is not a throttle"

    unthrottled_minutes = 10_000 * MEASURED_VERIFY_MS / 1000 / 60
    throttled_days = 10_000 / per_hour / 24
    assert unthrottled_minutes < 10
    assert throttled_days > 14


# ── The gate, on both paths ─────────────────────────────────────────────────


def _seed_supervisor(auth_service) -> dict[str, str]:
    auth_service.seed_local_user(
        user_id="018f0000-0000-7000-8000-000000000002",
        employee_code="S001",
        full_name="Ravi Menon",
        store_id=TEST_STORE_ID,
        pin="7241",
        roles=frozenset({perms.SUPERVISOR}),
        permissions=perms.permissions_for(frozenset({perms.SUPERVISOR})),
    )
    return {"employee_code": "S001", "pin": "7241"}


def _wrong_logins(auth_service, times: int) -> list[Exception]:
    raised: list[Exception] = []
    for _ in range(times):
        try:
            auth_service._login_offline("S001", "0000")
        except Exception as exc:
            raised.append(exc)
    return raised


def test_offline_login_locks_after_enough_wrong_pins(auth_service) -> None:
    """The path that has been reachable since phase 1.

    `_login_offline` looks an employee up by code, so a cashier who guesses a
    supervisor's PIN here does not get ninety seconds — they get that
    supervisor's session.
    """
    _seed_supervisor(auth_service)

    raised = _wrong_logins(auth_service, lockout.FAILURES_BEFORE_LOCK)

    assert all(isinstance(exc, LoginFailed) for exc in raised[:-1])
    assert isinstance(raised[-1], PinLocked)


def test_a_locked_account_is_refused_even_with_the_right_pin(auth_service) -> None:
    """Otherwise the lock is a speed bump: guess, get locked, and the one
    correct guess still works the moment it is found."""
    correct = _seed_supervisor(auth_service)
    _wrong_logins(auth_service, lockout.FAILURES_BEFORE_LOCK)

    with pytest.raises(PinLocked):
        auth_service._login_offline("S001", correct["pin"])


def test_a_correct_pin_forgets_the_wrong_ones(auth_service, users) -> None:
    """Consecutive, not cumulative. Somebody who mistypes twice a week for a
    year is not an attacker."""
    correct = _seed_supervisor(auth_service)
    _wrong_logins(auth_service, lockout.FAILURES_BEFORE_LOCK - 1)

    auth_service._login_offline("S001", correct["pin"])

    identity = users.get_by_employee_code("S001")
    assert identity.consecutive_pin_failures == 0
    assert identity.pin_locked_until is None


def test_the_counter_survives_a_restart(auth_service, users, db) -> None:
    """Persisted rather than in-process, which is the whole difference.

    The cloud's counter lives in a `Map` and that is fine for a function that
    restarts on its own schedule. A local one that reset when the app closed
    would protect nothing, because closing the app is something the person
    guessing can do.
    """
    _seed_supervisor(auth_service)
    _wrong_logins(auth_service, 3)

    # A new repository over the same file is what a restart looks like here.
    from app.data.repositories.users import CachedUserRepository

    restarted = CachedUserRepository(db, sealer=SnapshotSealer(TEST_MAC_KEY))
    identity = restarted.get_by_employee_code("S001")
    assert identity.consecutive_pin_failures == 3


def test_a_lock_lifts_when_its_time_is_up(auth_service, users) -> None:
    _seed_supervisor(auth_service)
    _wrong_logins(auth_service, lockout.FAILURES_BEFORE_LOCK)

    identity = users.get_by_employee_code("S001")
    assert identity.pin_locked_until is not None

    after = identity.pin_locked_until + timedelta(seconds=1)
    assert not identity.is_locked(now=after)


def test_locking_one_person_does_not_stop_the_till(auth_service, users) -> None:
    """Per approver, never per terminal. Selling is the one thing an outage
    may never interrupt (§1.1), and a supervisor's bad morning is not a
    reason to close the shop."""
    _seed_supervisor(auth_service)
    auth_service.seed_local_user(
        user_id="018f0000-0000-7000-8000-000000000001",
        employee_code="C001",
        full_name="Anita Rao",
        store_id=TEST_STORE_ID,
        pin="4913",
        roles=frozenset({perms.CASHIER}),
        permissions=perms.permissions_for(frozenset({perms.CASHIER})),
    )
    _wrong_logins(auth_service, lockout.FAILURES_BEFORE_LOCK)

    session = auth_service._login_offline("C001", "4913")
    assert session.employee_code == "C001"
    assert users.get_by_employee_code("C001").consecutive_pin_failures == 0


# ── The lock is a fact about the approver, not about the transport ──────────


class _CloudThatAlwaysSaysYes:
    """A reachable Supabase that accepts any PIN.

    Deliberately permissive: the question is whether the local lock is even
    consulted when the network is up, so a cloud that could refuse would
    confuse the answer.
    """

    def __init__(self, identity) -> None:
        self.identity = identity
        self.calls = 0

    async def authenticate_pin(self, employee_code, pin, store_code, terminal_code):
        self.calls += 1
        raise AssertionError(
            "the cloud was asked to verify a PIN for a locked account — the "
            "lock ran inside the offline branch instead of before the "
            "transport choice, so plugging in the network clears it"
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_being_online_does_not_clear_a_lockout(auth_service, users) -> None:
    """The most ordinary action in the shop must not disable the throttle.

    `authenticate-pin` counts in a `Map`, per function instance, with no idea
    which till is asking or how often it has already been refused. If the
    local lock lived inside the offline branch, an attacker would reach for
    the network cable rather than the PIN pad.
    """
    _seed_supervisor(auth_service)
    _wrong_logins(auth_service, lockout.FAILURES_BEFORE_LOCK)

    auth_service.cloud = _CloudThatAlwaysSaysYes(users.get_by_employee_code("S001"))

    with pytest.raises(PinLocked):
        await auth_service.login("S001", "7241")

    assert auth_service.cloud.calls == 0, "the cloud was consulted first"


def test_the_lockout_is_queued_for_the_cloud(auth_service, db) -> None:
    """One row per lockout event, and it has to leave the terminal.

    Slice 5's viewer reads the cloud, so a lockout that stays in SQLite is
    invisible to the one screen built to answer "who did that?".
    """
    _seed_supervisor(auth_service)
    _wrong_logins(auth_service, lockout.FAILURES_BEFORE_LOCK)

    rows = db.query("SELECT * FROM audit_log WHERE action = 'pin.locked'")
    queued = db.query("SELECT * FROM outbox WHERE entity = 'audit'")

    assert len(rows) == 1
    assert len(queued) == 1
    assert queued[0]["entity_id"] == rows[0]["id"]
    assert rows[0]["approver_id"] is None, "nobody authorised a lockout"


def test_the_attempts_themselves_are_not_queued(auth_service, db) -> None:
    """The bound that makes the lockout safe to push.

    An attacker generates refusals at whatever rate they like; the throttle
    generates lockouts. If every attempt were queued, the login screen would
    be an outbox flood with no credential required.
    """
    _seed_supervisor(auth_service)
    _wrong_logins(auth_service, lockout.FAILURES_BEFORE_LOCK)

    queued = db.query("SELECT * FROM outbox WHERE entity IN ('audit', 'override')")
    assert len(queued) == 1, (
        f"{len(queued)} rows queued for {lockout.FAILURES_BEFORE_LOCK} attempts — "
        "the attempts are being pushed, not just the lockout"
    )


def test_a_second_lockout_costs_the_attacker_before_it_costs_the_outbox(
    auth_service, db
) -> None:
    """Five attempts buy one row, and the next one costs fifteen minutes."""
    _seed_supervisor(auth_service)
    _wrong_logins(auth_service, lockout.FAILURES_BEFORE_LOCK * 4)

    queued = db.query("SELECT * FROM outbox WHERE entity = 'audit'")
    assert len(queued) == 1, (
        "further attempts while already locked queued more rows; a locked "
        "account must be refused before the counter is touched"
    )

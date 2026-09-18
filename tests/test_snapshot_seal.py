"""The offline cache is authenticated now — phase 7 slice 4.

`cached_users` is an unencrypted SQLite file on a shop counter holding the
permission snapshot and the expiry that bounds how long a dismissed employee
can keep trading. Every check downstream read those columns and believed them,
so extending a revoked supervisor's access for another fortnight was a text
edit.

**What the MAC does not do**, stated here as well as in the module, because a
defence described as more than it is gets relied on for more than it does:
anyone who can edit this file can also read `pin_hash`, carry it away and
grind it at whatever speed their hardware allows, with no lockout — the
lockout is in the file they just copied. Argon2id at the shipped parameters
costs real time per guess. Editing a column costs none. The seal closes the
free path, and that is worth doing on its own terms.

The whole suite is this file's positive control: 1000-odd tests seed, read and
re-read cached identities, and every one of them goes through
`snapshot_mac.verify`. A seal that verified nothing would not be caught here —
it would be caught by everything.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.data.repositories.users import CachedUserRepository
from app.domain import permissions as perms
from app.domain.identity import utcnow
from app.security import snapshot_mac
from app.security.snapshot_mac import SnapshotSealer
from app.services.auth_service import LoginFailed, NoOfflineIdentity
from tests.conftest import TEST_MAC_KEY, TEST_STORE_ID

SUPERVISOR_ID = "018f0000-0000-7000-8000-000000000002"


@pytest.fixture
def supervisor(auth_service):
    """Somebody whose cached row is worth editing."""
    auth_service.seed_local_user(
        user_id=SUPERVISOR_ID,
        employee_code="S001",
        full_name="Ravi Menon",
        store_id=TEST_STORE_ID,
        pin="7241",
        roles=frozenset({perms.SUPERVISOR}),
        permissions=perms.permissions_for(frozenset({perms.SUPERVISOR})),
    )
    return {"employee_code": "S001", "pin": "7241"}


def _edit(db, sql: str, params: tuple = ()) -> None:
    """Exactly what somebody with the file would do, and nothing more."""
    with db.write() as conn:
        conn.execute(sql, params)


# ── The edits the seal exists to catch ──────────────────────────────────────


def test_an_untouched_row_reads_back(supervisor, users) -> None:
    """The control. Everything below asserts that a row stops working, which
    is also what a seal that never verified anything would produce."""
    identity = users.get_by_employee_code("S001")

    assert identity is not None
    assert identity.user_id == SUPERVISOR_ID
    assert perms.SALE_VOID in identity.permissions


def test_extending_the_expiry_invalidates_the_row(supervisor, users, db) -> None:
    """The attack this whole slice is about.

    Fourteen days is the bound on how long a dismissed employee keeps trading.
    It arrived in a response body, was written to a file anybody at the counter
    can open, and was read back as fact.
    """
    _edit(
        db,
        "UPDATE cached_users SET snapshot_expires_at = ? WHERE user_id = ?",
        ((utcnow() + timedelta(days=3650)).isoformat(), SUPERVISOR_ID),
    )

    assert users.get_by_employee_code("S001") is None


def test_reactivating_a_disabled_account_invalidates_the_row(
    supervisor, users, db
) -> None:
    _edit(
        db, "UPDATE cached_users SET status = 'active' WHERE user_id = ?", (SUPERVISOR_ID,)
    )
    # This one changed nothing: the row was already active. A MAC is over
    # values, not over write events, so it cannot see a write that wrote the
    # same thing — and should not. Asserted because the opposite behaviour
    # would make every harmless UPDATE look like an attack, and a defence that
    # cries wolf gets switched off.
    identity = users.get_by_employee_code("S001")
    assert identity is not None, "an idempotent UPDATE must not break a row"

    _edit(
        db,
        "UPDATE cached_users SET status = 'disabled' WHERE user_id = ?",
        (SUPERVISOR_ID,),
    )
    assert users.get_by_employee_code("S001") is None


def test_granting_yourself_a_permission_invalidates_the_row(
    supervisor, users, db
) -> None:
    """The permission snapshot is sealed too, not just the expiry.

    A row that sealed the dates and left `cached_permissions` open would be a
    MAC over the least interesting half: `user.manage` is one INSERT away.
    """
    _edit(
        db,
        "INSERT INTO cached_permissions (user_id, store_id, permission_key) "
        "VALUES (?, ?, ?)",
        (SUPERVISOR_ID, TEST_STORE_ID, perms.USER_MANAGE),
    )

    assert users.get_by_employee_code("S001") is None


def test_clearing_a_lockout_invalidates_the_row(supervisor, users, db, auth_service) -> None:
    """Why the counters are inside the seal rather than beside it.

    Leaving them out would have been cheaper — no re-seal on every wrong PIN —
    and would have left `pin_locked_until = NULL` as a free edit. That is the
    single edit most useful to the person this lockout exists to stop, who is
    by definition sitting at the terminal.
    """
    from app.domain import lockout
    from app.services.auth_service import PinLocked

    for _ in range(lockout.FAILURES_BEFORE_LOCK):
        with pytest.raises((LoginFailed, PinLocked)):
            auth_service._login_offline("S001", "0000")

    assert users.get_by_employee_code("S001").pin_locked_until is not None

    _edit(
        db,
        "UPDATE cached_users SET consecutive_pin_failures = 0, "
        "pin_locked_until = NULL WHERE user_id = ?",
        (SUPERVISOR_ID,),
    )

    assert users.get_by_employee_code("S001") is None, (
        "a lockout was cleared by editing the file and the row still read back"
    )


def test_a_row_from_before_sealing_is_not_trusted(supervisor, users, db) -> None:
    """Upgrading a terminal costs one online sign-in, on purpose.

    Trusting unsealed rows "just for the migration" would leave the door open
    permanently: an attacker would blank the column rather than forge it.
    """
    _edit(db, "UPDATE cached_users SET row_mac = NULL WHERE user_id = ?", (SUPERVISOR_ID,))

    assert users.get_by_employee_code("S001") is None


def test_another_terminals_key_does_not_open_this_file(supervisor, db) -> None:
    """The key is per terminal, so a copied database is useless on another one.

    Which is the correct answer to somebody moving a file to a machine they
    control.
    """
    stranger = CachedUserRepository(db, sealer=SnapshotSealer(b"\xff" * 32))

    assert stranger.get_by_employee_code("S001") is None


# ── The refusal a cashier actually meets ────────────────────────────────────


def test_a_tampered_row_reads_as_no_cached_identity(supervisor, auth_service, db) -> None:
    """Not an exception at the till.

    The remedy is the same as for an employee this terminal has never seen —
    sign in online once — and the likely cause is a moved Windows profile
    rather than an attack. A till that refused to open would turn housekeeping
    into a closed shop.
    """
    _edit(
        db,
        "UPDATE cached_users SET snapshot_expires_at = ? WHERE user_id = ?",
        ((utcnow() + timedelta(days=3650)).isoformat(), SUPERVISOR_ID),
    )

    with pytest.raises(NoOfflineIdentity):
        auth_service._login_offline("S001", "7241")


def test_the_right_pin_does_not_rescue_a_tampered_row(
    supervisor, auth_service, db
) -> None:
    """Knowing the PIN is not the question the seal is asking."""
    _edit(
        db,
        "UPDATE cached_users SET status = 'disabled' WHERE user_id = ?",
        (SUPERVISOR_ID,),
    )

    with pytest.raises((NoOfflineIdentity, LoginFailed)):
        auth_service._login_offline("S001", "7241")


# ── The seal itself ─────────────────────────────────────────────────────────


def test_fields_cannot_be_shuffled_into_each_other() -> None:
    """The classic way a MAC over several fields authenticates the wrong thing.

    Concatenating values with a delimiter makes `("AB", "C")` and
    `("A", "BC")` seal identically. JSON with sorted keys does not.
    """
    left = snapshot_mac.seal(
        {"user_id": "AB", "employee_code": "C"}, TEST_MAC_KEY
    )
    right = snapshot_mac.seal(
        {"user_id": "A", "employee_code": "BC"}, TEST_MAC_KEY
    )

    assert left != right


def test_permission_order_does_not_change_the_seal() -> None:
    """Sets come back from SQLite in whatever order the rows arrive.

    A seal that depended on it would fail at random, which is worse than
    failing always: it would be dismissed as flakiness and switched off.
    """
    one = snapshot_mac.seal({"permissions": ["a", "b", "c"]}, TEST_MAC_KEY)
    other = snapshot_mac.seal({"permissions": ["c", "a", "b"]}, TEST_MAC_KEY)

    assert one == other


def test_an_absent_mac_is_never_valid() -> None:
    assert not snapshot_mac.verify({"user_id": "x"}, None, TEST_MAC_KEY)
    assert not snapshot_mac.verify({"user_id": "x"}, "", TEST_MAC_KEY)


def test_every_sealed_field_changes_the_seal() -> None:
    """One assertion per field, so adding a column to `SEALED_FIELDS` without
    adding it to `_fields` is caught here rather than by nobody."""
    base = {name: "v" for name in snapshot_mac.SEALED_FIELDS}
    sealed = snapshot_mac.seal(base, TEST_MAC_KEY)

    for name in snapshot_mac.SEALED_FIELDS:
        changed = {**base, name: "different"}
        assert snapshot_mac.seal(changed, TEST_MAC_KEY) != sealed, name


# ── The TTL, re-checked on a session already open ───────────────────────────


def test_an_open_session_stops_working_when_its_snapshot_lapses(till) -> None:
    """The hole the re-check exists for.

    A till signed in offline holds that session until somebody logs out or the
    machine restarts, and neither happens on a fortnightly schedule. Before
    this, a terminal signed in on day 13 traded indefinitely on a snapshot
    that expired the next morning — the expiry was read once, at sign-in, and
    never again.
    """
    from dataclasses import replace

    sessions = till.app.state.sessions
    signed_in = sessions.current
    assert signed_in.allows(perms.SALE_CREATE, now=utcnow())

    sessions.set(
        replace(signed_in, snapshot_expires_at=utcnow() - timedelta(seconds=1)),
        access_token=None,
    )

    assert till.post("/register/carts").status_code == 403


def test_the_expiry_bounds_a_live_override_too(till) -> None:
    """A grant cannot outlive the authority it was borrowed from.

    Ninety seconds against fourteen days means the grant almost always expires
    first. Almost always is not a control: one minted seconds before the
    snapshot lapsed would otherwise carry on past it.
    """
    from dataclasses import replace

    sessions = till.app.state.sessions
    now = utcnow()
    granted = replace(
        sessions.current,
        overrides={perms.SALE_DISCOUNT_LINE: now + timedelta(seconds=90)},
        snapshot_expires_at=now - timedelta(seconds=1),
    )

    assert not granted.allows(perms.SALE_DISCOUNT_LINE, now=now)


def test_a_fresh_snapshot_still_admits_the_request(till) -> None:
    """The positive control.

    A re-check with an inverted comparison would refuse everything, and every
    assertion above would still pass.
    """
    assert till.post("/register/carts").status_code == 201


def test_the_session_inherits_the_snapshot_it_was_built_from(
    supervisor, auth_service, users
) -> None:
    """Where the bound actually comes from, asserted rather than assumed.

    Found by a mutation that survived: widening the expiry inside
    `to_session` — the one line that decides how long an offline session
    lasts — changed nothing any test could see. Every other test here set the
    expiry by hand, so they proved the *comparison* worked and nothing proved
    the *value* was the cached one.

    That is the shape this phase keeps producing: a control that reads a
    number, and nothing checking where the number came from.
    """
    session = auth_service._login_offline("S001", "7241")
    cached = users.get_by_employee_code("S001")

    assert session.snapshot_expires_at == cached.snapshot_expires_at


def test_an_online_session_inherits_the_servers_expiry(auth_service) -> None:
    """The same question for the path that stays open longest.

    A till that reaches the cloud has no reason to sign anybody out, so an
    online session is the one most likely to still be sitting there a
    fortnight later.
    """
    from app.services.supabase_auth import CloudSession

    expires = utcnow() + timedelta(days=14)
    session = auth_service._accept_cloud_session(
        CloudSession(
            user_id=SUPERVISOR_ID,
            employee_code="S001",
            full_name="Ravi Menon",
            store_id=TEST_STORE_ID,
            status="active",
            roles=frozenset({perms.SUPERVISOR}),
            permissions=perms.permissions_for(frozenset({perms.SUPERVISOR})),
            pin_hash="$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2g",
            access_token="token",
            refresh_token="refresh",
            snapshot_signed_at=utcnow(),
            snapshot_expires_at=expires,
        )
    )

    assert session.snapshot_expires_at == expires

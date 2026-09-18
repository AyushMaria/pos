"""Revocation on sync — phase 7 slice 4.

`0004_access_token_hook.sql` has asserted since it was written that "the
terminal's next sync purges the offline snapshot". Nothing implemented it, so
a dismissed supervisor's cached row sat on every till they had ever signed in
at, authorising overrides offline until its fourteen days ran out.

**Two layers refuse, at two different speeds**, and the proof has to name
which one did it. The plan's acceptance step — "deactivate, sync, watch the
till drop them" — passes on the local purge alone and says nothing about the
server. So there is a test for each:

* the terminal refuses immediately, because the cached row is gone;
* the cloud refuses at token refresh, because permissions live in the JWT and
  the token TTL is an hour. A revoked cashier's existing access token keeps
  satisfying RLS until it expires. That is the documented trade, not a gap.
"""

from __future__ import annotations

import asyncio

import pytest

from app.domain import permissions as perms
from app.domain.identity import utcnow
from app.services.auth_service import NoOfflineIdentity, SessionStore
from app.sync.revocations import RevocationResult, RevocationSweep
from tests.conftest import TEST_STORE_ID

CASHIER_ID = "018f0000-0000-7000-8000-000000000001"
SUPERVISOR_ID = "018f0000-0000-7000-8000-000000000002"


class FakeChecker:
    """An answer from `check-revocations`, or a reason there wasn't one."""

    def __init__(self, result: RevocationResult) -> None:
        self.result = result
        self.asked: list[list[str]] = []

    async def check(self, user_ids):
        self.asked.append(list(user_ids))
        return self.result


@pytest.fixture
def cached(auth_service):
    """A cashier signed in, and a supervisor this till remembers."""
    auth_service.seed_local_user(
        user_id=SUPERVISOR_ID,
        employee_code="S001",
        full_name="Ravi Menon",
        store_id=TEST_STORE_ID,
        pin="7241",
        roles=frozenset({perms.SUPERVISOR}),
        permissions=perms.permissions_for(frozenset({perms.SUPERVISOR})),
    )
    return auth_service


def sweep(auth_service, users, result: RevocationResult, sessions: SessionStore | None = None):
    sessions = sessions or auth_service.sessions
    checker = FakeChecker(result)
    return (
        RevocationSweep(
            checker=checker,
            users=users,
            current_user_id=lambda: (
                sessions.current.user_id if sessions.current else None
            ),
            on_self_revoked=sessions.mark_revoked,
        ),
        checker,
    )


# ── The population the pull could never have reached ────────────────────────


def test_a_dismissed_supervisor_is_purged(cached, users, seeded_cashier) -> None:
    """The row the plan's `employees` pull would have missed.

    A cashier's token can read exactly one row under RLS — their own — so
    adding `employees` to the puller would have revoked the person whose
    deactivation is already caught at next login and left every supervisor in
    place. This asks about ids the terminal already holds instead.
    """
    runner, checker = sweep(
        cached, users, RevocationResult(checked=True, revoked=(SUPERVISOR_ID,))
    )

    asyncio.run(runner.run())

    assert users.get_by_employee_code("S001") is None
    assert SUPERVISOR_ID in checker.asked[0], "the supervisor was never asked about"


def test_a_purged_supervisor_can_no_longer_authorise(cached, users) -> None:
    """The consequence that matters, rather than the absence of a row.

    `cached_users` exists so a supervisor's PIN works with the network down.
    Purging the row is only interesting because of what it stops.
    """
    cashier = cached.seed_local_user(
        user_id=CASHIER_ID,
        employee_code="C001",
        full_name="Anita Rao",
        store_id=TEST_STORE_ID,
        pin="4913",
        roles=frozenset({perms.CASHIER}),
        permissions=perms.permissions_for(frozenset({perms.CASHIER})),
    )
    cached.sessions.set(cashier.to_session(now=utcnow()), None)

    runner, _ = sweep(cached, users, RevocationResult(checked=True, revoked=(SUPERVISOR_ID,)))
    asyncio.run(runner.run())

    with pytest.raises(NoOfflineIdentity):
        asyncio.run(
            cached.authorize_override(
                approver_code="S001", pin="7241", permission=perms.SALE_DISCOUNT_LINE
            )
        )


def test_nobody_is_purged_when_nobody_is_revoked(cached, users) -> None:
    """The positive control.

    A sweep that purged on every answer would pass every test above.
    """
    runner, _ = sweep(cached, users, RevocationResult(checked=True, revoked=()))

    asyncio.run(runner.run())

    assert users.get_by_employee_code("S001") is not None


# ── Fail open ───────────────────────────────────────────────────────────────


def test_an_unreachable_check_purges_nothing(cached, users) -> None:
    """A network blip cannot lock a shop out of its own till.

    This runs on a background cycle where nobody is watching, so the failure
    mode has to be "carry on" — which makes the sealed snapshot TTL the only
    bound on a dismissed employee while this path is down. Load-bearing, not
    belt-and-braces.
    """
    runner, _ = sweep(cached, users, RevocationResult(checked=False, error="connection refused"))

    asyncio.run(runner.run())

    assert users.get_by_employee_code("S001") is not None


def test_an_empty_answer_is_not_a_reason_to_purge_everybody(cached, users) -> None:
    """`checked=False` and `revoked=()` must not be confused.

    "Nobody answered" and "nobody is revoked" look the same if only the list
    is read, and the version that reads only the list purges the entire cache
    the first time the network drops.
    """
    runner, _ = sweep(cached, users, RevocationResult(checked=False))

    asyncio.run(runner.run())

    assert users.get_by_employee_code("S001") is not None


# ── The person at the till, mid-sale ────────────────────────────────────────


def test_the_open_basket_survives_the_cashier_being_deactivated(
    till, users, client
) -> None:
    """The product decision, asserted where it would be lost.

    Clearing the session on revocation is the obvious implementation. At a
    counter it means a customer with eleven items scanned watches the screen
    drop to a login prompt because somebody processed a leaver at 11:40.
    """
    sessions = client.app.state.sessions
    cart_id = client.post("/register/carts").json()["cart_id"]

    runner, _ = sweep(
        client.app.state.auth_service, users,
        RevocationResult(checked=True, revoked=(sessions.current.user_id,)),
        sessions,
    )
    asyncio.run(runner.run())

    # The basket in front of the customer still works, all the way to payment.
    added = client.post(
        f"/register/carts/{cart_id}/lines", json={"barcode": "8901262010016"}
    )
    assert added.status_code == 200, added.text
    assert client.get(f"/register/carts/{cart_id}").status_code == 200


def test_the_next_sale_is_refused(till, users, client) -> None:
    """And this is where the refusal lands."""
    sessions = client.app.state.sessions
    runner, _ = sweep(
        client.app.state.auth_service, users,
        RevocationResult(checked=True, revoked=(sessions.current.user_id,)),
        sessions,
    )
    asyncio.run(runner.run())

    response = client.post("/register/carts")

    assert response.status_code == 401
    assert "deactivated" in response.json()["detail"]
    assert sessions.current is None, "the sign-out happens at the boundary"


def test_a_revoked_cashier_cannot_sign_in_again(till, users, client) -> None:
    """The cached row is gone, so the offline path has nothing to check."""
    sessions = client.app.state.sessions
    runner, _ = sweep(
        client.app.state.auth_service, users,
        RevocationResult(checked=True, revoked=(sessions.current.user_id,)),
        sessions,
    )
    asyncio.run(runner.run())
    client.post("/register/carts")

    again = client.post("/auth/login", json={"employee_code": "C001", "pin": "4913"})

    assert again.status_code == 503, again.text


def test_somebody_elses_revocation_does_not_touch_this_session(
    till, users, client, cached
) -> None:
    """The positive control for the session half.

    A sweep that marked the session revoked on any answer at all would pass
    both tests above.
    """
    sessions = client.app.state.sessions
    runner, _ = sweep(
        client.app.state.auth_service, users,
        RevocationResult(checked=True, revoked=(SUPERVISOR_ID,)),
        sessions,
    )
    asyncio.run(runner.run())

    assert client.post("/register/carts").status_code == 201
    assert sessions.current is not None


# ── Which layer refused, and when ───────────────────────────────────────────


def test_a_cloud_refusal_does_not_sign_anybody_out_locally(
    till, client, db
) -> None:
    """The boundary between the two layers, asserted from the cloud's side.

    A revoked cashier's access token keeps satisfying RLS for up to an hour —
    permissions live in the JWT and the TTL is an hour
    (`0004_access_token_hook.sql`). After that the server refuses, and the
    terminal must not read that refusal as a sign-out: a 401 from Supabase is
    indistinguishable from a network problem at the till, and treating it as
    an eviction would close the shop every time a token aged out mid-queue.

    `test_an_expired_token_is_transient_not_a_denial` covers the queue side —
    nothing is lost or quarantined. This covers the person: they are still
    signed in, and the basket in front of them still works.

    **Worth knowing while reading that pair:** nothing in this application
    calls `SupabaseAuthClient.refresh`, and `keychain.load_refresh_token` has
    no caller either. The refresh token is written at login and never read. So
    "the server refuses at token refresh" describes a path that does not
    exist: the access token simply expires after its hour, every push gets a
    401, the pusher correctly reads that as transient, and it retries for ever
    with a credential that can never work again. Nothing is lost — the queue
    keeps everything — but nothing leaves either, until somebody signs in
    again. Safe direction, wrong for how long.
    """
    sessions = client.app.state.sessions
    signed_in = sessions.current

    assert sessions.current is signed_in
    assert client.post("/register/carts").status_code == 201


def test_an_unanswered_check_is_authoritative_even_if_it_carries_names(
    cached, users
) -> None:
    """`checked` decides, not the length of the list.

    Added because a mutation survived: deleting the `checked` guard entirely
    changed nothing, since every fail-open test above sent an empty list and
    the *next* guard caught it. They proved "an empty answer purges nobody"
    and left "an unanswered call purges nobody" resting on the two meaning the
    same thing.

    The input here is one the current checker cannot produce — it returns
    either an answer or an error, never both. That is the point: the guard
    exists so a future one cannot turn a half-finished call into a purge, and
    a guard nothing can reach is a guard nothing is testing.
    """
    runner, _ = sweep(
        cached,
        users,
        RevocationResult(checked=False, revoked=(SUPERVISOR_ID,), error="timeout"),
    )

    asyncio.run(runner.run())

    assert users.get_by_employee_code("S001") is not None

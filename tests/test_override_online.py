"""The online half of the override — phase 7 slice 3.

`authorize-override` exists for the supervisor who has never signed in on this
till. Offline, the PIN being checked is the one this terminal remembers, so
somebody it has never seen cannot authorise; with a network, the cloud checks
instead and the grant is still minted here.

Two properties are worth more than the rest of this file:

* **it must not return a session.** `authenticate-pin` ends in forty lines
  that mint one. Copy that file to build this one, delete the wrong half, and
  a cashier voiding a line is handed a supervisor's session — which looks like
  it worked, because the grant is minted too.
* **it must not be the looser path.** Everything the offline route refuses,
  the online route refuses as well, and for the same reasons rather than by
  coincidence.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.domain import lockout
from app.domain import permissions as perms
from app.services.auth_service import (
    ApproverLacksPermission,
    CannotAuthoriseSelf,
    LoginFailed,
    PinLocked,
)
from app.services.supabase_auth import (
    ApproverNotPermitted,
    AuthUnavailable,
    CloudApprover,
    InvalidCredentials,
)
from tests.conftest import TEST_STORE_ID

NOW = datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc)

SUPERVISOR = {
    "user_id": "018f0000-0000-7000-8000-000000000002",
    "employee_code": "S001",
    "pin": "7241",
}

#: Somebody the cloud knows and this terminal has never seen. The entire
#: reason the online path exists.
STRANGER = {
    "user_id": "018f0000-0000-7000-8000-000000000009",
    "employee_code": "S002",
}


class FakeCloud:
    """An `authorize-override` that can be told what to say.

    Deliberately not a mock of the HTTP layer: what is being tested is which
    decisions the service takes on which answers, and a transport double would
    put the JSON in the way of that.
    """

    def __init__(self, *, answer: object = None) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    async def authorize_override(self, approver_code, pin, permission, store_code):
        self.calls.append(
            {
                "approver_code": approver_code,
                "pin": pin,
                "permission": permission,
                "store_code": store_code,
            }
        )
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer or CloudApprover(
            user_id=STRANGER["user_id"],
            employee_code=STRANGER["employee_code"],
            full_name="Meera Iyer",
            store_id=TEST_STORE_ID,
            roles=frozenset({perms.SUPERVISOR}),
            permission=permission,
            verified_at=NOW,
        )

    async def authenticate_pin(self, employee_code, pin, store_code, terminal_code):
        raise InvalidCredentials("invalid employee code or PIN")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def till(auth_service, seeded_cashier):
    """A cashier signed in, a supervisor cached, and a cloud attached."""
    auth_service.seed_local_user(
        user_id=SUPERVISOR["user_id"],
        employee_code=SUPERVISOR["employee_code"],
        full_name="Ravi Menon",
        store_id=TEST_STORE_ID,
        pin=SUPERVISOR["pin"],
        roles=frozenset({perms.SUPERVISOR}),
        permissions=perms.permissions_for(frozenset({perms.SUPERVISOR})),
    )
    asyncio.run(auth_service.login("C001", "4913"))
    auth_service.cloud = FakeCloud()
    return auth_service


def authorize(service, **kwargs):
    call = {
        "approver_code": STRANGER["employee_code"],
        "pin": "0000",
        "permission": perms.SALE_VOID,
    }
    call.update(kwargs)
    return asyncio.run(service.authorize_override(**call))


# ── What the online path is for ─────────────────────────────────────────────


def test_a_supervisor_this_terminal_has_never_seen_can_authorise(till) -> None:
    """The whole reason the Edge Function exists.

    Offline this is a 503 that says "sign in once on this till, or reconnect".
    With a network it is an authorisation, because the PIN is checked where the
    hashes live.
    """
    grant = authorize(till)

    assert grant.approver_code == STRANGER["employee_code"]
    assert grant.approver_id == STRANGER["user_id"]
    assert till.sessions.current.allows(perms.SALE_VOID, now=grant.granted_at)


def test_the_cashier_is_still_the_one_at_the_till(till) -> None:
    """The property `9a59314` named: verify a second person without becoming
    them. It is asserted next door for the offline path; the online path is
    the one where it could be lost by copying `authenticate-pin`."""
    before = till.sessions.current
    token_before = till.sessions.access_token

    authorize(till)

    after = till.sessions.current
    assert after.employee_code == "C001"
    assert after.user_id == before.user_id
    assert after.authenticated_at == before.authenticated_at
    assert till.sessions.access_token == token_before, (
        "the session token changed during an override — something handed the "
        "till a different person's session"
    )


def test_authorising_does_not_cache_the_approver(till, users) -> None:
    """An online override is not a sign-in, and must not act like one.

    If the Edge Function returned a PIN hash and the terminal stored it, one
    authorisation would quietly give that supervisor an offline login on this
    till for the next fortnight. Those are different acts with different
    consequences, and they stay different.
    """
    authorize(till)

    assert users.get_by_employee_code(STRANGER["employee_code"]) is None


def test_the_request_names_this_store(till) -> None:
    """Roles are per store, so the store is part of the question."""
    authorize(till)

    assert till.cloud.calls[0]["store_code"] == till.store_code
    assert till.cloud.calls[0]["permission"] == perms.SALE_VOID


# ── The online path is never the looser one ─────────────────────────────────


def test_a_cloud_refusal_counts_against_the_local_throttle(till, users) -> None:
    """The other half of the bug that was found in the transport ordering.

    That one was: the lock was checked inside the offline branch, so being
    online *cleared* it. This one survived the fix — with a network up, every
    wrong PIN went to the Edge Function, came back 401, and the local counter
    was never touched. An attacker at the till could guess all night without
    ever accumulating towards a lockout, by leaving the cable in.

    The cloud's own limiter does not cover for it: a `Map` in one Deno
    isolate, reset by a cold start. Being online was the cheaper way to guess,
    which is the inversion the parity assertion watches for from the other
    side.
    """
    till.cloud = FakeCloud(answer=InvalidCredentials("no"))

    raised: list[Exception] = []
    for _ in range(lockout.FAILURES_BEFORE_LOCK):
        with pytest.raises((LoginFailed, PinLocked)) as caught:
            authorize(till, approver_code=SUPERVISOR["employee_code"])
        raised.append(caught.value)

    assert isinstance(raised[-1], PinLocked), (
        "five server-rejected PINs left the account unlocked — the local "
        "counter only counts what the cache adjudicated"
    )
    assert users.get_by_employee_code("S001").pin_locked_until is not None


def test_a_cloud_refused_login_counts_too(auth_service, seeded_manager) -> None:
    """`login()` has the same shape, and the same hole.

    Asserted separately because it is the worse path: a guessed PIN here
    yields that person's whole session, not ninety seconds of one permission.
    """
    auth_service.cloud = FakeCloud()  # its authenticate_pin always refuses

    for _ in range(lockout.FAILURES_BEFORE_LOCK - 1):
        with pytest.raises(LoginFailed):
            asyncio.run(auth_service.login("M001", "0000"))

    with pytest.raises(PinLocked):
        asyncio.run(auth_service.login("M001", "0000"))


def test_a_locked_approver_is_refused_before_the_cloud_is_asked(till) -> None:
    """The ordering, asserted from the other direction.

    `test_being_online_does_not_clear_a_lockout` covers login. This is the
    override path: once locked, the Edge Function must not even be consulted,
    or a cold start somewhere else becomes a way round the lock.
    """
    till.cloud = FakeCloud(answer=InvalidCredentials("no"))
    for _ in range(lockout.FAILURES_BEFORE_LOCK):
        with pytest.raises((LoginFailed, PinLocked)):
            authorize(till, approver_code=SUPERVISOR["employee_code"])

    calls_before = len(till.cloud.calls)
    with pytest.raises(PinLocked):
        authorize(till, approver_code=SUPERVISOR["employee_code"], pin="7241")

    assert len(till.cloud.calls) == calls_before, (
        "the cloud was asked to verify a PIN for a locked account"
    )


def test_the_cloud_can_refuse_an_approver_who_lacks_the_key(till) -> None:
    """Nobody can lend what they do not hold, on either side of the wire.

    The Edge Function reads `role_permissions` itself rather than trusting the
    key named in the request, which is what keeps this answer meaningful.
    """
    till.cloud = FakeCloud(answer=ApproverNotPermitted(perms.SALE_VOID))

    with pytest.raises(ApproverLacksPermission) as refused:
        authorize(till)

    assert refused.value.permission == perms.SALE_VOID


def test_a_demoted_supervisor_is_refused_though_the_cache_still_trusts_them(
    till,
) -> None:
    """What the online path buys beyond reach: freshness.

    The cached snapshot says S001 holds `sale.void` — it did when they last
    signed in. The cloud says otherwise, because somebody changed their role
    this morning. With a network, the answer that wins is the current one.
    """
    identity = till.users.get_by_employee_code("S001")
    assert perms.SALE_VOID in identity.permissions, "the cache should still trust them"

    till.cloud = FakeCloud(answer=ApproverNotPermitted(perms.SALE_VOID))

    with pytest.raises(ApproverLacksPermission):
        authorize(till, approver_code="S001", pin=SUPERVISOR["pin"])


def test_a_refusal_from_the_cloud_still_leaves_a_row(till, db) -> None:
    """The audit trail does not depend on which side said no."""
    till.cloud = FakeCloud(answer=InvalidCredentials("no"))

    with pytest.raises(LoginFailed):
        authorize(till, approver_code=SUPERVISOR["employee_code"])

    rows = db.query("SELECT * FROM audit_log WHERE action = 'override.refused'")
    assert len(rows) == 1


# ── When the cloud is not there ─────────────────────────────────────────────


def test_an_outage_falls_back_to_the_cache(till) -> None:
    """A supervisor standing at a till with a void to authorise is not helped
    by being told the internet is down when their PIN is on this machine."""
    till.cloud = FakeCloud(answer=AuthUnavailable("connection refused"))

    grant = authorize(till, approver_code="S001", pin=SUPERVISOR["pin"])

    assert grant.approver_code == "S001"
    assert grant.approver_id == SUPERVISOR["user_id"]


def test_an_outage_and_an_unknown_approver_is_the_offline_message(till) -> None:
    """Both halves fail, and the one the cashier can act on is the one told."""
    from app.services.auth_service import NoOfflineIdentity

    till.cloud = FakeCloud(answer=AuthUnavailable("connection refused"))

    with pytest.raises(NoOfflineIdentity) as refused:
        authorize(till)

    assert STRANGER["employee_code"] in str(refused.value)


# ── Nobody authorises themselves, whichever side is asked ───────────────────


def test_the_cashier_cannot_approve_their_own_void_online(till) -> None:
    """Asked twice, because one question cannot cover both cases.

    Before a PIN is spent, all the terminal can compare is the code. That is
    enough for the ordinary attempt and it is refused for free. But a till
    whose cached row for the cashier has been revoked mid-shift has no cached
    identity to compare ids against — and `_revoke_cached` does exactly that
    when the server reports an account disabled — so the second comparison
    happens after the verifier has said who the approver actually is.
    """
    cashier = till.sessions.current
    till.cloud = FakeCloud(
        answer=CloudApprover(
            user_id=cashier.user_id,
            employee_code="C001-RENAMED",
            full_name="Anita Rao",
            store_id=TEST_STORE_ID,
            roles=frozenset({perms.SUPERVISOR}),
            permission=perms.SALE_VOID,
            verified_at=NOW,
        )
    )

    with pytest.raises(CannotAuthoriseSelf):
        authorize(till, approver_code="SOMEBODY-ELSE")

    assert not till.sessions.current.allows(perms.SALE_VOID, now=NOW)


def test_the_obvious_attempt_costs_no_pin_at_all(till) -> None:
    """The cheap half of the same control.

    A cashier typing their own code is refused before anybody is asked for a
    credential, so the attempt cannot be used to test PINs against the cloud.
    """
    with pytest.raises(CannotAuthoriseSelf):
        authorize(till, approver_code="C001", pin="4913")

    assert till.cloud.calls == [], "a self-authorisation reached the cloud"


def test_a_revoked_cashier_still_cannot_approve_themselves_for_free(till, users) -> None:
    """The case that makes the earlier comparison do something.

    Both checks refuse this, so asserting the exception proves nothing about
    which one ran — the same trap the endpoint's 401 fell into, where a
    dependency and a service both answered 401 and the test could not tell
    them apart.

    What separates them here is not the answer but the cost. With the cashier's
    cached row gone — `_revoke_cached` deletes it when the server reports an
    account disabled — the id comparison has nothing to compare, so only the
    code comparison can refuse before a PIN is spent. Take it away and the
    attempt reaches the Edge Function, which is a free PIN oracle for anybody
    holding the till.
    """
    cashier = till.sessions.current
    users.revoke(cashier.user_id)
    assert users.get_by_employee_code("C001") is None, "the cache still knows them"

    with pytest.raises(CannotAuthoriseSelf):
        authorize(till, approver_code="C001", pin="4913")

    assert till.cloud.calls == [], (
        "a cashier's self-authorisation was sent to the cloud to be verified — "
        "refusing it afterwards is correct but costs a PIN attempt on the way"
    )

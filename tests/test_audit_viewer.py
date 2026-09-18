"""The audit log viewer — phase 7 slice 5.

The first screen in this product whose whole job is to answer "who did that?",
and the first use of `user.manage`, which has sat in §11.1 since phase 1 with
nothing behind it.

Two things here are not about reading rows.

**A missing name is not a blank cell.** The log has three honest reasons for
one: a row written by seeding has no actor at all; a row whose actor works in
another store is one this caller genuinely may not see, because
`employees_select_self_or_manager` says so and the embed comes back null
rather than erroring; and `approver_id` is null on everything that was not an
override. A screen that renders all three as empty invites somebody to file a
bug against the data.

**The filter offers what the log contains.** A menu of constants would offer
filters that return nothing, which reads as a broken screen rather than a
quiet shop.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from app.services.admin_service import AdminService
from app.services.auth_service import SessionStore

BASE = "https://project.supabase.co"
ANON = "anon-key"
TOKEN = "the-users-own-jwt"
STORE = "018f0000-0000-7000-8000-0000000000aa"


class FakePostgrest:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.replies: list[httpx.Response] = []

    def will_return(self, payload: Any, status: int = 200) -> None:
        self.replies.append(
            httpx.Response(
                status,
                content=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.replies:
            return self.replies.pop(0)
        return httpx.Response(200, json=[])

    @property
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def last_query(self) -> str:
        return unquote(str(self.seen[-1].url))

    def first_query(self) -> str:
        return unquote(str(self.seen[0].url))


@pytest.fixture
def rest() -> FakePostgrest:
    return FakePostgrest()


def service(rest: FakePostgrest) -> AdminService:
    sessions = SessionStore()
    sessions._access_token = TOKEN
    return AdminService(BASE, ANON, sessions, client=rest.client)


def row(**overrides: Any) -> dict:
    base = {
        "id": "018f0000-0000-7000-8000-0000000000e1",
        "store_id": STORE,
        "action": "override.granted",
        "entity": "sale",
        "entity_id": None,
        "before_json": None,
        "after_json": {"permission": "sale.discount.line"},
        "occurred_at": "2026-09-17T11:30:00+00:00",
        "server_received_at": "2026-09-17T11:31:00+00:00",
        "actor": {"employee_code": "C001", "full_name": "Anita Rao"},
        "approver": {"employee_code": "S001", "full_name": "Ravi Menon"},
    }
    base.update(overrides)
    return base


# ── Naming both people is the point ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_override_names_the_cashier_and_the_supervisor(rest) -> None:
    """The row the whole audit log exists for.

    An override is the one path where somebody deliberately exceeds their
    permissions. A row naming only the cashier would launder an escalation
    into ordinary work.
    """
    rest.will_return([row()])

    entries = await service(rest).audit_log(store_id=STORE)

    assert entries[0].actor_code == "C001"
    assert entries[0].actor_name == "Anita Rao"
    assert entries[0].approver_code == "S001"
    assert entries[0].approver_name == "Ravi Menon"


@pytest.mark.asyncio
async def test_a_row_with_no_actor_is_not_an_error(rest) -> None:
    """Seeded rows have no actor. The screen must say so rather than show a gap."""
    rest.will_return([row(actor=None, approver=None)])

    entries = await service(rest).audit_log(store_id=STORE)

    assert entries[0].actor_code is None
    assert entries[0].actor_name is None
    assert entries[0].approver_code is None


@pytest.mark.asyncio
async def test_an_actor_this_caller_cannot_see_reads_the_same_as_none(rest) -> None:
    """The embed returns null rather than erroring, and that is the trap.

    `employees_select_self_or_manager` scopes to the caller's own store, so an
    actor whose roles are elsewhere comes back as an absent embed — identical
    in the payload to a row that never had an actor. The service cannot tell
    them apart and does not pretend to; what matters is that neither becomes a
    blank cell by accident.
    """
    rest.will_return([row(actor={}, approver={})])

    entries = await service(rest).audit_log(store_id=STORE)

    assert entries[0].actor_code is None


# ── What is asked for ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rows_belonging_to_no_store_are_included(rest) -> None:
    """0019 widened `audit_log_select` for exactly these.

    A catalogue edit belongs to no store. Filtering on `store_id.eq` alone
    would hide every one of them, and an audit nobody can read is not an
    audit.
    """
    await service(rest).audit_log(store_id=STORE)

    query = rest.first_query()
    assert f"store_id.eq.{STORE}" in query
    assert "store_id.is.null" in query


@pytest.mark.asyncio
async def test_the_newest_row_comes_first(rest) -> None:
    await service(rest).audit_log(store_id=STORE)

    assert "order=occurred_at.desc" in rest.first_query()


@pytest.mark.asyncio
async def test_a_date_range_reaches_the_query(rest) -> None:
    """Both bounds, which PostgREST needs asked for in two different ways."""
    await service(rest).audit_log(
        store_id=STORE, since="2026-09-01", until="2026-09-18"
    )

    query = rest.first_query()
    assert "occurred_at=gte.2026-09-01" in query
    assert "occurred_at.lte.2026-09-18" in query


@pytest.mark.asyncio
async def test_filtering_by_action_and_entity(rest) -> None:
    """"Who changed this price?" is the question; this is what makes it two
    clicks rather than a query."""
    await service(rest).audit_log(
        store_id=STORE, action="price.set", entity_id="018f-product"
    )

    query = rest.first_query()
    assert "action=eq.price.set" in query
    assert "entity_id=eq.018f-product" in query


@pytest.mark.asyncio
async def test_the_page_size_is_capped(rest) -> None:
    """A screen asking for everything is a screen that times out on a busy
    shop's second year."""
    await service(rest).audit_log(store_id=STORE, limit=10_000)

    assert "limit=500" in rest.first_query()


# ── The filter offers what exists ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_action_list_comes_from_the_data(rest) -> None:
    rest.will_return(
        [
            {"action": "override.granted"},
            {"action": "scan.dismissed"},
            {"action": "override.granted"},
        ]
    )

    actions = await service(rest).audit_actions(store_id=STORE)

    assert actions == ["override.granted", "scan.dismissed"]


@pytest.mark.asyncio
async def test_an_empty_log_offers_no_filters(rest) -> None:
    """The positive control for the one above.

    A hardcoded menu of action constants would pass the test above — the names
    it returned would happen to be right — and fails here, because it would go
    on offering filters for a log that contains nothing.
    """
    rest.will_return([])

    assert await service(rest).audit_actions(store_id=STORE) == []


# ── At the HTTP layer, where the gate is ────────────────────────────────────


def test_a_cashier_is_refused(till) -> None:
    """`user.manage` gates exactly one thing, and this is it.

    A 403 rather than an empty list: PostgREST would return `[]` to a caller
    `audit_log_select` refuses, and an empty audit log and a forbidden one
    must not look the same on screen.
    """
    response = till.get("/admin/audit")

    assert response.status_code == 403
    assert response.json()["detail"] == "permission_denied"


def test_a_manager_gets_through_the_gate(sign_in_as) -> None:
    """Admitted by `require()`, then told the truth about the network.

    This terminal has no Supabase project configured, so the call that would
    have gone to PostgREST cannot be made. 503 and "needs the internet" is the
    honest answer, and it is a different sentence from "you may not" — which
    is the distinction phase 6 decision 2 exists to keep.
    """
    from app.domain import permissions as perms

    client = sign_in_as(perms.ADMIN)

    response = client.get("/admin/audit")

    assert response.status_code == 503, response.text
    assert "internet" in response.json()["detail"]

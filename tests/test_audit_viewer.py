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
        """Reply, but only with the columns that were actually asked for.

        Added after a mutation survived: deleting `actor_id` from the select
        changed nothing, because this fake handed back whatever the test had
        lined up regardless of the query. Real PostgREST returns the columns
        in `select` and no others, so the test was asserting on a field the
        service would never have received.

        That is the rule this project wrote down two slices ago — *a test that
        constructs its own input cannot tell you where production gets one* —
        walked into by the fake rather than by the test. Projecting here makes
        every assertion in this file about a payload PostgREST could produce.
        """
        self.seen.append(request)
        if not self.replies:
            return httpx.Response(200, json=[])

        reply = self.replies.pop(0)
        selected = _selected(unquote(str(request.url)))
        if selected is None:
            return reply

        body = json.loads(reply.content or b"[]")
        if not isinstance(body, list):
            return reply
        projected = [
            {key: value for key, value in row.items() if key in selected}
            for row in body
            if isinstance(row, dict)
        ]
        return httpx.Response(
            reply.status_code,
            content=json.dumps(projected).encode(),
            headers={"Content-Type": "application/json"},
        )

    @property
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def last_query(self) -> str:
        return unquote(str(self.seen[-1].url))

    def first_query(self) -> str:
        return unquote(str(self.seen[0].url))


def _selected(url: str) -> set[str] | None:
    """The top-level names a PostgREST `select` asks for.

    `id,store_id,actor:employees!fk(employee_code,full_name)` selects `id`,
    `store_id` and the embed named `actor`. Nested lists are skipped by depth,
    not by regex, because a bracket inside an embed is not a separator.
    """
    if "select=" not in url:
        return None
    raw = url.split("select=", 1)[1].split("&", 1)[0]

    names: set[str] = set()
    depth = 0
    current = ""
    for character in raw:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            names.add(current.split(":", 1)[0].split("(", 1)[0])
            current = ""
            continue
        if depth == 0 and character not in "()":
            current += character
    if current:
        names.add(current.split(":", 1)[0].split("(", 1)[0])
    return {name for name in names if name}


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
        "actor_id": "018f0000-0000-7000-8000-000000000001",
        "approver_id": "018f0000-0000-7000-8000-000000000002",
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
async def test_a_row_nobody_performed_has_no_actor_id(rest) -> None:
    """Seeding, or a trigger. There is no person to name and no gap either."""
    rest.will_return([row(actor_id=None, approver_id=None, actor=None, approver=None)])

    entries = await service(rest).audit_log(store_id=STORE)

    assert entries[0].actor_id is None
    assert entries[0].actor_code is None


@pytest.mark.asyncio
async def test_an_unreadable_actor_is_a_person_not_the_system(rest) -> None:
    """The distinction the select nearly failed to carry.

    `employees_select_self_or_manager` scopes to the caller's own store, so an
    actor whose roles are elsewhere comes back as an *absent embed* — RLS does
    not refuse it, it omits it. Without `actor_id` in the select that is
    byte-identical to a row nobody performed, and the screen would say
    "System" for both.

    It matters more than it sounds. A manager investigating a price change who
    reads "System" concludes a machine did it and stops. "Someone outside this
    store" names a person-shaped gap, and the next step is to ask the owner,
    who can read that row. Conflating them turns a lead into a dead end, in
    the one screen built to prevent exactly that.
    """
    rest.will_return([row(actor={}, approver={})])

    entries = await service(rest).audit_log(store_id=STORE)

    assert entries[0].actor_code is None, "the name is genuinely unreadable"
    assert entries[0].actor_id is not None, (
        "a person did this and the row no longer says so — the query has "
        "stopped carrying actor_id, and the screen will call them the system"
    )


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
        store_id=STORE,
        since="2026-09-01T00:00:00+05:30",
        until="2026-09-19T00:00:00+05:30",
    )

    query = rest.first_query()
    assert "occurred_at=gte.2026-09-01T00:00:00+05:30" in query
    assert "occurred_at.lt.2026-09-19T00:00:00+05:30" in query


@pytest.mark.asyncio
async def test_the_upper_bound_is_exclusive(rest) -> None:
    """`lte` against a day boundary excludes the day it names.

    Found by an acceptance run, before it was run. The target row happened at
    18:35 UTC, a filter for "up to the 15th" sent `lte.2026-09-15`, and
    Postgres read that as midnight at the *start* of the 15th — so asking for
    the 15th returned nothing that happened on it. The caller now passes the
    first instant it does not want.
    """
    await service(rest).audit_log(store_id=STORE, until="2026-09-16T00:00:00Z")

    query = rest.first_query()
    assert "occurred_at.lt." in query
    assert "lte." not in query, (
        "an inclusive upper bound against a date is a range that excludes its "
        "own last day"
    )


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

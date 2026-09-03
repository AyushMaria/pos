"""Catalogue admin — phase 6 slice 6.

The interesting failures here are not arithmetic, they are *silence*. A
missing RLS policy does not raise: PostgREST returns 200 and an empty list,
and a screen built on that looks like it worked. Several of these tests exist
to make sure the service turns that silence into a sentence.

The other theme is that "offline" and "refused" must stay distinguishable all
the way to the UI, because the next thing the owner should do differs.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services.admin_service import (
    AdminRejected,
    AdminService,
    AdminUnavailable,
    DuplicateBarcode,
)
from app.services.auth_service import SessionStore

BASE = "https://project.supabase.co"
ANON = "anon-key"
TOKEN = "the-users-own-jwt"
STORE = "018f0000-0000-7000-8000-0000000000aa"
PRODUCT = "018f0000-0000-7000-8000-0000000000b1"


class FakePostgrest:
    """Records what was asked and replies with whatever the test lined up."""

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

    def last(self) -> httpx.Request:
        return self.seen[-1]


def signed_in(token: str | None = TOKEN) -> SessionStore:
    store = SessionStore()
    # The service only ever asks for the token, so a session object it never
    # reads would be scaffolding for its own sake.
    store._access_token = token
    return store


def service(rest: FakePostgrest, sessions: SessionStore | None = None) -> AdminService:
    return AdminService(BASE, ANON, sessions or signed_in(), client=rest.client)


@pytest.fixture
def rest() -> FakePostgrest:
    return FakePostgrest()


# ── Offline is a different answer from refused ────────────────────────────


@pytest.mark.asyncio
async def test_a_terminal_with_no_cloud_says_so(rest: FakePostgrest) -> None:
    admin = AdminService("", "", signed_in(), client=rest.client)
    with pytest.raises(AdminUnavailable, match="no cloud project"):
        await admin.search_products("rice")
    assert rest.seen == [], "should not have tried to call anything"


@pytest.mark.asyncio
async def test_signing_in_from_the_cache_cannot_edit(rest: FakePostgrest) -> None:
    """A session with no cloud identity behind it.

    The till still sells — that is the whole point of the offline cache — but
    there is no token to present to RLS, so editing is unavailable rather than
    forbidden.
    """
    admin = service(rest, signed_in(token=None))
    with pytest.raises(AdminUnavailable, match="needs the internet"):
        await admin.search_products("rice")


@pytest.mark.asyncio
async def test_a_network_failure_is_unavailable_not_rejected() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    admin = AdminService(
        BASE, ANON, signed_in(), client=httpx.AsyncClient(transport=httpx.MockTransport(explode))
    )
    with pytest.raises(AdminUnavailable):
        await admin.low_stock(STORE)


@pytest.mark.asyncio
async def test_a_server_error_is_unavailable(rest: FakePostgrest) -> None:
    rest.will_return({"message": "boom"}, status=500)
    admin = service(rest)
    with pytest.raises(AdminUnavailable):
        await admin.search_products("rice")


# ── It is the user's identity that reaches RLS ────────────────────────────


@pytest.mark.asyncio
async def test_the_users_own_token_is_what_postgrest_sees(rest: FakePostgrest) -> None:
    """Not the anon key, and not a service key.

    This is the whole reason the admin screens can be trusted to the same
    three enforcement points as everything else: Postgres decides, using the
    identity of the person who signed in.
    """
    rest.will_return([])
    await service(rest).search_products("rice")
    assert rest.last().headers["Authorization"] == f"Bearer {TOKEN}"
    assert rest.last().headers["apikey"] == ANON


# ── Products ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_looks_at_name_and_sku(rest: FakePostgrest) -> None:
    """38.6% of this catalogue has no barcode, so search is a primary path."""
    rest.will_return([])
    await service(rest).search_products("tomato")
    url = unquote(str(rest.last().url))
    assert "sku.ilike.*tomato*" in url
    assert "name.ilike.*tomato*" in url
    assert "deleted_at=is.null" in url


@pytest.mark.asyncio
async def test_an_edit_rls_filtered_away_is_not_a_success(rest: FakePostgrest) -> None:
    """The silent failure this slice was most likely to ship.

    PostgREST answers a PATCH that matched no rows with 200 and `[]` — the
    same shape as a successful edit of nothing. Without this the screen would
    show the new name until somebody refreshed.
    """
    rest.will_return([])
    with pytest.raises(AdminRejected, match="no product was changed"):
        await service(rest).update_product(PRODUCT, name="Tomatoes")


@pytest.mark.asyncio
async def test_a_patch_only_sends_what_it_was_given(rest: FakePostgrest) -> None:
    rest.will_return([_product_row()])
    await service(rest).update_product(PRODUCT, name="Tomatoes (loose)")
    assert json.loads(rest.last().content) == {"name": "Tomatoes (loose)"}


# ── Barcodes ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_duplicate_code_names_the_product_holding_it(
    rest: FakePostgrest,
) -> None:
    """`ux_product_barcodes_barcode` is the most useful constraint in the
    schema and the least useful error string."""
    rest.will_return({"code": "23505", "message": "duplicate key value"}, status=409)
    rest.will_return([_holder_row()])

    with pytest.raises(DuplicateBarcode) as caught:
        await service(rest).add_barcode(PRODUCT, "8906110944741")

    assert "SKU-1004" in str(caught.value)
    assert "Tomatoes (loose)" in str(caught.value)


@pytest.mark.asyncio
async def test_a_duplicate_is_still_reported_if_the_holder_cannot_be_read(
    rest: FakePostgrest,
) -> None:
    """A cashier-shaped permission set can hit the constraint and not be able
    to read the row behind it. Say the useful half rather than nothing."""
    rest.will_return({"code": "23505", "message": "duplicate key value"}, status=409)
    rest.will_return({"message": "permission denied"}, status=403)

    with pytest.raises(DuplicateBarcode, match="already on another product"):
        await service(rest).add_barcode(PRODUCT, "8906110944741")


@pytest.mark.asyncio
async def test_symbology_is_derived_not_asked_for(rest: FakePostgrest) -> None:
    rest.will_return([_barcode_row()])
    await service(rest).add_barcode(PRODUCT, "8906110944741")
    assert json.loads(rest.last().content)["symbology"] == "EAN13"


@pytest.mark.asyncio
async def test_withdrawing_a_barcode_is_a_soft_delete(rest: FakePostgrest) -> None:
    """The row survives so last month's `sale_lines.barcode_scanned` still
    resolves; the partial unique index frees the code anyway."""
    rest.will_return([_barcode_row()])
    await service(rest).withdraw_barcode("018f0000-0000-7000-8000-0000000000c1")
    assert rest.last().method == "PATCH"
    assert json.loads(rest.last().content) == {"deleted_at": "now()"}


# ── Prices ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_price_opens_before_the_old_one_closes(
    rest: FakePostgrest,
) -> None:
    """Order matters and it is not arbitrary.

    There is no transaction across two PostgREST calls, so either one can be
    the last thing that happens. Closing first and failing leaves a product
    with no price, which stops the till selling it. Opening first and failing
    leaves an overlap, which is recoverable. A gap is a customer waiting.
    """
    rest.will_return([_price_row()])
    rest.will_return([])
    await service(rest).set_price(PRODUCT, STORE, 4500)

    opened, closed = rest.seen
    assert opened.method == "POST"
    assert json.loads(opened.content)["price"] == 4500
    assert closed.method == "PATCH"
    assert "valid_to=is.null" in str(closed.url)


@pytest.mark.asyncio
async def test_a_price_is_paise_all_the_way_down(rest: FakePostgrest) -> None:
    rest.will_return([_price_row()])
    rest.will_return([])
    price = await service(rest).set_price(PRODUCT, STORE, 4500)
    assert price.price == 4500
    assert isinstance(price.price, int)


# ── The unknown-scan queue ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolving_sets_only_resolved(rest: FakePostgrest) -> None:
    """The scan is evidence about a moment, not a view of the catalogue.

    0016 enforces this with a trigger as well; sending anything else would be
    refused there. Not sending it is the first line of that defence.
    """
    rest.will_return([_scan_row()])
    await service(rest).resolve_scan("s1")
    assert json.loads(rest.last().content) == {"resolved": True}


@pytest.mark.asyncio
async def test_resolving_something_rls_hid_is_not_a_success(
    rest: FakePostgrest,
) -> None:
    """Before 0016 there was no update policy at all, so *every* resolve
    looked like this: 200, empty body, nothing changed."""
    rest.will_return([])
    with pytest.raises(AdminRejected, match="no scan was resolved"):
        await service(rest).resolve_scan("s1")


@pytest.mark.asyncio
async def test_the_queue_asks_for_this_store_and_the_unresolved(
    rest: FakePostgrest,
) -> None:
    rest.will_return([])
    await service(rest).unknown_scans(STORE)
    url = str(rest.last().url)
    assert f"store_id=eq.{STORE}" in url
    assert "resolved=is.false" in url


# ── Low stock ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_low_stock_reads_the_view_not_the_products_table(
    rest: FakePostgrest,
) -> None:
    """Rooted at `stock_levels` via the view, which is what keeps the unlisted
    placeholder off the reorder list: it never gets a level row at all."""
    rest.will_return([])
    await service(rest).low_stock(STORE)
    assert "/rest/v1/low_stock" in str(rest.last().url)
    assert f"store_id=eq.{STORE}" in str(rest.last().url)


# ── Through the router ────────────────────────────────────────────────────


def _sign_in(client: TestClient, who: dict[str, str], *, token: str | None) -> None:
    client.post("/auth/login", json=who).raise_for_status()
    store: SessionStore = client.app.state.sessions
    current = store.current
    assert current is not None
    store.set(current, token)


def test_a_cashier_is_refused_before_the_cloud_is_called(
    client: TestClient, seeded_cashier: dict[str, str]
) -> None:
    """403 from the router, not 422 from Postgres.

    Both would be correct; only one of them can be produced without a round
    trip, and a cashier should never be told to wait for the internet in
    order to be told no.
    """
    _sign_in(client, seeded_cashier, token=TOKEN)
    assert client.get("/admin/unknown-scans").status_code == 403


def test_a_manager_signed_in_offline_gets_503_not_500(
    client: TestClient, seeded_manager: dict[str, str]
) -> None:
    """503 specifically, because `ApiError.isUnavailable` is true for it and
    the screen then says "needs the internet" instead of "that did not work"."""
    _sign_in(client, seeded_manager, token=None)
    response = client.get("/admin/low-stock")
    assert response.status_code == 503
    assert "internet" in response.json()["detail"]


def test_a_duplicate_barcode_reaches_the_screen_as_a_sentence(
    client: TestClient, seeded_manager: dict[str, str], rest: FakePostgrest
) -> None:
    _sign_in(client, seeded_manager, token=TOKEN)
    rest.will_return({"code": "23505", "message": "duplicate key value"}, status=409)
    rest.will_return([_holder_row()])
    client.app.state.admin_service = service(rest)

    response = client.post(
        f"/admin/products/{PRODUCT}/barcodes", json={"barcode": "8906110944741"}
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "SKU-1004" in detail
    assert "constraint" not in detail.lower()


def test_the_store_comes_from_the_session_not_the_body(
    client: TestClient, seeded_manager: dict[str, str], rest: FakePostgrest
) -> None:
    """A manager edits the shop they are signed in to. There is no field on
    the form for which store, and no way to send one."""
    _sign_in(client, seeded_manager, token=TOKEN)
    rest.will_return([_price_row()])
    rest.will_return([])
    client.app.state.admin_service = service(rest)

    client.put(f"/admin/products/{PRODUCT}/price", json={"price": 4500})
    body = json.loads(rest.seen[0].content)
    assert body["store_id"] == client.app.state.sessions.current.store_id


# ── Fixtures for the rows PostgREST would send back ───────────────────────


def _product_row() -> dict[str, Any]:
    return {
        "id": PRODUCT,
        "sku": "SKU-1004",
        "name": "Tomatoes (loose)",
        "short_name": "Tomato",
        "uom": "kg",
        "tax_code": "GST0",
        "is_weighed": True,
        "track_stock": True,
        "is_active": True,
    }


def _barcode_row() -> dict[str, Any]:
    return {
        "id": "018f0000-0000-7000-8000-0000000000c1",
        "product_id": PRODUCT,
        "barcode": "8906110944741",
        "symbology": "EAN13",
        "pack_size": 1,
        "is_primary": False,
    }


def _holder_row() -> dict[str, Any]:
    """What the follow-up lookup finds: who already holds the code."""
    return {
        "product_id": PRODUCT,
        "products": {"sku": "SKU-1004", "name": "Tomatoes (loose)"},
    }


def _scan_row() -> dict[str, Any]:
    return {
        "id": "s1",
        "store_id": STORE,
        "barcode": "8906110944741",
        "scanned_at": "2026-09-01T10:00:00Z",
        "resolved": True,
    }


def _price_row() -> dict[str, Any]:
    return {
        "id": "018f0000-0000-7000-8000-0000000000d1",
        "product_id": PRODUCT,
        "store_id": STORE,
        "price": 4500,
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to": None,
    }

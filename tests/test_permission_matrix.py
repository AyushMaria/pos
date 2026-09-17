"""The permission matrix at every layer that enforces it — phase 7 slice 1.

Phase 6's lesson, recorded in its own plan: *reconciliation is the exit
criterion, so write that query first*. Phase 7's exit criterion is not a
query, it is this file. It is written before the override flow, the snapshot
signing and the audit viewer, so that the work those slices have to do
arrives as a list of failures rather than as a paragraph in a document.

Three layers enforce the matrix and only one of them is security:

    UI          hides a control              courtesy
    FastAPI     refuses the request          UX, plus a real guard locally
    RLS         refuses the row              the boundary

The UI layer is asserted in `ui-src/src/core/rbac/permissionMatrix.test.tsx`
and the RLS layer in `tests/test_rls.py`, because each needs a world this file
does not have. What lives here is the Python layer, the FastAPI layer, and the
two inventories that keep the other three honest: which permissions have an
API surface at all, and which routes have no `require()`.

The route table is **discovered from the running application**, not written
out here. `tests/test_route_coverage.py` records what happens when a list like
that is maintained by hand: it fell a migration behind and nothing failed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from app.domain import permissions as perms
from tests.conftest import REPO_ROOT, ROLE_USERS
from tests.test_permissions import EXPECTED

# ── Discovering what the application actually gates ─────────────────────────


class Operation(NamedTuple):
    method: str
    path: str


def _routes(routes: list[Any]) -> Iterator[Any]:
    """Every `APIRoute` reachable from the application.

    `include_router` in this FastAPI version leaves an `_IncludedRouter`
    wrapper whose `path` is None and whose children hang off
    `original_router`, so a plain walk of `app.routes` finds only what was
    declared on the app itself. `test_route_coverage.py` hit the same wall and
    went to the OpenAPI schema; that is not an option here, because the schema
    says nothing about which permission a route requires.
    """
    for route in routes:
        if hasattr(route, "dependant") and getattr(route, "path", None):
            yield route
        for child in getattr(route, "routes", None) or ():
            yield from _routes([child])
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _routes(list(included.routes))


def _required_permissions(dependant: Any) -> set[str]:
    """The keys `require()` is guarding this route with.

    `require(permission)` returns a closure named `dep` over exactly one
    string, so the key can be read back off the dependency graph. Reading the
    graph rather than the source means a route gated in some unusual way still
    shows up here.
    """
    found: set[str] = set()
    call = getattr(dependant, "call", None)
    closure = getattr(call, "__closure__", None)
    if getattr(call, "__name__", "") == "dep" and closure:
        found |= {
            cell.cell_contents for cell in closure if isinstance(cell.cell_contents, str)
        }
    for sub in dependant.dependencies:
        found |= _required_permissions(sub)
    return found


def _operations(route: Any) -> set[Operation]:
    methods = set(getattr(route, "methods", None) or ()) - {"HEAD", "OPTIONS"}
    return {Operation(method, route.path) for method in methods}


def gated_operations(client: TestClient) -> dict[Operation, str]:
    """Every operation that carries a `require()`, and the key it names."""
    found: dict[Operation, str] = {}
    for route in _routes(list(client.app.routes)):  # type: ignore[attr-defined]
        keys = _required_permissions(route.dependant)
        if not keys:
            continue
        assert len(keys) == 1, f"{route.path} requires several keys: {sorted(keys)}"
        for operation in _operations(route):
            found[operation] = next(iter(keys))
    return found


def all_operations(client: TestClient) -> set[Operation]:
    return {
        operation
        for route in _routes(list(client.app.routes))  # type: ignore[attr-defined]
        for operation in _operations(route)
    }


# ── One request per permission ──────────────────────────────────────────────


class Probe(NamedTuple):
    """A request that reaches exactly one `require()` and nothing expensive.

    The assertion is only ever "403 or not 403", so a probe is free to name a
    product that does not exist. A role that holds the key gets 404 or 422 and
    that counts as admitted — the guard let it through to the part of the
    application that had an opinion about the arguments.
    """

    method: str
    path: str
    json: dict | None = None


PROBES: dict[str, Probe] = {
    perms.PRODUCT_READ: Probe("GET", "/admin/products?q=milk"),
    perms.PRODUCT_CREATE: Probe(
        "POST",
        "/admin/products",
        {"sku": "SKU-PROBE", "name": "Probe", "tax_code": "GST0"},
    ),
    # A GET, so the probe cannot alter the catalogue it asserts about.
    perms.PRODUCT_EDIT: Probe("GET", "/admin/unknown-scans"),
    perms.STOCK_RECEIVE: Probe(
        "POST",
        "/inventory/receipts/preview",
        {"barcode": "8901262010016", "packs": 1},
    ),
    perms.STOCK_COUNT: Probe(
        "POST",
        "/inventory/counts",
        {"lines": [{"product_id": "no-such-product", "counted_milli": 0}]},
    ),
    perms.STOCK_ADJUST: Probe(
        "POST",
        "/inventory/adjustments",
        {"product_id": "no-such-product", "delta_milli": -1000, "note": "probe"},
    ),
    perms.SALE_CREATE: Probe("POST", "/register/carts"),
    perms.PAYMENT_ATTEST: Probe("POST", "/register/payments/no-such-attempt/confirm"),
    perms.SALE_REVIEW_RESOLVE: Probe("GET", "/register/reviews"),
    perms.REPORT_MARGIN: Probe("GET", "/reports/margin"),
    perms.REPORT_SALES_STORE: Probe("GET", "/sync/failures"),
}

#: Permission keys with no route behind them yet.
#:
#: **This is the work order, not permission.** Nine of the twenty keys in
#: §11.1 gate nothing at the API layer, because the acts they describe have
#: not been built. Five of them are slice 3's: a void, a refund, a line
#: discount, an unlimited discount and a price override are exactly the
#: permissions a supervisor override exists to lend. `cash.payout` and
#: `shift.close` wait on a cash-drawer screen; `user.manage` gets its first
#: read-only surface in slice 5; `settings.manage` has no screen at all.
#:
#: A key may only leave this set. Adding one means writing down that the
#: matrix has grown a hole, which should be harder than fixing it.
NO_API_SURFACE: frozenset[str] = frozenset(
    {
        perms.SALE_DISCOUNT_LINE,
        perms.SALE_DISCOUNT_UNLIMITED,
        perms.SALE_VOID,
        perms.SALE_REFUND,
        perms.PRICE_OVERRIDE,
        perms.CASH_PAYOUT,
        perms.SHIFT_CLOSE,
        perms.USER_MANAGE,
        perms.SETTINGS_MANAGE,
    }
)

#: Operations that deliberately carry no `require()`.
#:
#: Signing in cannot need a permission, and `/health` is what the shell polls
#: before there is a session at all. The reads are session-scoped: a cart
#: belongs to the till that opened it, and `GET /catalog/size` answers "is the
#: catalogue here yet", which the splash asks before anyone has signed in.
#:
#: `POST /sync/push` is in this list under protest — see
#: `test_sync_push_is_gated`, which is the thing that will take it out again.
UNGATED: frozenset[Operation] = frozenset(
    {
        Operation("GET", "/health"),
        Operation("POST", "/auth/login"),
        Operation("POST", "/auth/logout"),
        Operation("GET", "/auth/session"),
        Operation("GET", "/catalog/size"),
        Operation("GET", "/catalog/tax-codes"),
        Operation("GET", "/register/carts/{cart_id}"),
        Operation("GET", "/register/carts/{cart_id}/tender-quote"),
        Operation("GET", "/register/payments/{attempt_id}"),
        Operation("POST", "/register/sales/{sale_id}/receipt.pdf"),
        Operation("GET", "/sync/status"),
        Operation("POST", "/sync/push"),
    }
)


# ── The matrix, at the Python layer ─────────────────────────────────────────


@pytest.mark.parametrize("permission", sorted(EXPECTED))
@pytest.mark.parametrize("role", perms.ROLES)
def test_python_layer(role: str, permission: str) -> None:
    """`permissions_for` agrees with architecture §11.1, key by key.

    Already passing before phase 7 started. It is repeated here so one file
    can say "this key, this role, at this layer" for every layer, and a reader
    comparing two layers is not comparing two files.
    """
    granted = perms.permissions_for(frozenset({role}))
    should_have = role in EXPECTED[permission]
    assert (permission in granted) is should_have, (
        f"python layer: {role} should "
        f"{'hold' if should_have else 'not hold'} {permission}"
    )


# ── The matrix, at the FastAPI layer ────────────────────────────────────────


@pytest.mark.parametrize("permission", sorted(PROBES))
@pytest.mark.parametrize("role", perms.ROLES)
def test_fastapi_layer(role: str, permission: str, sign_in_as: Any) -> None:
    """A role without the key is refused; a role with it gets through.

    "Gets through" is deliberately weak: anything but 403. The probe's
    arguments are nonsense on purpose, so a 404 or a 422 is the guard having
    admitted the request and something further in having refused it for a
    reason that is not about permission.
    """
    probe = PROBES[permission]
    client = sign_in_as(role)
    response = client.request(probe.method, probe.path, json=probe.json)

    should_have = role in EXPECTED[permission]
    denied = response.status_code == 403

    if should_have:
        assert not denied, (
            f"fastapi layer: {role} holds {permission} but "
            f"{probe.method} {probe.path} returned 403"
        )
    else:
        assert denied, (
            f"fastapi layer: {role} does not hold {permission} yet "
            f"{probe.method} {probe.path} returned {response.status_code}, not 403"
        )


# ── The inventories that keep the layers honest ─────────────────────────────


def test_every_gated_key_has_a_probe(client: TestClient) -> None:
    """A newly gated route joins the matrix, or fails here.

    This is what stops the FastAPI layer above from testing eleven keys for
    ever while the application quietly grows a twelfth.
    """
    missing = sorted(set(gated_operations(client).values()) - set(PROBES))
    assert not missing, (
        "these permission keys now gate a route but have no probe:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd one to PROBES so the matrix covers them."
    )


def test_the_no_surface_list_is_exactly_the_keys_with_no_route(
    client: TestClient,
) -> None:
    """`NO_API_SURFACE` describes the application, not a wish about it."""
    gated = set(gated_operations(client).values())
    absent = set(perms.ALL_PERMISSIONS) - gated

    gained = sorted(NO_API_SURFACE - absent)
    assert not gained, (
        "these keys now gate a route and must leave NO_API_SURFACE:\n  "
        + "\n  ".join(gained)
    )

    unlisted = sorted(absent - NO_API_SURFACE)
    assert not unlisted, (
        "these keys gate nothing at the API layer and are not written down:\n  "
        + "\n  ".join(unlisted)
        + "\n\nBuild the route, or add them to NO_API_SURFACE and say why."
    )


def test_no_route_is_ungated_by_accident(client: TestClient) -> None:
    """Every operation either requires a key or is listed as not needing one."""
    ungated = all_operations(client) - set(gated_operations(client))
    surprises = sorted(ungated - UNGATED)
    assert not surprises, (
        "these operations have no require() and are not listed as ungated:\n  "
        + "\n  ".join(f"{method} {path}" for method, path in surprises)
        + "\n\nGate them, or add them to UNGATED and say why in the commit."
    )


def test_the_ungated_list_does_not_outlive_the_debt(client: TestClient) -> None:
    """An operation that gained a `require()` must leave `UNGATED`."""
    stale = sorted(set(gated_operations(client)) & UNGATED)
    assert not stale, (
        "these operations are gated now and must leave UNGATED:\n  "
        + "\n  ".join(f"{method} {path}" for method, path in stale)
    )


# ── The finding this file was written to make visible ───────────────────────


@pytest.mark.xfail(
    reason="phase 7 slice 2 — POST /sync/push has no require(); "
    "its two siblings require report.sales.store",
    strict=True,
)
def test_sync_push_is_gated(client: TestClient) -> None:
    """Any signed-in cashier can force a drain of the outbox.

    `GET /sync/failures` and `POST /sync/failures/retry` both require
    `report.sales.store`. `POST /sync/push` requires nothing, which is either
    a decision nobody wrote down or an oversight — the plan asks for it to be
    decided rather than inherited.

    `xfail(strict=True)`, so slice 2 cannot close this quietly: the day a key
    is added, this test passes unexpectedly and fails the run until the marker
    comes off with it.
    """
    assert Operation("POST", "/sync/push") in gated_operations(client)


# ── Groundwork the three layers all depend on ───────────────────────────────


def test_every_role_can_sign_in(sign_in_as: Any) -> None:
    """The matrix is worthless if a role cannot get a session at all.

    Cheap, and it fails with one clear message rather than letting every
    parametrised case for that role fail inside a fixture.
    """
    for role in perms.ROLES:
        client = sign_in_as(role)
        session = client.get("/auth/session")
        assert session.status_code == 200, f"{role}: {session.text}"
        assert set(session.json()["permissions"]) == set(
            perms.permissions_for(frozenset({role}))
        ), role


def test_role_users_covers_every_role() -> None:
    assert set(ROLE_USERS) == set(perms.ROLES)


# ── The UI audit slice 2 has to finish ──────────────────────────────────────
#
# The UI layer of the matrix is asserted in
# `ui-src/src/core/rbac/permissionMatrix.test.tsx`, which exercises
# `<PermissionGate>` against all twenty keys and all five roles. What it
# cannot see is a control that never went through the component.
#
# So the audit is a grep, and it lives here rather than in Vitest for the same
# reason `test_no_role_string_checks_in_the_codebase` does: it is a question
# about source text, not about rendering, and asking it from Node would have
# meant `@types/node` and a `tsc` that no longer typechecks the browser
# bundle as a browser bundle.

UI_SRC = REPO_ROOT / "ui-src" / "src"

#: Ad-hoc permission checks, by file, as slice 1 found them.
#:
#: **This is debt, not permission.** Slice 2 replaces each with
#: `<PermissionGate>` or `useHasPermission`, so that an audit by grep finds
#: every gated control. A number may only go down; raising one records that a
#: screen grew a control the audit cannot see, which should be harder than
#: using the component.
#:
#: The plan says seven. It is five — two of them `.filter()` calls over a tab
#: list, which is a reasonable shape and still invisible to the audit.
AD_HOC_PERMISSION_CHECKS: dict[str, int] = {
    "features/admin/AdminScreen.tsx": 4,
    "features/stockroom/StockroomScreen.tsx": 1,
}

_AD_HOC = re.compile(r"session\??\.permissions\??\.includes\(")


def _ui_sources() -> list[Path]:
    return [
        path
        for path in sorted(UI_SRC.rglob("*.ts*"))
        if ".test." not in path.name and path.name != "PermissionGate.tsx"
    ]


def _ad_hoc_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in _ui_sources():
        hits = len(_AD_HOC.findall(path.read_text(encoding="utf-8")))
        if hits:
            counts[path.relative_to(UI_SRC).as_posix()] = hits
    return counts


def test_the_ui_audit_finds_no_ad_hoc_check_outside_the_list() -> None:
    counts = _ad_hoc_counts()
    surprises = sorted(set(counts) - set(AD_HOC_PERMISSION_CHECKS))
    assert not surprises, (
        "these files gate a control without <PermissionGate>:\n  "
        + "\n  ".join(surprises)
        + "\n\nUse the component, or add the file to "
        "AD_HOC_PERMISSION_CHECKS and say why."
    )


def test_the_ui_audit_list_does_not_outlive_the_debt() -> None:
    """A file that lost its ad-hoc checks must lose its entry."""
    counts = _ad_hoc_counts()
    wrong = {
        path: (counts.get(path, 0), listed)
        for path, listed in AD_HOC_PERMISSION_CHECKS.items()
        if counts.get(path, 0) != listed
    }
    assert not wrong, (
        "the audit list disagrees with the source:\n  "
        + "\n  ".join(
            f"{path}: {found} found, {listed} listed — "
            + ("slice 2 fixed some; lower it" if found < listed else "a new one appeared")
            for path, (found, listed) in wrong.items()
        )
    )


def test_the_ad_hoc_pattern_still_matches_something() -> None:
    """The grep above is load-bearing, and a rename would silence it.

    If `SessionResponse.permissions` is ever renamed, `_AD_HOC` stops matching
    and both tests above go green on an empty set — the audit would report
    that slice 2 was finished when nothing had been done.
    """
    assert _ad_hoc_counts(), (
        "no ad-hoc permission check found anywhere in ui-src. Either slice 2 "
        "is complete — in which case empty AD_HOC_PERMISSION_CHECKS and delete "
        "this test — or the pattern has stopped matching the code."
    )


def test_the_gate_is_actually_used() -> None:
    """A grep-based audit means nothing if nothing uses the component."""
    uses = sum(
        path.read_text(encoding="utf-8").count("<PermissionGate")
        for path in _ui_sources()
    )
    assert uses > 0, "no <PermissionGate> in ui-src"

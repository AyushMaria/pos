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
#: `POST /sync/push` is here by decision rather than by default, which is what
#: slice 2 settled and `test_sync_push_is_deliberately_ungated` records. It
#: drains the outbox early; every row in it was written under a permission
#: checked at the time, and RLS still refuses each one on its own merits.
#:
#: `POST /overrides/authorize` is the other decision. A gate on it would be
#: circular: the cashier who needs an override is the one who does not hold
#: the key, so any permission strict enough to matter would refuse exactly the
#: people it exists for. It is not unauthenticated — it requires a session,
#: because a grant is lent to somebody — and the credential it checks is a
#: second person's PIN rather than the caller's rights.
UNGATED: frozenset[Operation] = frozenset(
    {
        Operation("GET", "/health"),
        Operation("POST", "/auth/login"),
        Operation("POST", "/auth/logout"),
        Operation("GET", "/auth/session"),
        Operation("GET", "/catalog/size"),
        Operation("GET", "/catalog/tax-codes"),
        Operation("POST", "/overrides/authorize"),
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


def test_sync_push_is_deliberately_ungated(client: TestClient) -> None:
    """`POST /sync/push` needs no permission, and that is the decision.

    Slice 1 found it ungated while both its neighbours in the same router
    require `report.sales.store`, and could not tell whether that was a choice
    or an oversight. Slice 2 made it a choice: draining the queue early is the
    same act as waiting for the next cycle. The rows were written under
    permissions checked when they were written, the push runs under the
    terminal's own credentials, and RLS refuses each row on its own merits.
    Asking sooner grants nothing.

    Its neighbours are gated and should be — they show what was quarantined
    and put it back in the queue, which are judgements about other people's
    work. This one only changes when.

    The assertion runs in three directions on purpose. "Is not gated" alone
    would also pass if the route disappeared; the neighbours are checked so
    that a future change gating all three cannot leave this docstring
    describing a system that no longer exists; and the last one is the
    difference between *ungated* and *unauthenticated*.

    That third one guards a specific, likely mistake. `push_now` declares
    `session: CurrentSession` and never mentions it again — the parameter
    exists only to make FastAPI resolve the dependency. Someone reading
    "requires nothing" directly above an argument the body never uses could
    reasonably delete it, and without this line every test here would stay
    green while the outbox drain became reachable to anyone who could reach
    the port.
    """
    gated = gated_operations(client)

    assert Operation("POST", "/sync/push") in all_operations(client)
    assert Operation("POST", "/sync/push") not in gated

    assert gated.get(Operation("GET", "/sync/failures")) == perms.REPORT_SALES_STORE
    assert (
        gated.get(Operation("POST", "/sync/failures/retry"))
        == perms.REPORT_SALES_STORE
    )

    # `client` carries the terminal's own bearer token but nobody has signed
    # in, so this is the session guard answering and not the token one. The
    # detail is asserted rather than just the status, because both refusals
    # are 401 and only one of them is the thing under test here.
    refused = client.post("/sync/push")
    assert refused.status_code == 401
    assert refused.json()["detail"] == "not_signed_in"


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

#: Ad-hoc permission checks, by file. **Empty, and meant to stay that way.**
#:
#: Slice 1 found five — four in `AdminScreen.tsx`, one in
#: `StockroomScreen.tsx`, two of them `.filter()` calls over a tab list. Slice
#: 2 replaced all five with `useHasPermission`, so every gated control in the
#: UI is now findable by grepping for the component or the hook.
#:
#: A file may be added back only with a reason. The point of the list is not
#: the count; it is that gating a control without the component becomes
#: something you have to write down.
AD_HOC_PERMISSION_CHECKS: dict[str, int] = {}

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


def test_the_ad_hoc_pattern_would_still_catch_one() -> None:
    """The grep above is load-bearing, and now it matches nothing.

    While the audit list had entries, "did it find them?" was its own proof.
    Slice 2 emptied the list, so the two tests above are now asserting that a
    search finds nothing — which is exactly what a search that has quietly
    stopped working also reports.

    So the pattern is tested against a sample instead of against the tree. If
    `SessionResponse.permissions` is renamed, or the check is written some
    third way, this fails rather than the audit going silently green.
    """
    assert _AD_HOC.search("const may = session.permissions.includes('sale.void');")
    assert _AD_HOC.search("if (session?.permissions?.includes(key)) return true;")
    assert not _AD_HOC.search("const has = useHasPermission(session);")
    assert not _AD_HOC.search('<PermissionGate session={session} permission="x">')


def test_the_gate_is_actually_used() -> None:
    """A grep-based audit means nothing if nothing uses the component."""
    uses = sum(
        path.read_text(encoding="utf-8").count("<PermissionGate")
        for path in _ui_sources()
    )
    assert uses > 0, "no <PermissionGate> in ui-src"


# ── What a 403 means, and why the matrix may believe it ─────────────────────

#: Files in `app/api` allowed to produce a 403 that did not come from
#: `require()`, and why.
#:
#: The matrix asserts "403 or not 403" for every gated operation. That reads a
#: status code and concludes something about a *layer*, which only holds while
#: one layer produces it. Two layers answering with the same status for
#: different reasons is a test that cannot tell you which one answered — the
#: trap `POST /overrides/authorize` fell into with its 401, where a dependency
#: and a service both said `not_signed_in` and deleting the dependency changed
#: nothing the test could see.
#:
#: `overrides.py` is here because refusing a self-authorisation and refusing an
#: approver who lacks the key are both genuinely "forbidden", and the route is
#: in `UNGATED` — it has no `require()` to be confused with, so the matrix
#: never probes it.
NON_REQUIRE_403: dict[str, str] = {
    "overrides.py": "an ungated route; its 403s are about the approver",
}

API_DIR = Path(__file__).resolve().parent.parent / "app" / "api"


def test_require_is_the_only_thing_that_forbids() -> None:
    """A 403 from a gated route means the permission gate refused it.

    The positive control is `deps.py`: `require()` must still be the thing
    raising 403, or this check is asserting a property of an application that
    no longer exists.
    """
    forbidding = {
        path.name
        for path in sorted(API_DIR.glob("*.py"))
        if "HTTP_403_FORBIDDEN" in path.read_text(encoding="utf-8")
    }

    assert "deps.py" in forbidding, (
        "require() no longer raises 403 — the matrix is reading a status code "
        "that no longer means what it says"
    )

    surprises = sorted(forbidding - {"deps.py"} - set(NON_REQUIRE_403))
    assert not surprises, (
        "these files answer 403 without being the permission gate:\n  "
        + "\n  ".join(surprises)
        + "\n\nA second producer makes every 403 in the matrix ambiguous: the "
        "assertion still passes, and it no longer tells you which layer "
        "refused. Add it to NON_REQUIRE_403 with a reason, or use a status "
        "that says what actually happened."
    )

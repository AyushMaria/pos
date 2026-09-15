"""No endpoint may exist that no test ever calls.

## Why this exists

`POST /admin/products` shipped in slice 6 with a service method, a route, a
request model, a TypeScript client function — and no test anywhere and no
caller in the UI. The slice's own acceptance step was "create a product", and
it was impossible to do.

The reason nothing noticed is worth stating: `GET /admin/products` *was*
tested. Any check that counted paths rather than operations would have called
that endpoint covered. So this counts **method and path**, which is the level
at which the hole existed.

## What "called" means here

A literal HTTP call somewhere under `tests/` — `client.post("/admin/products")`
or the f-string form. It is a text search, not an execution trace, which makes
it cheap, order-independent, and immune to `-k` filtering. The cost is that it
believes a call it can see even if that test is skipped.

A service-level test with a fake transport does *not* count. `test_admin.py`
has 22 of those and they are good tests, but they exercise
`AdminService`, not the router — not the permission dependency, not the error
mapping, not the response model. Slice 6 mapped three exception types onto
three status codes; only a call through the app proves that mapping.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

TESTS = Path(__file__).resolve().parent

#: The methods an endpoint can be reached by. OpenAPI keys these lowercase.
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

#: Endpoints that had no HTTP-level test when this check was written.
#:
#: **This is debt, not permission.** Turning the check on would otherwise have
#: meant writing a dozen unrelated tests in one commit. It may shrink; a new
#: entry means writing down that an endpoint ships untested, which should feel
#: like something worth avoiding.
#:
#: `POST /admin/products` is deliberately absent — it is the reason this file
#: exists, and it got its test in the same commit.
#:
#: Six of the ten are the slice 6 admin router, which is the honest shape of
#: that slice: thoroughly tested one layer below the thing the screen talks
#: to. `POST /admin/unknown-scans/{scan_id}/resolve` left the list when the
#: queue's Done button turned out to be closing entries without cataloguing
#: anything — the layer below had always agreed with itself.
UNTESTED: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/admin/products"),
        ("GET", "/admin/products/{product_id}"),
        ("PATCH", "/admin/products/{product_id}"),
        ("GET", "/admin/products/{product_id}/barcodes"),
        ("DELETE", "/admin/barcodes/{barcode_id}"),
        ("GET", "/admin/products/{product_id}/prices"),
        ("GET", "/catalog/lookup"),
        ("GET", "/catalog/size"),
        ("DELETE", "/register/carts/{cart_id}"),
        ("POST", "/sync/push"),
    }
)


def _operations(client: TestClient) -> set[tuple[str, str]]:
    """Every (method, path) the application actually serves.

    Read from the OpenAPI surface, not `app.routes`. This FastAPI version does
    not put an included router's `APIRoute` objects on the application —
    `include_router` leaves a wrapper whose `path` is None — so walking
    `app.routes` sees only what was declared on the app itself and reports
    every router endpoint as not existing. That is all nine routers,
    including the admin one this check was written for: it checked 1 of 46
    operations and called the other 45 gone.

    Using the schema also settles `GET /` without an exemption. The dev login
    fallback is `include_in_schema=False`, and exists at all only when there
    is no UI build to mount over it — so it is absent here by construction
    rather than by a rule that would have to explain itself.
    """
    found: set[tuple[str, str]] = set()
    for path, item in client.app.openapi()["paths"].items():  # type: ignore[attr-defined]
        for method in item:
            if method.upper() in _METHODS:
                found.add((method.upper(), path))
    return found


def _test_sources() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(TESTS.rglob("*.py"))
        if path.name != Path(__file__).name
    )


def _is_called(method: str, path: str, sources: str) -> bool:
    """Does any test make this call?

    A path parameter is written `{cart_id}` in the route and, in a test, as
    anything from `"c1"` to `{tender['attempt_id']}` — so a segment matches
    anything that is not a slash. The trailing group allows a query string,
    because `/catalog/search?q=x` is still a call to `/catalog/search`.
    """
    literal = re.escape(path)
    literal = re.sub(r"\\\{[a-z_]+\\\}", lambda _: r"[^/]+", literal)
    pattern = re.compile(
        r"\." + method.lower() + r"\(\s*\n?\s*f?[\"']" + literal + r"(?:[?\"'])"
    )
    return bool(pattern.search(sources))


def test_every_endpoint_is_called_by_some_test(client: TestClient) -> None:
    sources = _test_sources()
    uncalled = {
        operation
        for operation in _operations(client)
        if not _is_called(*operation, sources)
    }

    surprises = sorted(uncalled - UNTESTED)
    assert not surprises, (
        "these endpoints have no HTTP-level test:\n  "
        + "\n  ".join(f"{method} {path}" for method, path in surprises)
        + "\n\nWrite one, or add it to UNTESTED and say why in the commit."
    )


def test_the_debt_list_does_not_outlive_the_debt(client: TestClient) -> None:
    """A route that gained a test must leave `UNTESTED`.

    Otherwise the list quietly becomes a list of things that *used* to be
    untested, and stops meaning anything.
    """
    sources = _test_sources()
    operations = _operations(client)

    stale = sorted(
        operation
        for operation in UNTESTED
        if operation not in operations or _is_called(*operation, sources)
    )
    assert not stale, (
        "these are in UNTESTED but no longer need to be:\n  "
        + "\n  ".join(f"{method} {path}" for method, path in stale)
    )

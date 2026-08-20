"""Local API security — architecture §5.

Binding to a loopback port does not make an API private: any process on the
machine, and any web page the cashier opens, can reach it. Four things close
that gap, and all four are cheap:

  * a per-launch session token, handed to the webview in its URL and required
    as ``Authorization: Bearer`` on every request including the WS upgrade;
  * a ``Host`` allow-list, which is what actually defeats DNS rebinding —
    a malicious page can be made to resolve a name to 127.0.0.1, but it
    cannot forge the Host header the browser sends;
  * an ``Origin`` check, so a page cannot drive the API from the background;
  * no CORS headers at all — React is served by FastAPI, so every legitimate
    request is same-origin by construction.
"""

from __future__ import annotations

import secrets
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

#: Endpoints reachable without the session token. `/health` is the startup
#: gate the splash polls, and it must answer before a token is in play. It
#: leaks nothing beyond a version string and the migration state.
PUBLIC_PATHS = frozenset({"/health"})

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def new_session_token() -> str:
    """A fresh token for this launch. Never persisted, never logged."""
    return secrets.token_urlsafe(32)


def pick_free_port() -> int:
    """An ephemeral port, so nothing can pre-target a fixed one.

    There is an unavoidable race between closing this socket and uvicorn
    binding it; on a single-user till the window is microseconds and the
    alternative — a fixed port — is a standing target.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _host_allowed(header: str | None) -> bool:
    if not header:
        return False
    host = header.rsplit(":", 1)[0] if not header.startswith("[") else header.split("]")[0] + "]"
    return host in ALLOWED_HOSTS


def _origin_allowed(header: str | None) -> bool:
    # A same-origin fetch from the served page may omit Origin entirely.
    if header is None:
        return True
    if header == "null":
        return False
    parsed = urlparse(header)
    return parsed.hostname in ALLOWED_HOSTS


class HostGuardMiddleware(BaseHTTPMiddleware):
    """Rejects anything not addressed to loopback by name."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not _host_allowed(request.headers.get("host")):
            return JSONResponse({"detail": "bad_host"}, status_code=421)
        if not _origin_allowed(request.headers.get("origin")):
            return JSONResponse({"detail": "bad_origin"}, status_code=403)
        return await call_next(request)


class SessionTokenMiddleware(BaseHTTPMiddleware):
    """Requires the per-launch bearer token on every non-public request."""

    def __init__(self, app: object, token: str) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._token = token

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in PUBLIC_PATHS or self._is_ui_asset(request.url.path):
            return await call_next(request)

        if not verify_session_token(self._token, extract_bearer(request)):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @staticmethod
    def _is_ui_asset(path: str) -> bool:
        # The webview loads the page itself before it has parsed the token out
        # of its own URL, so the static bundle is served unauthenticated. It
        # contains no data — only the shell that then authenticates.
        return path == "/" or path.startswith("/assets/") or path == "/favicon.ico"


def extract_bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    # The WebSocket upgrade cannot carry custom headers from a browser, so the
    # token may arrive as a query parameter there (architecture §5).
    return request.query_params.get("t")


def verify_session_token(expected: str, presented: str | None) -> bool:
    if not presented:
        return False
    return secrets.compare_digest(expected, presented)


def require_session_token(request: Request, expected: str) -> None:
    if not verify_session_token(expected, extract_bearer(request)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")


def websocket_origin_allowed(origin: str | None) -> bool:
    """Origin check for the WebSocket upgrade (phase 3 uses this)."""
    return _origin_allowed(origin)

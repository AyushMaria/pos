"""Local API security — architecture §5.

Loopback is not a private channel. The threat model is concrete: a page the
cashier opens in a browser, or any other process on the till, trying to drive
the register. Each test below is one of the four defences.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.security.local_auth import (
    _host_allowed,
    _origin_allowed,
    new_session_token,
    pick_free_port,
    verify_session_token,
)
from tests.conftest import TEST_TOKEN


def test_health_needs_no_token(raw_client: TestClient) -> None:
    """The splash polls /health before a token is in play."""
    assert raw_client.get("/health").status_code == 200


def test_api_rejects_a_missing_token(raw_client: TestClient) -> None:
    response = raw_client.post(
        "/auth/login", json={"employee_code": "C001", "pin": "4913"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_api_rejects_a_wrong_token(raw_client: TestClient) -> None:
    response = raw_client.post(
        "/auth/login",
        json={"employee_code": "C001", "pin": "4913"},
        headers={"Authorization": "Bearer not-the-token"},
    )
    assert response.status_code == 401


def test_api_accepts_the_session_token(client: TestClient, seeded_cashier: dict) -> None:
    response = client.post("/auth/login", json=seeded_cashier)
    assert response.status_code == 200


def test_dns_rebinding_is_refused(client: TestClient) -> None:
    """A rebound name resolves to 127.0.0.1 but still sends its own Host."""
    response = client.get("/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 421


def test_a_foreign_origin_is_refused(client: TestClient, seeded_cashier: dict) -> None:
    response = client.post(
        "/auth/login",
        json=seeded_cashier,
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403


def test_null_origin_is_refused(client: TestClient, seeded_cashier: dict) -> None:
    """A sandboxed iframe or a file:// page sends Origin: null."""
    response = client.post(
        "/auth/login", json=seeded_cashier, headers={"Origin": "null"}
    )
    assert response.status_code == 403


def test_loopback_origin_is_accepted(client: TestClient, seeded_cashier: dict) -> None:
    response = client.post(
        "/auth/login",
        json=seeded_cashier,
        headers={"Origin": "http://127.0.0.1:52341"},
    )
    assert response.status_code == 200


def test_no_cors_headers_are_ever_sent(client: TestClient) -> None:
    """Same-origin by construction; a CORS header would only weaken it."""
    headers = client.get("/health").headers
    assert not any(name.lower().startswith("access-control-") for name in headers)


def test_host_allow_list() -> None:
    assert _host_allowed("127.0.0.1:8000") is True
    assert _host_allowed("localhost:8000") is True
    assert _host_allowed("127.0.0.1") is True
    assert _host_allowed("evil.example.com") is False
    assert _host_allowed("127.0.0.1.evil.example.com") is False
    assert _host_allowed(None) is False


def test_origin_allow_list() -> None:
    assert _origin_allowed(None) is True  # same-origin fetches omit it
    assert _origin_allowed("http://127.0.0.1:9999") is True
    assert _origin_allowed("http://localhost:9999") is True
    assert _origin_allowed("null") is False
    assert _origin_allowed("https://evil.example.com") is False


def test_token_comparison_rejects_prefixes() -> None:
    token = new_session_token()
    assert verify_session_token(token, token) is True
    assert verify_session_token(token, token[:-1]) is False
    assert verify_session_token(token, "") is False
    assert verify_session_token(token, None) is False


def test_tokens_are_unpredictable() -> None:
    tokens = {new_session_token() for _ in range(1000)}
    assert len(tokens) == 1000
    assert all(len(token) >= 40 for token in tokens)


def test_port_is_ephemeral_and_free() -> None:
    first, second = pick_free_port(), pick_free_port()
    assert 1024 < first < 65536
    assert first != second


def test_openapi_is_not_exposed(client: TestClient) -> None:
    """Docs are off; the schema is generated at build time, not served."""
    assert client.get("/docs").status_code == 404


# ── The event channel (architecture §4, §5) ─────────────────────────────────


def test_the_websocket_needs_the_session_token(client: TestClient) -> None:
    """A browser cannot set headers on an upgrade, so the token is a query
    parameter — and it is still checked."""
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(
        "/events?t=wrong-token"
    ):
        pass

    assert refused.value.code == 1008


def test_the_websocket_accepts_the_right_token(client: TestClient) -> None:
    with client.websocket_connect(f"/events?t={TEST_TOKEN}") as socket:
        first = socket.receive_json()

    assert first["event"] == "connectivity"
    assert first["payload"]["online"] is True


def test_the_websocket_refuses_a_foreign_origin(client: TestClient) -> None:
    """Otherwise any page the cashier opens could drive the register."""
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(
        f"/events?t={TEST_TOKEN}", headers={"Origin": "https://evil.example.com"}
    ):
        pass

    assert refused.value.code == 1008

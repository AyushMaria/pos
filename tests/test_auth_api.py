"""Login and the health gate — the phase 1 exit criteria, end to end."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.data.migrations import latest_version
from app.data.repositories.users import CachedUserRepository
from app.domain import permissions as perms
from app.domain.identity import SNAPSHOT_TTL, utcnow
from app.services.auth_service import AuthService

# ── /health ─────────────────────────────────────────────────────────────────


def test_health_reports_schema_version(client: TestClient) -> None:
    body = client.get("/health").json()

    assert body["status"] == "ready"
    assert body["schema_version"] == latest_version()
    assert body["expected_schema_version"] == latest_version()
    assert body["migrations_pending"] is False
    assert body["store_code"] == "ST01"
    assert body["terminal_code"] == "T1"


def test_health_flags_an_unconfigured_cloud(client: TestClient) -> None:
    assert client.get("/health").json()["cloud_configured"] is False


# ── Login ───────────────────────────────────────────────────────────────────


def test_seeded_cashier_logs_in(client: TestClient, seeded_cashier: dict) -> None:
    """Exit criterion: a seeded cashier signs in with employee code + PIN."""
    response = client.post("/auth/login", json=seeded_cashier)
    assert response.status_code == 200

    body = response.json()
    assert body["employee_code"] == "C001"
    assert body["full_name"] == "Anita Rao"
    assert body["offline"] is True
    assert perms.SALE_CREATE in body["permissions"]
    assert perms.REPORT_MARGIN not in body["permissions"]


def test_employee_code_is_case_insensitive(
    client: TestClient, seeded_cashier: dict
) -> None:
    response = client.post("/auth/login", json={"employee_code": "c001", "pin": "4913"})
    assert response.status_code == 200


def test_wrong_pin_is_rejected(client: TestClient, seeded_cashier: dict) -> None:
    response = client.post("/auth/login", json={"employee_code": "C001", "pin": "0000"})
    assert response.status_code == 401


def test_unknown_employee_offline_gets_an_actionable_message(
    client: TestClient, seeded_cashier: dict
) -> None:
    """A cashier must be told what to do, not just refused (plan §5)."""
    response = client.post("/auth/login", json={"employee_code": "Z999", "pin": "4913"})
    assert response.status_code == 503
    assert "internet" in response.json()["detail"].lower()


def test_short_pin_is_refused_by_the_contract(client: TestClient) -> None:
    response = client.post("/auth/login", json={"employee_code": "C001", "pin": "12"})
    assert response.status_code == 422


def test_no_token_is_returned_to_the_browser(
    client: TestClient, seeded_cashier: dict
) -> None:
    """The access token stays in the service process (architecture §5)."""
    body = client.post("/auth/login", json=seeded_cashier).json()
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert "pin_hash" not in body


def test_session_survives_until_logout(client: TestClient, seeded_cashier: dict) -> None:
    client.post("/auth/login", json=seeded_cashier)
    assert client.get("/auth/session").status_code == 200

    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/session").status_code == 401


def test_session_endpoint_requires_a_login(client: TestClient) -> None:
    assert client.get("/auth/session").status_code == 401


def test_expired_snapshot_stops_the_till(
    client: TestClient, auth_service: AuthService, users: CachedUserRepository
) -> None:
    """A dismissed employee cannot trade forever on a cached PIN."""
    stale = utcnow() - SNAPSHOT_TTL - timedelta(hours=1)
    auth_service.seed_local_user(
        user_id="018f0000-0000-7000-8000-00000000000e",
        employee_code="X001",
        full_name="Long Gone",
        store_id="s1",
        pin="4913",
        roles=frozenset({perms.CASHIER}),
        permissions=perms.permissions_for(frozenset({perms.CASHIER})),
        signed_at=stale,
    )

    response = client.post("/auth/login", json={"employee_code": "X001", "pin": "4913"})
    assert response.status_code == 401
    assert "offline too long" in response.json()["detail"]


def test_revoking_purges_the_snapshot(
    client: TestClient, auth_service: AuthService, users: CachedUserRepository
) -> None:
    auth_service.seed_local_user(
        user_id="018f0000-0000-7000-8000-00000000000f",
        employee_code="R001",
        full_name="Revoked Rita",
        store_id="s1",
        pin="4913",
        roles=frozenset({perms.CASHIER}),
        permissions=perms.permissions_for(frozenset({perms.CASHIER})),
    )
    assert client.post(
        "/auth/login", json={"employee_code": "R001", "pin": "4913"}
    ).status_code == 200

    users.revoke("018f0000-0000-7000-8000-00000000000f")

    assert users.get_by_employee_code("R001") is None
    assert client.post(
        "/auth/login", json={"employee_code": "R001", "pin": "4913"}
    ).status_code == 503


# ── The permission gate ─────────────────────────────────────────────────────


def test_cashier_is_denied_the_margin_report(
    client: TestClient, seeded_cashier: dict
) -> None:
    client.post("/auth/login", json=seeded_cashier)
    response = client.get("/reports/margin")
    assert response.status_code == 403
    assert response.json()["detail"] == "permission_denied"


def test_manager_passes_the_margin_gate(
    client: TestClient, seeded_manager: dict
) -> None:
    """501, not 403: the gate opens even though the report is phase 8 work."""
    client.post("/auth/login", json=seeded_manager)
    assert client.get("/reports/margin").status_code == 501


def test_margin_report_requires_a_login(client: TestClient) -> None:
    assert client.get("/reports/margin").status_code == 401

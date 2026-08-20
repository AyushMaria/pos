"""The shell bootstrap — architecture §2.

Everything except the webview itself: a real uvicorn on a real ephemeral port,
polled through the real health gate. The window is left out because a display
is not available in CI, and because the interesting failures are all on this
side of it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app.api.server import build_app
from app.config import Settings
from app.data.db import Database
from app.data.migrations import latest_version, migrate
from app.main import _serve, wait_for_health
from app.security.local_auth import new_session_token, pick_free_port


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[int, str, Settings]]:
    settings = Settings(
        data_dir=tmp_path,
        supabase_url="",
        supabase_anon_key="",
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
    )
    db = Database(settings.db_path)
    migrate(db)

    port = pick_free_port()
    token = new_session_token()
    app = build_app(token=token, settings=settings, db=db, run_migrations=False)

    threading.Thread(target=_serve, args=(app, port), daemon=True).start()
    yield port, token, settings
    db.close()


def test_health_gate_opens(live_server: tuple[int, str, Settings]) -> None:
    port, token, _ = live_server

    health = wait_for_health(port, token, timeout=20.0)

    assert health["status"] == "ready"
    assert health["schema_version"] == latest_version()


def test_health_gate_times_out_on_a_dead_port() -> None:
    """The splash must give up and show a diagnostic, not hang forever."""
    with pytest.raises(TimeoutError, match="did not become ready"):
        wait_for_health(pick_free_port(), "token", timeout=0.5)


def test_the_service_binds_only_to_loopback(
    live_server: tuple[int, str, Settings],
) -> None:
    """127.0.0.1, never 0.0.0.0 — the till is not a network service."""
    import socket

    port, token, _ = live_server
    wait_for_health(port, token, timeout=20.0)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        assert probe.connect_ex(("127.0.0.1", port)) == 0

    hostname_ip = socket.gethostbyname(socket.gethostname())
    if hostname_ip.startswith("127."):
        pytest.skip("this machine has no non-loopback address to probe")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        assert probe.connect_ex((hostname_ip, port)) != 0, (
            "the local API is reachable from the LAN"
        )


def test_a_full_login_over_real_http(live_server: tuple[int, str, Settings]) -> None:
    """The phase 1 exit criterion, over the wire the webview actually uses."""
    from app.data.repositories.users import CachedUserRepository
    from app.domain import permissions as perms
    from app.services.auth_service import AuthService, SessionStore

    port, token, settings = live_server
    wait_for_health(port, token, timeout=20.0)

    db = Database(settings.db_path)
    AuthService(
        users=CachedUserRepository(db),
        sessions=SessionStore(),
        cloud=None,
        store_code=settings.store_code,
        terminal_code=settings.terminal_code,
        settings=settings,
    ).seed_local_user(
        user_id="018f0000-0000-7000-8000-000000000001",
        employee_code="C001",
        full_name="Anita Rao",
        store_id="018f0000-0000-7000-8000-000000000100",
        pin="4913",
        roles=frozenset({perms.CASHIER}),
        permissions=perms.permissions_for(frozenset({perms.CASHIER})),
    )
    db.close()

    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {token}"}

    response = httpx.post(
        f"{base}/auth/login",
        json={"employee_code": "C001", "pin": "4913"},
        headers=headers,
        timeout=10.0,
    )
    assert response.status_code == 200, response.text
    assert response.json()["full_name"] == "Anita Rao"

    # And the register screen is denied margin, over the same connection.
    denied = httpx.get(f"{base}/reports/margin", headers=headers, timeout=10.0)
    assert denied.status_code == 403


def test_the_login_page_is_served(live_server: tuple[int, str, Settings]) -> None:
    port, token, _ = live_server
    wait_for_health(port, token, timeout=20.0)

    response = httpx.get(f"http://127.0.0.1:{port}/", timeout=10.0)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="root"' in response.text or "Sign in" in response.text

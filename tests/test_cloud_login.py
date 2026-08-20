"""Online login and the offline fallback — architecture §11.4.

The rule being pinned down here is subtle and easy to get backwards: when the
server is reachable and says *no*, the cache must not be consulted, or a
revoked PIN keeps working. When the server is unreachable, the cache is
exactly what keeps the shop trading.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from app.config import Settings
from app.data.repositories.users import CachedUserRepository
from app.domain import permissions as perms
from app.domain.identity import utcnow
from app.services.auth_service import (
    AuthService,
    LoginFailed,
    NoOfflineIdentity,
    SessionStore,
)
from app.services.supabase_auth import (
    SupabaseAuthClient,
    permissions_from_access_token,
    store_ids_from_access_token,
)

# Every test here reaches the OS credential store through the auth
# service, so the in-memory stub is required throughout this module.
pytestmark = pytest.mark.usefixtures("in_memory_keychain")

BASE_URL = "https://project.supabase.co"
LOGIN_URL = f"{BASE_URL}/functions/v1/authenticate-pin"
STORE_ID = "018f0000-0000-7000-8000-000000000100"

# A JWT with the claims the custom access token hook stamps in. Signature is
# not checked on the terminal — Postgres is the only party that can be trusted
# to verify it (architecture §1.3).
ACCESS_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIwMThmMDAwMC0wMDAwLTcwMDAtODAwMC0wMDAwMDAwMDAwMDEiLCJhcHBfbWV0YWRhdGE"
    "iOnsicGVybWlzc2lvbnMiOlsic2FsZS5jcmVhdGUiLCJwcm9kdWN0LnJlYWQiXSwic3RvcmVfaWRzIjp"
    "bIjAxOGYwMDAwLTAwMDAtNzAwMC04MDAwLTAwMDAwMDAwMDEwMCJdfX0"
    ".not-a-real-signature"
)


def cloud_payload(**overrides: object) -> dict:
    signed = utcnow()
    payload = {
        "access_token": ACCESS_TOKEN,
        "refresh_token": "refresh-abc",
        "pin_hash": "",  # filled in by the fixture
        "snapshot_signed_at": signed.isoformat(),
        "snapshot_expires_at": (signed + timedelta(days=14)).isoformat(),
        "user": {
            "id": "018f0000-0000-7000-8000-000000000001",
            "employee_code": "C001",
            "full_name": "Anita Rao",
            "store_id": STORE_ID,
            "status": "active",
            "roles": ["cashier"],
            "permissions": sorted(perms.permissions_for(frozenset({perms.CASHIER}))),
        },
    }
    payload.update(overrides)  # type: ignore[arg-type]
    return payload


@pytest.fixture
def online_auth(
    users: CachedUserRepository, sessions: SessionStore, settings: Settings
) -> AuthService:
    return AuthService(
        users=users,
        sessions=sessions,
        cloud=SupabaseAuthClient(BASE_URL, "anon-key"),
        store_code="ST01",
        terminal_code="T1",
        settings=settings,
    )


@respx.mock
async def test_online_login_caches_a_snapshot(
    online_auth: AuthService, users: CachedUserRepository, settings: Settings
) -> None:
    from app.security.pins import hash_pin

    payload = cloud_payload(pin_hash=hash_pin("4913", settings))
    respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=payload))

    session = await online_auth.login("C001", "4913")

    assert session.offline is False
    assert session.store_id == STORE_ID

    cached = users.get_by_employee_code("C001")
    assert cached is not None
    assert cached.permissions == perms.permissions_for(frozenset({perms.CASHIER}))


@respx.mock
async def test_a_cached_cashier_logs_in_when_the_network_drops(
    online_auth: AuthService, settings: Settings
) -> None:
    from app.security.pins import hash_pin

    payload = cloud_payload(pin_hash=hash_pin("4913", settings))
    route = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=payload))
    await online_auth.login("C001", "4913")

    route.mock(side_effect=httpx.ConnectError("no route to host"))
    session = await online_auth.login("C001", "4913")

    assert session.offline is True
    assert perms.SALE_CREATE in session.permissions


@respx.mock
async def test_a_server_rejection_does_not_fall_back_to_the_cache(
    online_auth: AuthService, settings: Settings
) -> None:
    """The cache is a fallback for outages, never for a changed PIN."""
    from app.security.pins import hash_pin

    payload = cloud_payload(pin_hash=hash_pin("4913", settings))
    route = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=payload))
    await online_auth.login("C001", "4913")

    route.mock(return_value=httpx.Response(401, json={"error": "invalid_credentials"}))

    with pytest.raises(LoginFailed):
        await online_auth.login("C001", "4913")


@respx.mock
async def test_a_disabled_account_purges_the_snapshot(
    online_auth: AuthService, users: CachedUserRepository, settings: Settings
) -> None:
    from app.security.pins import hash_pin

    payload = cloud_payload(pin_hash=hash_pin("4913", settings))
    route = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=payload))
    await online_auth.login("C001", "4913")
    assert users.get_by_employee_code("C001") is not None

    route.mock(return_value=httpx.Response(403, json={"error": "account_disabled"}))

    with pytest.raises(LoginFailed):
        await online_auth.login("C001", "4913")
    assert users.get_by_employee_code("C001") is None


@respx.mock
async def test_a_broken_function_is_an_outage_not_a_rejection(
    online_auth: AuthService, settings: Settings
) -> None:
    from app.security.pins import hash_pin

    payload = cloud_payload(pin_hash=hash_pin("4913", settings))
    route = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=payload))
    await online_auth.login("C001", "4913")

    route.mock(return_value=httpx.Response(500, text="boom"))
    session = await online_auth.login("C001", "4913")

    assert session.offline is True


@respx.mock
async def test_never_seen_here_and_offline_says_so(online_auth: AuthService) -> None:
    respx.post(LOGIN_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(NoOfflineIdentity):
        await online_auth.login("Z999", "4913")


# ── JWT claims ──────────────────────────────────────────────────────────────


def test_claims_are_read_from_the_access_token() -> None:
    assert permissions_from_access_token(ACCESS_TOKEN) == frozenset(
        {"sale.create", "product.read"}
    )
    assert store_ids_from_access_token(ACCESS_TOKEN) == frozenset({STORE_ID})

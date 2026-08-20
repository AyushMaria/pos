"""Supabase authentication client.

Only the anon key ever leaves the repository (architecture §1.7) — PIN
verification and session minting need privileged access, so they happen inside
the ``authenticate-pin`` Edge Function where the service_role key stays on the
server. The terminal sends a code and a PIN and gets back a session plus the
snapshot it needs to authenticate the same person offline tomorrow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=3.0)


class AuthUnavailable(RuntimeError):
    """The cloud could not be reached. Callers should fall back to the cache."""


class InvalidCredentials(RuntimeError):
    """The employee code or PIN was rejected by the server."""


class AccountDisabled(RuntimeError):
    """The server recognises the employee but the account is not active."""


@dataclass(frozen=True)
class CloudSession:
    """What a successful online authentication yields."""

    user_id: str
    employee_code: str
    full_name: str
    store_id: str
    status: str
    roles: frozenset[str]
    permissions: frozenset[str]
    pin_hash: str
    access_token: str
    refresh_token: str
    snapshot_signed_at: datetime
    snapshot_expires_at: datetime


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _session_from_payload(payload: dict[str, Any]) -> CloudSession:
    user = payload["user"]
    return CloudSession(
        user_id=user["id"],
        employee_code=user["employee_code"],
        full_name=user["full_name"],
        store_id=user["store_id"],
        status=user.get("status", "active"),
        roles=frozenset(user.get("roles", [])),
        permissions=frozenset(user.get("permissions", [])),
        pin_hash=payload["pin_hash"],
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        snapshot_signed_at=_parse_ts(payload["snapshot_signed_at"]),
        snapshot_expires_at=_parse_ts(payload["snapshot_expires_at"]),
    )


class SupabaseAuthClient:
    def __init__(self, base_url: str, anon_key: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {self.anon_key}",
            "Content-Type": "application/json",
        }

    async def authenticate_pin(
        self, employee_code: str, pin: str, store_code: str, terminal_code: str
    ) -> CloudSession:
        client = await self._http()
        try:
            response = await client.post(
                f"{self.base_url}/functions/v1/authenticate-pin",
                headers=self._headers,
                json={
                    "employee_code": employee_code,
                    "pin": pin,
                    "store_code": store_code,
                    "terminal_code": terminal_code,
                },
            )
        except httpx.HTTPError as exc:
            raise AuthUnavailable(str(exc)) from exc

        if response.status_code == 401:
            raise InvalidCredentials("invalid employee code or PIN")
        if response.status_code == 403:
            raise AccountDisabled(response.json().get("error", "account_disabled"))
        if response.status_code >= 500:
            # A broken function is an outage, not a rejection — the till should
            # fall back to the cache rather than refusing to open.
            raise AuthUnavailable(f"authenticate-pin returned {response.status_code}")
        response.raise_for_status()

        return _session_from_payload(response.json())

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        """Exchange a refresh token for a new access token. Returns (access, refresh)."""
        client = await self._http()
        try:
            response = await client.post(
                f"{self.base_url}/auth/v1/token",
                params={"grant_type": "refresh_token"},
                headers=self._headers,
                json={"refresh_token": refresh_token},
            )
        except httpx.HTTPError as exc:
            raise AuthUnavailable(str(exc)) from exc

        if response.status_code in (400, 401):
            raise InvalidCredentials("refresh token rejected")
        response.raise_for_status()
        body = response.json()
        return body["access_token"], body["refresh_token"]


def claims_from_access_token(access_token: str) -> dict[str, Any]:
    """Read the custom-hook claims out of a JWT without verifying the signature.

    Verification belongs to Postgres, which is the only party that can be
    trusted to do it (architecture §1.3). The terminal reads these claims for
    UX and audit only — every one of them is re-enforced by RLS on sync.
    """
    return jwt.decode(access_token, options={"verify_signature": False})


def permissions_from_access_token(access_token: str) -> frozenset[str]:
    claims = claims_from_access_token(access_token)
    metadata = claims.get("app_metadata") or {}
    return frozenset(metadata.get("permissions") or [])


def store_ids_from_access_token(access_token: str) -> frozenset[str]:
    claims = claims_from_access_token(access_token)
    metadata = claims.get("app_metadata") or {}
    return frozenset(metadata.get("store_ids") or [])

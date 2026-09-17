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


class ApproverNotPermitted(RuntimeError):
    """The PIN was right and the person does not hold what they were asked to lend.

    Separate from `InvalidCredentials` because it is a different sentence at
    the counter — fetch somebody more senior, rather than retype. It is only
    ever raised after a correct PIN, so it discloses nothing: you have to be
    the approver already to learn that the approver lacks a key.
    """


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


@dataclass(frozen=True)
class CloudApprover:
    """What a successful online *authorisation* yields — note what is missing.

    No access token, no refresh token, no PIN hash. `CloudSession` above has
    all three because logging in is becoming somebody; this is verifying
    somebody and walking away. The two dataclasses sitting next to each other
    is deliberate: the difference between them is the entire security property
    of the override flow.
    """

    user_id: str
    employee_code: str
    full_name: str
    store_id: str
    roles: frozenset[str]
    permission: str
    verified_at: datetime


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
        if response.status_code != 200:
            # Everything that is not an explicit decision about this PIN is an
            # outage, and the till falls back to the cache rather than
            # refusing to open. 5xx is a broken function; 404 is the Edge
            # Function not deployed to this project at all; 400 and 429 come
            # from the gateway. None of them are a judgement about whether
            # this cashier may sign in.
            #
            # This used to be `raise_for_status()`, which turned a 404 into an
            # httpx.HTTPStatusError that nothing up the stack caught — so a
            # project missing the Edge Function did not fall back offline, it
            # returned 500 from /auth/login and the till would not open at
            # all. The one deployment mistake most likely to happen on a new
            # project was also the one that bricked the shop.
            raise AuthUnavailable(f"authenticate-pin returned {response.status_code}")

        return _session_from_payload(response.json())

    async def authorize_override(
        self, approver_code: str, pin: str, permission: str, store_code: str
    ) -> CloudApprover:
        """Ask the cloud to verify a second person, and nothing more.

        The asymmetry with `authenticate_pin` is the point: that one returns a
        session and this one must not. `authorize-override` answers "is this
        PIN right, and does this person hold this key in this store?" and the
        grant is minted here, on the terminal, into the session the cashier
        already has.

        No `pin_hash` comes back either, so authorising does not cache the
        approver on this till. An online override works for a supervisor who
        has never touched this terminal; an offline one still does not, and
        those stay different acts.
        """
        client = await self._http()
        try:
            response = await client.post(
                f"{self.base_url}/functions/v1/authorize-override",
                headers=self._headers,
                json={
                    "approver_code": approver_code,
                    "pin": pin,
                    "permission": permission,
                    "store_code": store_code,
                },
            )
        except httpx.HTTPError as exc:
            raise AuthUnavailable(str(exc)) from exc

        if response.status_code == 401:
            raise InvalidCredentials("invalid employee code or PIN")
        if response.status_code == 403:
            error = response.json().get("error", "")
            if error == "approver_lacks_permission":
                raise ApproverNotPermitted(permission)
            raise AccountDisabled(error or "account_disabled")
        if response.status_code != 200:
            # Same rule as `authenticate_pin`: anything that is not an explicit
            # decision about this PIN is an outage, and the caller falls back to
            # the cache rather than refusing. A 404 here is the likeliest of
            # all — this function is newer than the projects it will be
            # deployed to, and a till whose supervisor cannot authorise a void
            # because a deployment is one function behind should fall back to
            # the path that has always worked.
            raise AuthUnavailable(f"authorize-override returned {response.status_code}")

        payload = response.json()
        approver = payload["approver"]
        return CloudApprover(
            # Indexed, not `.get`. This is a contract between two files in one
            # repository; a missing key is a deployment that does not match
            # this build, and a KeyError names it better than a None would.
            user_id=approver["user_id"],
            employee_code=approver["employee_code"],
            full_name=approver["full_name"],
            store_id=approver["store_id"],
            roles=frozenset(approver.get("roles", [])),
            permission=payload["permission"],
            verified_at=_parse_ts(payload["verified_at"]),
        )

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

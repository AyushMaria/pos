"""Purging identities the cloud no longer recognises — architecture §11.4.

`0004_access_token_hook.sql` has asserted since it was written that "the
terminal's next sync purges the offline snapshot". Nothing implemented it, so
a dismissed supervisor's cached row sat on every till they had ever signed in
at, authorising overrides offline until its fourteen days ran out.

## What this is not

Not a pull. The puller runs under the signed-in cashier's own token, and
`employees_select_self_or_manager` lets a cashier read exactly one row — their
own. Adding `employees` to `ENTITIES` would have revoked the one person whose
deactivation is already caught at next login and missed every supervisor, and
it would have looked like it worked: a keyset pull returning one row is
indistinguishable from one that returned everything there was.

So it asks a function that holds the service key, about ids this terminal
already has, and gets back the subset that is gone.

## Two layers refuse, at two different speeds

Worth being exact, because "the till drops them" is not one event:

* **The terminal refuses immediately.** The cached row is purged, so that
  person cannot sign in here or authorise an override, offline or otherwise.
* **The cloud refuses within the hour.** Permissions live in the JWT and the
  token TTL is an hour (`0004_access_token_hook.sql` says so in its header).
  A revoked cashier's *existing* access token keeps satisfying RLS until it
  expires. That is the documented trade rather than a gap, and it is the
  reason a proof of this slice has to name which layer did the refusing —
  "deactivate, sync, watch the till drop them" passes on the local purge alone
  and says nothing about the server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=3.0)


@dataclass
class RevocationResult:
    """What one check found. `checked` is False when nobody answered."""

    checked: bool = False
    revoked: tuple[str, ...] = ()
    error: str | None = None


class RevocationChecker:
    def __init__(
        self,
        *,
        base_url: str,
        anon_key: str,
        token_provider: Callable[[], str | None],
        store_code: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        self.token_provider = token_provider
        self.store_code = store_code
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self._client

    async def check(self, user_ids: list[str]) -> RevocationResult:
        """Ask which of these are gone.

        **Fails open, deliberately.** Every error here — no network, the
        function not deployed, a 500, an expired token — returns
        `checked=False` and nothing is purged. A shop cannot be locked out of
        its own till by a network blip, and this runs on a background cycle
        where nobody is watching.

        That makes the sealed snapshot TTL load-bearing rather than
        belt-and-braces: with this path unavailable, fourteen days is the only
        thing bounding a dismissed employee, and it is doing that job alone.
        `app/security/snapshot_mac.py` is what makes that number worth
        anything, since before it was sealed it could be edited in the file.
        """
        token = self.token_provider()
        if not token or not user_ids:
            return RevocationResult()

        client = await self._http()
        try:
            response = await client.post(
                f"{self.base_url}/functions/v1/check-revocations",
                headers={
                    "apikey": self.anon_key,
                    # The caller's own session, not the anon key. The function
                    # refuses the anon key precisely because it is on every
                    # request this project makes and proves nothing.
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"store_code": self.store_code, "user_ids": user_ids},
            )
        except httpx.HTTPError as exc:
            return RevocationResult(error=str(exc))

        if response.status_code != 200:
            return RevocationResult(error=f"check-revocations returned {response.status_code}")

        revoked = response.json().get("revoked") or []
        return RevocationResult(checked=True, revoked=tuple(revoked))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class RevocationSweep:
    """One pass: ask, purge, and decide what happens to the person at the till.

    Takes the session as two callables rather than importing `SessionStore`.
    `app.sync` and `app.services` are independent siblings in the import
    contract, and the pusher already does the same thing for its access token
    — injection is how this codebase crosses that line.
    """

    def __init__(
        self,
        *,
        checker: RevocationChecker,
        users,
        current_user_id: Callable[[], str | None],
        on_self_revoked: Callable[[], None],
        publish: Callable | None = None,
    ) -> None:
        self.checker = checker
        self.users = users
        self.current_user_id = current_user_id
        self.on_self_revoked = on_self_revoked
        self.publish = publish

    async def run(self) -> RevocationResult:
        cached = self.users.cached_user_ids()
        result = await self.checker.check(cached)
        if not result.checked:
            if result.error:
                log.warning("revocation check skipped: %s", result.error)
            return result

        if not result.revoked:
            return result

        signed_in = self.current_user_id()
        for user_id in result.revoked:
            self.users.revoke(user_id)
            log.warning("purged the cached identity for %s", user_id)

        if signed_in in result.revoked:
            # **The open basket finishes.**
            #
            # Clearing the session here is the obvious implementation and the
            # wrong one. At a counter it means a customer with eleven items
            # scanned watches the screen drop to a login prompt because
            # somebody in an office processed a leaver at 11:40. A
            # deactivation is almost never an emergency — it is a leaver or a
            # role change — and finishing the sale in front of you is nearly
            # always right.
            #
            # So the refusal lands at the next sale, not this one. The cached
            # row is already gone, so they cannot sign in again or authorise
            # anything; what they can still do is take the money for the
            # basket that is already on the screen.
            self.on_self_revoked()

        if self.publish is not None:
            await self.publish(
                "auth.revoked",
                {"user_ids": list(result.revoked), "yours": signed_in in result.revoked},
            )
        return result

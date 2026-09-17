"""The supervisor override (architecture §11.3).

One endpoint, and the only one in the application whose purpose is to *hand
out* a permission. Everything else in `app/api` asks whether the session may
do a thing; this is how a session that may not comes to may, for ninety
seconds, with a second person's PIN and a row in the audit log naming both of
them.

**It carries no `require()`, and that is a decision rather than an oversight.**
A permission gate here would be circular: the cashier who needs an override is
by definition the one who does not hold the key, so any gate strict enough to
matter would refuse exactly the people the endpoint exists for. What it does
require is a *session* — a grant is lent to somebody, and there has to be a
somebody. `tests/test_permission_matrix.py` records it in `UNGATED` beside
`POST /sync/push`, which is where a reader should be able to find every route
that answers to nothing.

The refusals are deliberately distinguishable, because the person reading them
is a supervisor standing at a till who needs to know what to do next: a PIN to
retype, a wait to sit out, a colleague to fetch, or a cable to plug in. That
does disclose whether an employee code is cached on this terminal — the same
disclosure `/auth/login` already makes, for the same reason, and one that says
nothing about anybody's credential.
"""

from __future__ import annotations

import asyncio
import math
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentSession, get_auth_service
from app.api.schemas import OverrideRequest, OverrideResponse
from app.domain.identity import NotOverridable, OverrideGrant, utcnow
from app.services.auth_service import (
    ApproverLacksPermission,
    AuthService,
    CannotAuthoriseSelf,
    LoginFailed,
    NoOfflineIdentity,
    NothingToAuthorise,
    NotSignedIn,
    PinLocked,
)

router = APIRouter(prefix="/overrides", tags=["overrides"])

#: The same flattening `/auth/login` applies, for the same reason: a wrong PIN
#: and an unknown-but-cached code must not be told apart by a stopwatch.
_FAILURE_DELAY_SECONDS = 0.4

#: 423 rather than 429. "Locked" is a fact about the account that a wait will
#: clear; "too many requests" is a fact about the caller, which would be the
#: wrong thing to tell a supervisor who has typed a PIN twice on a terminal
#: somebody else spent the afternoon guessing at.
HTTP_423_LOCKED = 423


def _to_response(grant: OverrideGrant) -> OverrideResponse:
    remaining = (grant.expires_at - utcnow()).total_seconds()
    return OverrideResponse(
        permission=grant.permission,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        # Floor, never round: a countdown that claims one second it does not
        # have is a modal that hands control back into a 403.
        expires_in_seconds=max(0, math.floor(remaining)),
        approver_code=grant.approver_code,
        actor_code=grant.actor_code,
    )


@router.post("/authorize", response_model=OverrideResponse)
async def authorize(
    body: OverrideRequest,
    session: CurrentSession,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> OverrideResponse:
    """Verify a second person and lend the signed-in cashier a permission.

    `session` is taken as a dependency and then not used, deliberately. The
    service reads the current session for itself — it has to, because the
    grant is written into the store rather than returned to be applied — and
    naming it here is what makes "no session" a 401 like every other route
    instead of a 400 that only this endpoint knows how to produce.
    """
    try:
        grant = await auth.authorize_override(
            approver_code=body.approver_code,
            pin=body.pin,
            permission=body.permission,
        )
    except NotOverridable as exc:
        # Not a failure of this request so much as of the caller's idea of the
        # world: RLS would refuse the write this authorises, hours later and
        # into the failures queue, where nobody is watching.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{exc.permission} cannot be lent by an override. It needs the "
            "role, not a supervisor's PIN.",
        ) from exc
    except NothingToAuthorise as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"You already have permission to do that ({exc.permission}). "
            "No override is needed.",
        ) from exc
    except NotSignedIn as exc:  # pragma: no cover - the dependency gets there first
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_signed_in") from exc
    except NoOfflineIdentity as exc:
        # Actionable, and the action is not "retype": sign in once on this
        # till, or reconnect. 503 is what `/auth/login` says for the same
        # condition and the UI already tells it apart from a rejection.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except CannotAuthoriseSelf as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "An override has to be authorised by somebody else.",
        ) from exc
    except ApproverLacksPermission as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{exc.approver_code} cannot authorise that. Ask a supervisor or "
            "the owner.",
        ) from exc
    except PinLocked as exc:
        # No delay here: the lock is meant to be discoverable, because the
        # screen has something useful to say and waiting is the only thing
        # that will help.
        until = exc.until.isoformat() if exc.until is not None else "later"
        raise HTTPException(
            HTTP_423_LOCKED,
            f"Too many wrong PINs for {exc.employee_code}. That account "
            f"cannot authorise anything until {until}.",
        ) from exc
    except LoginFailed as exc:
        await asyncio.sleep(_FAILURE_DELAY_SECONDS)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    return _to_response(grant)

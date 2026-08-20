"""Authentication endpoints.

Note what is *not* here: no password field, no email, no token in the
response body. The cashier's credential is an employee code and a PIN, the
access token stays in the service process, and the browser side holds only the
per-launch session token it was handed in its URL.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentSession, get_auth_service
from app.api.schemas import LoginRequest, SessionResponse
from app.domain.identity import Session
from app.services.auth_service import (
    AuthService,
    LoginFailed,
    NoOfflineIdentity,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# A wrong PIN costs the attacker at least this long. argon2id already makes
# each attempt expensive; this flattens the timing difference between "no such
# employee" and "wrong PIN", which is otherwise the cheapest way to enumerate
# staff codes.
_FAILURE_DELAY_SECONDS = 0.4


def _to_response(session: Session) -> SessionResponse:
    return SessionResponse(
        user_id=session.user_id,
        employee_code=session.employee_code,
        full_name=session.full_name,
        store_id=session.store_id,
        roles=sorted(session.roles),
        permissions=sorted(session.permissions),
        authenticated_at=session.authenticated_at,
        offline=session.offline,
    )


@router.post("/login", response_model=SessionResponse)
async def login(
    body: LoginRequest,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> SessionResponse:
    try:
        session = await auth.login(body.employee_code, body.pin)
    except LoginFailed as exc:
        await asyncio.sleep(_FAILURE_DELAY_SECONDS)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except NoOfflineIdentity as exc:
        # Distinct from a rejection: this one a cashier can act on, which is
        # what the definition of done asks for (execution plan §5).
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return _to_response(session)


@router.get("/session", response_model=SessionResponse)
def read_session(session: CurrentSession) -> SessionResponse:
    return _to_response(session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(auth: Annotated[AuthService, Depends(get_auth_service)]) -> None:
    auth.logout()

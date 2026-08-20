"""Reporting endpoints — placeholder.

Real reports are phase 8. What exists now is the *gate*: a single endpoint
behind ``report.margin`` so that the permission matrix has something to assert
against at the API layer in phase 1, alongside the RLS test that asserts the
same rule at the layer that actually enforces it.

Deliberately answers 501 for a permitted caller. A cashier gets 403; a manager
gets "not built yet". Nobody gets a number that isn't real.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import require
from app.domain import permissions
from app.domain.identity import Session

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/margin")
def margin_report(
    _: Annotated[Session, Depends(require(permissions.REPORT_MARGIN))],
) -> None:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "reports arrive in phase 8")

"""The startup gate.

The splash polls this until it answers, then the webview loads the register.
It is deliberately unauthenticated and deliberately cheap: if this endpoint
needs the database to be healthy in order to report that the database is
unhealthy, the diagnostics screen has nothing to show.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app import __version__
from app.api.deps import get_db, get_settings_dep
from app.api.schemas import HealthResponse
from app.config import Settings
from app.data.db import Database
from app.data.migrations import latest_version

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> HealthResponse:
    applied = db.user_version
    expected = latest_version()
    return HealthResponse(
        status="ready" if applied == expected else "degraded",
        version=__version__,
        schema_version=applied,
        expected_schema_version=expected,
        migrations_pending=applied < expected,
        store_code=settings.store_code,
        terminal_code=settings.terminal_code,
        cloud_configured=settings.cloud_configured,
    )

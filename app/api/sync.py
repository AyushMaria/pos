"""Sync status — architecture §4, §9.

The cashier needs one thing from this: whether the sales they have taken have
left the building. Everything else here is for whoever they call when the
answer is no.

Nothing on this router can start or stop selling. That is deliberate — sync is
never on the request path of a sale (§1.1), so a broken sync must never be
able to present itself as a broken till.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from app.api.deps import CurrentSession, require
from app.api.schemas import (
    SyncFailureOut,
    SyncFailuresResponse,
    SyncRetryRequest,
    SyncRetryResponse,
    SyncStatusResponse,
)
from app.domain import permissions
from app.domain.identity import Session

router = APIRouter(prefix="/sync", tags=["sync"])


def _engine(request: Request):  # type: ignore[no-untyped-def]
    engine = getattr(request.app.state, "sync", None)
    if engine is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no sync engine: this terminal has no Supabase project configured",
        )
    return engine


@router.get("/status", response_model=SyncStatusResponse)
def sync_status(request: Request, session: CurrentSession) -> SyncStatusResponse:
    """Backlog depth, last push and pull, and anything that went wrong.

    Read live rather than from the last cycle, because the number a cashier
    cares about — how many sales are still here — changes with every sale
    they take, not every ninety seconds.
    """
    engine = _engine(request)
    return SyncStatusResponse(**engine.snapshot().as_dict())


@router.post("/push", response_model=SyncStatusResponse)
async def push_now(request: Request, session: CurrentSession) -> SyncStatusResponse:
    """Drain the queue now.

    For the person who has just plugged the network back in and would rather
    watch the backlog empty than trust that it will.
    """
    engine = _engine(request)
    await engine.cycle()
    return SyncStatusResponse(**engine.snapshot().as_dict())


@router.get("/failures", response_model=SyncFailuresResponse)
def sync_failures(
    request: Request,
    session: Annotated[Session, Depends(require(permissions.REPORT_SALES_STORE))],
) -> SyncFailuresResponse:
    """What was set aside, and why.

    A quarantined sale is in the local database and not in the cloud, which
    means the day's totals differ between the two. Someone has to be able to
    see which ones and decide what to do, so this shows the payload that was
    actually sent rather than a row id and an apology.
    """
    engine = _engine(request)
    return SyncFailuresResponse(
        items=[
            SyncFailureOut(
                id=int(row["id"]),
                outbox_id=int(row["outbox_id"]) if row["outbox_id"] else None,
                error=row["error"],
                failed_at=row["failed_at"],
                entity=_entity_of(row["payload_json"]),
            )
            for row in engine.outbox.failures()
        ]
    )


@router.post("/failures/retry", response_model=SyncRetryResponse)
async def retry_failures(
    request: Request,
    body: SyncRetryRequest,
    session: Annotated[Session, Depends(require(permissions.REPORT_SALES_STORE))],
) -> SyncRetryResponse:
    """Put quarantined sales back in the queue, and try them now.

    The other half of `/sync/failures`. Seeing why a sale was refused is only
    useful if something can be done once the cause is fixed, and until this
    existed nothing could: `quarantine()` marks the outbox row synced, so the
    drain loop steps over it forever. A sale refused by a bug that was later
    corrected stayed on the terminal permanently.

    Manager-gated on the same permission as the failures list. A cashier
    should not be deciding that a refusal no longer applies.

    Pushing immediately rather than waiting for the next cycle is the point:
    whoever pressed this is standing there wanting to know whether the fix
    worked, and a fresh failure is a more useful answer than a spinner.
    """
    engine = _engine(request)
    requeued = await run_in_threadpool(engine.outbox.retry_failures, body.failure_ids)
    if requeued:
        await engine.cycle()
    return SyncRetryResponse(
        requeued=requeued,
        status=SyncStatusResponse(**engine.snapshot().as_dict()),
    )


def _entity_of(payload_json: str) -> str | None:
    try:
        payload = json.loads(payload_json)
    except ValueError:
        return None
    return payload.get("entity") if isinstance(payload, dict) else None

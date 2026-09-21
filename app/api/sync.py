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

    `CurrentSession` rather than `require(...)`, and that is a decision rather
    than an oversight — phase 7 slice 2 made it deliberately, because the
    permission matrix asked the question. Draining early is the same act as
    waiting for the next cycle: every row was written under a permission that
    was checked when it was written, the push runs under the terminal's own
    credentials, and RLS refuses each row on its own merits either way. There
    is nothing here to escalate to.

    The two neighbours below require `report.sales.store` and should. They
    show what was quarantined and put it back in the queue — reading refused
    sales and deciding a refusal no longer applies are both judgements about
    other people's work. This one only changes when.

    **`session` is load-bearing, and unused on purpose.** No permission is not
    no session: the argument never appears in the body, and exists so FastAPI
    resolves the dependency and refuses a caller who has not signed in.
    Deleting it — the obvious tidy-up for an unused parameter sitting under a
    docstring that says nothing is required — would leave the drain reachable
    by anything that can reach the port.
    `test_sync_push_is_deliberately_ungated` asserts that 401, so the tidy-up
    fails loudly instead of silently.
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
                reference=_reference_of(row["payload_json"]),
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


def _reference_of(payload_json: str) -> str | None:
    """What a manager would call this row, read off the envelope it carries.

    An `outbox_id` and an entity name are what the system calls it; a
    receipt number is what the person who took the money calls it. The
    failures screen exists for the second person.
    """
    try:
        payload = json.loads(payload_json)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    entity = payload.get("entity")
    if entity == "sale":
        return data.get("receipt_no")
    if entity == "stock_movement":
        return data.get("reason")
    if entity == "unknown_scan":
        return data.get("barcode")
    if entity in ("override", "audit"):
        after = data.get("after_json")
        if isinstance(after, str):
            try:
                after = json.loads(after)
            except ValueError:
                after = None
        if isinstance(after, dict) and after.get("actor_code"):
            return f"{after['actor_code']} ← {after.get('approver_code', '?')}"
        return data.get("action")
    return None

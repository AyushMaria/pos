"""Shifts — phase 8 slices 1 and 2.

Open the day, move cash, read the X-report, close the day. Everything here
is local-first: the close writes to SQLite and the outbox, and the badge
carries it to the cloud when the line is up. Nothing on this router waits.

Permissions, from §11.1 and the RLS that has existed since 0003:

* opening a shift and reading the X-report need `sale.create` — the cashier
  opens their own day, and the insert policy says `user_id = auth.uid()`;
* a cash movement needs `cash.payout`, which a cashier does not hold. Lending
  it through the override modal waits for slice 3, when the cloud policy can
  accept a row that names an approver (see `move_cash`);
* closing needs `shift.close`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import get_shift_service, require
from app.api.schemas import (
    CashMovementRequest,
    CashMovementResponse,
    CloseShiftRequest,
    CloseShiftResponse,
    MoneyOut,
    OpenShiftRequest,
    ShiftFiguresOut,
    ShiftOut,
    XReportResponse,
)
from app.data.repositories.shifts import OpenShift
from app.domain import permissions
from app.domain.identity import Session
from app.domain.money import Money
from app.domain.shift import ShiftFigures
from app.services.shift_service import (
    NoOpenShift,
    ShiftAlreadyOpen,
    ShiftRefused,
    ShiftService,
)

router = APIRouter(prefix="/shifts", tags=["shifts"])

Shifts = Annotated[ShiftService, Depends(get_shift_service)]


def _shift_out(shift: OpenShift) -> ShiftOut:
    return ShiftOut(
        id=shift.id,
        opened_at=shift.opened_at.isoformat(),
        opened_by=shift.user_id,
        opening_float=MoneyOut.of(shift.opening_float),
    )


def _figures_out(f: ShiftFigures, *, counted: Money | None) -> ShiftFiguresOut:
    """The X-report leaves `expected_cash` out. That is a rule, not an omission."""
    return ShiftFiguresOut(
        cash_sales=MoneyOut.of(f.cash_sales),
        upi_attested=MoneyOut.of(f.upi_attested),
        upi_verified=MoneyOut.of(f.upi_verified),
        cash_in=MoneyOut.of(f.cash_in),
        cash_out=MoneyOut.of(f.cash_out),
        rounding=MoneyOut.of(f.rounding),
        takings=MoneyOut.of(f.takings),
        under_review_count=f.under_review_count,
        under_review_total=MoneyOut.of(f.under_review_total),
        sales_count=f.sales_count,
        expected_cash=None if counted is None else MoneyOut.of(f.expected_cash),
        counted_cash=None if counted is None else MoneyOut.of(counted),
        variance=None if counted is None else MoneyOut.of(f.variance(counted)),
    )


@router.post("/open", response_model=ShiftOut, status_code=status.HTTP_201_CREATED)
def open_shift(
    body: OpenShiftRequest,
    shifts: Shifts,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> ShiftOut:
    """Open the day with what is in the drawer.

    Refused with 409 if a shift is already open on this terminal. The person
    who opened it is named, because the fix is to find them, not to force it.
    """
    try:
        opened = shifts.open(session, Money(body.opening_float_paise))
    except ShiftAlreadyOpen as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ShiftRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return _shift_out(opened)


@router.get("/current", response_model=ShiftOut)
def current_shift(
    shifts: Shifts,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> ShiftOut:
    """The open shift, or 404 — which is what the register screen asks on load."""
    current = shifts.current()
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no shift is open")
    return _shift_out(current)


@router.post("/cash", response_model=CashMovementResponse)
def move_cash(
    body: CashMovementRequest,
    shifts: Shifts,
    session: Annotated[Session, Depends(require(permissions.CASH_PAYOUT))],
) -> CashMovementResponse:
    """Cash in or out of the drawer, with a reason, on the open shift.

    `cash.payout` is **not** overridable yet, on purpose. `cash_movements_insert`
    (0003) accepts a row only from a caller who holds the key, so a cashier
    lent it at the counter would have their payout refused by RLS hours later,
    into the failures queue — the silent-and-late shape the phase 7 decision
    exists to prevent. Widening the policy to accept a row that names an
    approver is slice 3's cloud change; until then a payout is a supervisor's
    act, `approved_by` stays null, and the column waits for its first writer.
    """
    try:
        movement_id = shifts.move_cash(
            session,
            direction=body.direction,
            amount=Money(body.amount_paise),
            reason=body.reason,
        )
    except NoOpenShift as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ShiftRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return CashMovementResponse(movement_id=movement_id)


@router.get("/current/x", response_model=XReportResponse)
def x_report(
    shifts: Shifts,
    session: Annotated[Session, Depends(require(permissions.SALE_CREATE))],
) -> XReportResponse:
    """The day so far. Sales, not the drawer: `expected_cash` is not here."""
    try:
        current, figures = shifts.x_report()
    except NoOpenShift as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return XReportResponse(shift=_shift_out(current), figures=_figures_out(figures, counted=None))


@router.post("/close", response_model=CloseShiftResponse)
def close_shift(
    body: CloseShiftRequest,
    request: Request,
    shifts: Shifts,
    session: Annotated[Session, Depends(require(permissions.SHIFT_CLOSE))],
) -> CloseShiftResponse:
    """Count the drawer and close the day.

    The figures come back with the expected cash and the variance — after
    the count has been committed, never before. The close is queued for the
    cloud and the sync loop is nudged after the commit, the same order as a
    sale: a failure to nudge can never be a failure to close.
    """
    current = shifts.current()
    if current is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no shift is open")
    try:
        close_id, figures = shifts.close(
            session, counted_cash=Money(body.counted_cash_paise), note=body.note
        )
    except ShiftRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    engine = getattr(request.app.state, "sync", None)
    if engine is not None:
        engine.nudge()

    return CloseShiftResponse(
        close_id=close_id,
        shift=_shift_out(current),
        figures=_figures_out(figures, counted=Money(body.counted_cash_paise)),
    )

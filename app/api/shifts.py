"""Shifts — phase 8 slices 1 and 2.

Open the day, move cash, read the X-report, close the day. Everything here
is local-first: the close writes to SQLite and the outbox, and the badge
carries it to the cloud when the line is up. Nothing on this router waits.

Permissions, from §11.1 and the RLS that has existed since 0003:

* opening a shift and reading the X-report need `sale.create` — the cashier
  opens their own day, and the insert policy says `user_id = auth.uid()`;
* a cash movement needs `cash.payout`, which a cashier can be lent through
  the override modal; the lending supervisor is recorded (see `move_cash`);
* closing needs `shift.close`, which is never lent.
* the Z-report of a closed shift needs `shift.close` too: it shows the
  variance, and the variance is the supervisor's to see first.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import get_admin_service, get_shift_service, require
from app.api.schemas import (
    CashMovementRequest,
    CashMovementResponse,
    CheckLineOut,
    CloseCheckResponse,
    CloseShiftRequest,
    CloseShiftResponse,
    MoneyOut,
    OpenShiftRequest,
    ShiftFiguresOut,
    ShiftOut,
    XReportResponse,
    ZReportPdfResponse,
    ZReportResponse,
)
from app.data.repositories.shifts import OpenShift
from app.domain import permissions
from app.domain.close_check import explain
from app.domain.identity import Session, utcnow
from app.domain.money import Money
from app.domain.shift import ShiftFigures
from app.domain.zreport import ZReport
from app.services import zreport_render
from app.services.admin_service import AdminRejected, AdminService, AdminUnavailable
from app.services.receipt_render import PdfUnavailable
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


def _figures_out(
    f: ShiftFigures, *, counted: Money | None, under_review: list[str] | None = None
) -> ShiftFiguresOut:
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
        under_review_receipts=list(under_review or []),
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

    A supervisor holding `cash.payout` approves their own movement and
    `approved_by` stays null. A cashier lent the key through the override
    modal writes a row naming the supervisor who lent it — the first writer
    of the column phase 1 put on the table. 0024 widened
    `cash_movements_insert` to `sale.create`, so the cashier's claim pushes
    it; `tests/test_rls.py` holds that line.
    """
    try:
        movement_id = shifts.move_cash(
            session,
            direction=body.direction,
            amount=Money(body.amount_paise),
            reason=body.reason,
            approved_by=session.approver_for(permissions.CASH_PAYOUT, now=utcnow()),
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
    return XReportResponse(
        shift=_shift_out(current),
        figures=_figures_out(
            figures, counted=None, under_review=shifts.under_review_receipts(current.id)
        ),
    )


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
        close_id, _ = shifts.close(
            session, counted_cash=Money(body.counted_cash_paise), note=body.note
        )
    except ShiftRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    engine = getattr(request.app.state, "sync", None)
    if engine is not None:
        engine.nudge()

    report = _z_or_404(shifts, current.id)
    return CloseShiftResponse(
        close_id=close_id,
        shift=_shift_out(current),
        figures=_z_figures(report),
        zreport_html=zreport_render.render_html(report),
    )


def _z_or_404(shifts: ShiftService, session_id: str) -> ZReport:
    report = shifts.z_report(session_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that shift has not closed")
    return report


def _z_figures(report: ZReport) -> ShiftFiguresOut:
    return _figures_out(
        report.figures,
        counted=report.counted_cash,
        under_review=list(report.under_review_receipts),
    )


@router.get("/{session_id}/z", response_model=ZReportResponse)
def z_report(
    session_id: str,
    shifts: Shifts,
    session: Annotated[Session, Depends(require(permissions.SHIFT_CLOSE))],
) -> ZReportResponse:
    """A closed shift's Z-report, from the figures stored at the close.

    Needs `shift.close`: the variance is on it, and the rule that the
    supervisor counts before seeing expected holds after the close too — a
    cashier reading tonight's Z learns what tomorrow's drawer should hold.
    """
    report = _z_or_404(shifts, session_id)
    return ZReportResponse(
        close_id=report.close_id,
        session_id=session_id,
        closed_at=report.closed_at.isoformat(),
        figures=_z_figures(report),
        zreport_html=zreport_render.render_html(report),
    )


@router.get("/{session_id}/check", response_model=CloseCheckResponse)
async def day_close_check(
    session_id: str,
    shifts: Shifts,
    admin: Annotated[AdminService, Depends(get_admin_service)],
    session: Annotated[Session, Depends(require(permissions.SHIFT_CLOSE))],
) -> CloseCheckResponse:
    """The cloud's figures for a closed shift beside the till's (0025).

    Cloud-direct, like the catalogue: it needs the internet and says so with
    a 503. A 404 means the cloud has no close for this shift yet — usually
    because it has not pushed. When the figures differ, the explanation uses
    this till's own outbox to say which sales have not arrived.
    """
    try:
        lines = await admin.day_close_check(session_id)
    except AdminUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"the check needs the internet: {exc}"
        ) from exc
    except AdminRejected as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    if not lines:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "the cloud has not received this close yet"
        )
    waiting, quarantined = shifts.undelivered_sales(session_id)
    return CloseCheckResponse(
        session_id=session_id,
        agrees=all(line.agrees for line in lines),
        lines=[
            CheckLineOut(figure=ln.figure, till=ln.till, cloud=ln.cloud, agrees=ln.agrees)
            for ln in lines
        ],
        explanation=explain(lines, waiting=waiting, quarantined=quarantined),
    )


@router.post("/{session_id}/z.pdf", response_model=ZReportPdfResponse)
def write_z_pdf(
    session_id: str,
    shifts: Shifts,
    request: Request,
    session: Annotated[Session, Depends(require(permissions.SHIFT_CLOSE))],
) -> ZReportPdfResponse:
    """Write the Z-report PDF under the data directory. On demand, like receipts."""
    report = _z_or_404(shifts, session_id)
    destination = zreport_render.zreport_path(request.app.state.settings.data_dir, report)
    try:
        zreport_render.render_pdf(report, destination)
    except PdfUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return ZReportPdfResponse(close_id=report.close_id, path=str(destination))

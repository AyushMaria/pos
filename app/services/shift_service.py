"""Opening, moving cash, and closing — phase 8 plan, decisions 1 and 2.

Orchestration only. The arithmetic is `app/domain/shift.py`; the writes are
`ShiftRepository`. What lives here is the two rules the plan fixed before any
of this was built:

* **A close is a row, not an update.** The shop closes at ten with the line
  down as often as not, and a close that needs the cloud is a shop that
  cannot go home. `close()` writes locally and queues; nothing here waits.
* **A sale needs an open shift.** `require_open()` is what the register asks
  before it opens a cart and what `SaleService.post` asks before it commits.
  The float typed at 8am is the number the variance at 10pm is measured
  against; a float typed at 10pm from memory is a guess.
"""

from __future__ import annotations

import logging

from app.data.repositories.shifts import OpenShift, ShiftRepository
from app.domain import shift
from app.domain.identity import Session, utcnow
from app.domain.money import Money
from app.domain.shift import ShiftFigures
from app.domain.zreport import ZReport

log = logging.getLogger(__name__)


class NoOpenShift(RuntimeError):
    """The register was asked to do something that needs a shift, and none is open."""

    def __init__(self) -> None:
        super().__init__("open a shift before selling")


class ShiftAlreadyOpen(RuntimeError):
    def __init__(self, opened_by: str) -> None:
        super().__init__(f"a shift is already open on this terminal (opened by {opened_by})")
        self.opened_by = opened_by


class ShiftRefused(ValueError):
    """A request that is wrong on its face — a negative float, an empty reason."""


class ShiftService:
    def __init__(
        self,
        shifts: ShiftRepository,
        *,
        terminal_code: str,
        store_name: str = "",
        store_gstin: str | None = None,
    ) -> None:
        self.shifts = shifts
        self.terminal_code = terminal_code
        self.store_name = store_name
        self.store_gstin = store_gstin or None

    # ── Opening ─────────────────────────────────────────────────────────────

    def current(self) -> OpenShift | None:
        return self.shifts.current(self.terminal_code)

    def require_open(self) -> OpenShift:
        current = self.current()
        if current is None:
            raise NoOpenShift()
        return current

    def open(self, session: Session, opening_float: Money) -> OpenShift:
        """Open the day. One per terminal; the index in 008 enforces it too."""
        if opening_float.is_negative:
            raise ShiftRefused("an opening float cannot be negative")
        current = self.current()
        if current is not None:
            raise ShiftAlreadyOpen(current.user_id)
        opened = self.shifts.open(
            terminal_id=self.terminal_code,
            user_id=session.user_id,
            opening_float=opening_float,
            opened_at=utcnow(),
            store_id=session.store_id,
        )
        log.info("shift %s opened with float %s", opened.id, opening_float)
        return opened

    # ── Cash in and out ─────────────────────────────────────────────────────

    def move_cash(
        self,
        session: Session,
        *,
        direction: str,
        amount: Money,
        reason: str,
        approved_by: str | None = None,
    ) -> str:
        """A payout for tea, or a float top-up. Always on the open shift.

        `approved_by` is the supervisor when a cashier was lent `cash.payout`
        through the override modal — the first use of the column phase 1 put
        on the table. The route decides who that is; this only records it.
        """
        if direction not in ("in", "out"):
            raise ShiftRefused(f"cash moves in or out, not {direction!r}")
        if not amount.is_positive:
            raise ShiftRefused("a cash movement needs an amount")
        if not reason.strip():
            raise ShiftRefused("a cash movement needs a reason")
        current = self.require_open()
        return self.shifts.record_cash_movement(
            session_id=current.id,
            direction=direction,
            amount=amount,
            reason=reason.strip(),
            actor_id=session.user_id,
            approved_by=approved_by,
            occurred_at=utcnow(),
        )

    # ── Closing ─────────────────────────────────────────────────────────────

    def figures(self, session_id: str, opening_float: Money) -> ShiftFigures:
        """The Z-report's numbers for a session, open or closed."""
        return shift.figures(
            opening_float=opening_float,
            sales=self.shifts.sales_for(session_id),
            payments=self.shifts.payments_for(session_id),
            movements=self.shifts.movements_for(session_id),
        )

    def x_report(self) -> tuple[OpenShift, ShiftFigures]:
        """The open shift's figures so far. Read-only; changes nothing."""
        current = self.require_open()
        return current, self.figures(current.id, current.opening_float)

    def close(
        self, session: Session, *, counted_cash: Money, note: str | None = None
    ) -> tuple[str, ShiftFigures]:
        """Count the drawer, write the close, queue it. Returns the close id.

        The figures are computed here, once, and stored with the close. A
        review resolved tomorrow changes tomorrow's numbers, not tonight's —
        the Z-report a supervisor signed is the Z-report.
        """
        if counted_cash.is_negative:
            raise ShiftRefused("a counted drawer cannot be negative")
        current = self.require_open()
        figures = self.figures(current.id, current.opening_float)
        close_id = self.shifts.close(
            session_id=current.id,
            closed_by=session.user_id,
            counted_cash=counted_cash,
            figures=figures,
            note=note,
            closed_at=utcnow(),
        )
        log.info(
            "shift %s closed: expected %s, counted %s, variance %s",
            current.id, figures.expected_cash, counted_cash, figures.variance(counted_cash),
        )
        return close_id, figures

    def under_review_receipts(self, session_id: str) -> list[str]:
        """What the X-report lists: under review as of now."""
        return self.shifts.under_review_receipts(session_id, as_of=utcnow())

    # ── The Z-report ────────────────────────────────────────────────────────

    def z_report(self, session_id: str) -> ZReport | None:
        """The closed shift as a document, from the stored close. None if the
        session never closed (or does not exist)."""
        closed = self.shifts.close_for(session_id)
        if closed is None:
            return None
        return ZReport(
            close_id=closed.close_id,
            session_id=session_id,
            store_name=self.store_name,
            store_gstin=self.store_gstin,
            terminal_code=closed.terminal_id,
            opened_at=closed.opened_at,
            opened_by=self.shifts.name_of(closed.opened_by),
            closed_at=closed.closed_at,
            closed_by=self.shifts.name_of(closed.closed_by),
            figures=closed.figures,
            counted_cash=closed.counted_cash,
            under_review_receipts=tuple(
                self.shifts.under_review_receipts(session_id, as_of=closed.closed_at)
            ),
            note=closed.note,
        )

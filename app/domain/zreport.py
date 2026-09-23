"""The Z-report — a closed shift as a document.

Phase 8 slice 2b. The close stored its figures (decision 1: the numbers a
supervisor signed are the numbers), so a Z-report is those stored figures
with names and times around them. Nothing here recomputes a total; a review
resolved tomorrow changes tomorrow's X-report, never tonight's Z.

Like `Receipt`, this is the one model every rendering reads — screen, text
and PDF — so the three can never disagree about a figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.money import Money
from app.domain.shift import ShiftFigures


@dataclass(frozen=True, slots=True)
class ZReport:
    close_id: str
    session_id: str
    store_name: str
    store_gstin: str | None
    terminal_code: str
    opened_at: datetime
    opened_by: str
    closed_at: datetime
    closed_by: str
    figures: ShiftFigures
    counted_cash: Money
    #: Receipt numbers of the sales still under review when the shift closed.
    #: Listed so the supervisor can chase each one; never in a total.
    under_review_receipts: tuple[str, ...]
    note: str | None

    @property
    def expected_cash(self) -> Money:
        return self.figures.expected_cash

    @property
    def variance(self) -> Money:
        """Positive is over; negative is short."""
        return self.figures.variance(self.counted_cash)

    @property
    def variance_word(self) -> str:
        if self.variance.is_zero:
            return "balanced"
        return "over" if self.variance.is_positive else "short"

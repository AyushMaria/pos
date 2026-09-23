"""The day-close check, as words — phase 8 slice 3.

The cloud recomputes a closed shift's figures from its own rows
(`public.day_close_check`, migration 0025) and returns them beside the ones
the till stored. This turns the two columns into the sentence a supervisor
reads. It knows nothing about HTTP or SQLite: the caller brings the cloud's
lines and the till's own count of sales it has not delivered.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.money import Money

#: Figures that are counts, not money.
COUNTS = frozenset({"sales_count", "under_review_count"})


@dataclass(frozen=True, slots=True)
class CheckLine:
    figure: str
    till: int
    cloud: int

    @property
    def agrees(self) -> bool:
        return self.till == self.cloud


def _amount(line: CheckLine, value: int) -> str:
    return str(value) if line.figure in COUNTS else str(Money(value))


def explain(lines: list[CheckLine], *, waiting: int, quarantined: int) -> str:
    """One paragraph: agreement, or the gap and the likeliest reason for it.

    `waiting` and `quarantined` are this till's sales on the shift that have
    not reached the cloud — still in the outbox, or set aside in the
    failures queue. They are the usual reason the counts differ, and the
    one a supervisor can act on.
    """
    differing = [line for line in lines if not line.agrees]
    if not differing:
        return "The cloud agrees with the till to the paisa."

    by_name = {line.figure: line for line in lines}
    said: list[str] = []

    sales = by_name.get("sales_count")
    if sales is not None and not sales.agrees:
        said.append(
            f"The till closed with {sales.till} sales; the cloud has {sales.cloud}."
        )
        reasons = []
        if quarantined:
            reasons.append(f"{quarantined} in the failures queue")
        if waiting:
            reasons.append(f"{waiting} still waiting to push")
        if reasons:
            said.append("Of this shift's sales on this till: " + ", ".join(reasons) + ".")
        elif sales.till > sales.cloud:
            said.append("None is waiting on this till — the gap is in the cloud.")

    attested = by_name.get("upi_attested")
    verified = by_name.get("upi_verified")
    if (
        attested is not None
        and verified is not None
        and not verified.agrees
        and attested.till + verified.till == attested.cloud + verified.cloud
    ):
        moved = verified.cloud - verified.till
        said.append(
            f"{Money(moved)} of UPI was verified after the close; takings are unchanged."
        )
        differing = [d for d in differing if d.figure not in ("upi_attested", "upi_verified")]

    rest = [d for d in differing if d.figure != "sales_count"]
    if rest and sales is not None and sales.agrees:
        said.append(
            "Differs: "
            + "; ".join(
                f"{d.figure.replace('_', ' ')} till {_amount(d, d.till)}, "
                f"cloud {_amount(d, d.cloud)}"
                for d in rest
            )
            + "."
        )
    return " ".join(said)

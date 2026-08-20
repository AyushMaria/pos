"""Cash rounding at the tender boundary — architecture §13.4.

The shop does not stock coins below ₹1, so a ₹123.40 total is collected as
₹123 in cash. But over UPI it is collected as ₹123.40 exactly, because a
transfer has no coins. **Rounding is therefore a property of the tender, not
of the basket**, and it cannot be applied to a line total or to the GST base.

Three consequences the rest of the system depends on:

  * The adjustment is applied *after* the customer chooses how to pay.
  * On a split tender only the cash portion rounds, and only for the amount
    actually being handed over in cash.
  * The adjustment is its own recorded figure — `sales.rounding_adjustment` —
    shown as its own line on the receipt and reported separately at shift
    close. Folded into the total instead, it becomes a permanent unexplainable
    variance that a manager will eventually read as cashier theft.

The GST base is never touched. Tax was settled at the line, on the discounted
amount; rounding happens downstream of it and changes nothing about what is
owed to the tax authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from app.domain.money import RUPEE, Money

TenderMethod = Literal["cash", "upi"]

#: The largest adjustment nearest-rupee rounding can ever produce. Asserted as
#: a property test, and quoted in the plan's phase 2 exit criteria.
MAX_ADJUSTMENT = Money(RUPEE // 2)


class TenderError(ValueError):
    """A tender amount was invalid."""


@dataclass(frozen=True, slots=True)
class CashRounding:
    """What the customer hands over, and the adjustment that made it round."""

    #: The exact amount still owed before rounding.
    outstanding: Money
    #: The amount actually collected in cash.
    collected: Money
    #: collected - outstanding. Negative when the shop rounds in the
    #: customer's favour, which is the common case.
    adjustment: Money

    def __post_init__(self) -> None:
        if self.outstanding + self.adjustment != self.collected:
            raise TenderError("rounding does not reconcile")


def round_to_rupee(amount: Money) -> Money:
    """Nearest whole rupee, HALF_UP.

    ₹123.50 becomes ₹124, not ₹123: the same rule as everywhere else in the
    system, so nobody has to remember a second one.
    """
    rupees = Decimal(amount.paise) / RUPEE
    whole = rupees.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return Money(int(whole) * RUPEE)


def round_cash_tender(outstanding: Money) -> CashRounding:
    """Round an amount about to be collected in cash.

    Called with the balance outstanding *at the moment cash is chosen*, which
    on a split tender is what remains after the UPI portion has been approved.
    """
    if outstanding.is_negative:
        raise TenderError(
            f"cannot round a negative amount to collect ({outstanding}); "
            "change due is computed from the cash actually handed over"
        )

    collected = round_to_rupee(outstanding)
    return CashRounding(
        outstanding=outstanding,
        collected=collected,
        adjustment=collected - outstanding,
    )


def no_rounding(outstanding: Money) -> CashRounding:
    """The non-cash case: collected exactly, adjustment zero.

    Exists so that every tender goes through the same shape and a UPI sale is
    provably carrying a zero adjustment rather than simply never having been
    asked.
    """
    return CashRounding(
        outstanding=outstanding, collected=outstanding, adjustment=Money.zero()
    )


def rounding_for(outstanding: Money, method: TenderMethod) -> CashRounding:
    """Dispatch on tender method. The single entry point the register uses."""
    if method == "cash":
        return round_cash_tender(outstanding)
    return no_rounding(outstanding)


def change_due(collected: Money, owed: Money) -> Money:
    """Change owed when the customer hands over more than the rounded total.

    Only cash can overpay — a UPI attempt is always for the exact outstanding
    balance because the amount is embedded in the QR (architecture §13.2).
    """
    if collected < owed:
        raise TenderError(
            f"{collected} does not cover {owed}; the balance loop is not finished"
        )
    return collected - owed

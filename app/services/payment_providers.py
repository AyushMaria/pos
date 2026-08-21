"""Payment providers — architecture §13.1.

One protocol, and cash is simply the provider that resolves immediately.
Modelling cash as synchronous and everything else as asynchronous is the
mistake that forces a register rewrite when the second tender method arrives.
Adding UPI proved the point: it was a class and one line of registry, and the
register's state machine did not change (architecture §17, last row).

Providers are async because the interesting ones are. Cash never awaits
anything, which costs nothing and keeps one call shape for the register.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.identity import utcnow
from app.domain.ids import new_id
from app.domain.money import Money
from app.domain.payments import (
    AttemptState,
    PaymentAttempt,
    PaymentError,
)
from app.domain.tender import TenderMethod


@runtime_checkable
class PaymentProvider(Protocol):
    """What every tender method must be able to do."""

    method: TenderMethod

    async def begin(self, attempt: PaymentAttempt) -> PaymentAttempt: ...

    async def poll(self, attempt: PaymentAttempt) -> PaymentAttempt: ...

    async def cancel(self, attempt: PaymentAttempt) -> PaymentAttempt: ...


class CashProvider:
    """Cash. Approves the moment the drawer takes it.

    `tendered` is what the customer actually handed over, which may exceed the
    amount owed. Change is computed from it by the balance loop, never here.
    """

    method: TenderMethod = "cash"

    async def begin(self, attempt: PaymentAttempt) -> PaymentAttempt:
        if attempt.method != "cash":
            raise PaymentError(f"CashProvider cannot handle {attempt.method}")
        return attempt.to(AttemptState.APPROVED, at=utcnow())

    async def poll(self, attempt: PaymentAttempt) -> PaymentAttempt:
        # Nothing to poll: cash was resolved the instant it was begun.
        return attempt

    async def cancel(self, attempt: PaymentAttempt) -> PaymentAttempt:
        if attempt.state is AttemptState.APPROVED:
            # Money already in the drawer is not cancelled, it is refunded —
            # and a refund is a new, audited transaction (architecture §1.4).
            raise PaymentError(
                "cash already taken cannot be cancelled; refund the sale instead"
            )
        return attempt.to(AttemptState.CANCELLED, at=utcnow(), reason="cancelled")


class UpiProvider:
    """UPI against the shop's printed counter QR — architecture §13.3.

    The till generates nothing. The QR is already standing on the counter, so
    `begin` opens a PENDING attempt and stops: what happens next is a customer
    scanning a piece of paper, which no code here can observe.

    Resolution is therefore manual. The cashier hears the soundbox and attests
    (`payment.attest`), or admits they cannot tell, which is `UNKNOWN` and
    posts the sale for review rather than guessing. There is nothing to poll —
    no PSP, no webhook, no device — and `poll` says so by returning the
    attempt untouched rather than pretending to check.

    All three of those become real when a payment terminal arrives: it reports
    what it settled, and this class is replaced rather than modified (§13.6).
    """

    method: TenderMethod = "upi"

    async def begin(self, attempt: PaymentAttempt) -> PaymentAttempt:
        if attempt.method != "upi":
            raise PaymentError(f"UpiProvider cannot handle {attempt.method}")
        if attempt.expires_at is None:
            raise PaymentError(
                "a UPI attempt must expire, or an abandoned payment wedges the cart"
            )
        return attempt

    async def poll(self, attempt: PaymentAttempt) -> PaymentAttempt:
        # Nothing to ask. The only signal is a human hearing a soundbox, and
        # that arrives through attest() instead.
        return attempt

    async def cancel(self, attempt: PaymentAttempt) -> PaymentAttempt:
        if attempt.state is AttemptState.APPROVED:
            raise PaymentError(
                "an attested UPI payment cannot be cancelled; refund the sale instead"
            )
        return attempt.to(AttemptState.CANCELLED, at=utcnow(), reason="cancelled")


class ProviderRegistry:
    """Method name to provider. Phase 4 adds one line here."""

    def __init__(self, *providers: PaymentProvider) -> None:
        self._providers: dict[str, PaymentProvider] = {
            provider.method: provider for provider in providers
        }

    def for_method(self, method: TenderMethod) -> PaymentProvider:
        provider = self._providers.get(method)
        if provider is None:
            raise PaymentError(f"no provider for tender method {method!r}")
        return provider

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


def new_attempt(
    method: TenderMethod,
    amount: Money,
    *,
    expires_at: datetime | None = None,
    tendered: Money | None = None,
    started_at: datetime | None = None,
) -> PaymentAttempt:
    """A fresh PENDING attempt, with a client-generated UUID v7 id.

    The id is generated here, once, rather than by the server — it is what a
    dropped ACK is deduplicated against on sync. It is *not* what appears on a
    bank statement: the counter QR is printed, so nothing carries this id out
    to the payment (architecture §13.3). The UTR captured at attestation is
    the only reference the statement and this attempt share.
    """
    return PaymentAttempt(
        id=new_id(),
        method=method,
        amount=amount,
        state=AttemptState.PENDING,
        started_at=started_at or utcnow(),
        expires_at=expires_at,
        tendered=tendered,
    )


def default_registry() -> ProviderRegistry:
    """Cash and UPI. A card terminal is one more line here (§13.6)."""
    return ProviderRegistry(CashProvider(), UpiProvider())

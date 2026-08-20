"""Payment providers — architecture §13.1.

One protocol, and cash is simply the provider that resolves immediately.
Modelling cash as synchronous and everything else as asynchronous is the
mistake that forces a register rewrite when the second tender method arrives;
here, adding `UpiQrProvider` in phase 4 is a registry entry and a tender
button (architecture §17, last row).

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
    amount owed — only cash can overpay, because a UPI attempt is always for
    the exact balance (architecture §13.2). Change is computed from it by the
    balance loop, never here.
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

    The id is the `txn_ref` a UPI QR will carry in phase 4, which is what makes
    later settlement reconciliation possible (architecture §13.3) — so it is
    generated here, once, rather than by the server.
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
    """Phase 3 ships cash only. Phase 4 adds UpiQrProvider alongside it."""
    return ProviderRegistry(CashProvider())

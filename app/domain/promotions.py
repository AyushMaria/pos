"""Promotions — architecture §8 scope fence.

v1 is deliberately small: a percentage or a fixed amount off, scoped to a
product or a category, inside a date window. Buy-one-get-one, threshold
offers, basket-level deals and coupon codes are all out (architecture §8) and
should stay out until after the pilot.

The reason for the fence is not effort, it is explicability. A cashier has to
be able to answer "why is this ₹18 off?" while a queue waits. Every promotion
here reduces to one sentence.

Promotions become `Discount`s, which the pricing engine then stacks in a
defined order — automatic promotions first, then any manual reduction a
supervisor authorises on top (see `pricing._ordered`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.domain.pricing import Discount, DiscountKind, PricingError


class PromotionError(ValueError):
    """A promotion was configured in a way that cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class Promotion:
    """One offer, as the catalogue holds it.

    `priority` orders application: lower runs first. Ties break on id so that
    the same basket always produces the same receipt, whatever order the rows
    came back from the database in.
    """

    id: str
    kind: DiscountKind
    value: int
    priority: int = 0
    product_ids: frozenset[str] = frozenset()
    category_ids: frozenset[str] = frozenset()
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise PromotionError(f"promotion value must be an int, got {self.value!r}")
        if self.value < 0:
            raise PromotionError("a negative promotion is a price increase")

        # A fixed-amount promotion with no scope would take ₹10 off *every*
        # line in every basket. That is a cash leak wearing a discount's
        # clothes, so it is refused at construction rather than at the till.
        if self.kind == "amount" and not (self.product_ids or self.category_ids):
            raise PromotionError(
                f"promotion {self.id}: a fixed-amount promotion must name the "
                "products or categories it applies to"
            )

        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise PromotionError(f"promotion {self.id} ends before it starts")

    @property
    def is_unscoped(self) -> bool:
        """True when this applies to every product — only legal for percentages."""
        return not (self.product_ids or self.category_ids)

    def active_at(self, when: datetime) -> bool:
        if self.starts_at and when < self.starts_at:
            return False
        return not (self.ends_at and when >= self.ends_at)

    def covers(self, product_id: str, category_id: str | None) -> bool:
        if self.is_unscoped:
            return True
        if product_id in self.product_ids:
            return True
        return category_id is not None and category_id in self.category_ids

    def to_discount(self) -> Discount:
        try:
            return Discount(
                kind=self.kind,
                value=self.value,
                reason=self.description or f"Promotion {self.id}",
                source="promotion",
                promotion_id=self.id,
            )
        except PricingError as exc:
            raise PromotionError(f"promotion {self.id}: {exc}") from exc


def applicable(
    promotions: Iterable[Promotion],
    *,
    product_id: str,
    category_id: str | None,
    when: datetime,
) -> list[Promotion]:
    """Every promotion that applies to this product at this moment, in order."""
    matched = [
        promotion
        for promotion in promotions
        if promotion.active_at(when) and promotion.covers(product_id, category_id)
    ]
    return sorted(matched, key=lambda promotion: (promotion.priority, promotion.id))


def discounts_for(
    promotions: Iterable[Promotion],
    *,
    product_id: str,
    category_id: str | None,
    when: datetime,
) -> tuple[Discount, ...]:
    """The discounts to attach to a line, ready for the pricing engine."""
    return tuple(
        promotion.to_discount()
        for promotion in applicable(
            promotions, product_id=product_id, category_id=category_id, when=when
        )
    )

"""Promotions — architecture §8 scope fence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.money import Money
from app.domain.pricing import LineInput, price_line
from app.domain.promotions import Promotion, PromotionError, applicable, discounts_for
from app.domain.tax import TaxCode

GST18 = TaxCode("GST18", "GST 18%", 1800)
NOW = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)


def ten_percent_off_atta() -> Promotion:
    return Promotion(
        id="p-atta",
        kind="percentage",
        value=1000,
        product_ids=frozenset({"atta"}),
        description="10% off atta",
    )


# ── Scope ───────────────────────────────────────────────────────────────────


def test_a_product_scoped_promotion() -> None:
    promo = ten_percent_off_atta()

    assert promo.covers("atta", None)
    assert not promo.covers("milk", None)


def test_a_category_scoped_promotion() -> None:
    promo = Promotion(
        id="p-staples", kind="percentage", value=500,
        category_ids=frozenset({"staples"}),
    )

    assert promo.covers("anything", "staples")
    assert not promo.covers("anything", "toiletries")
    assert not promo.covers("anything", None)


def test_an_unscoped_percentage_covers_everything() -> None:
    promo = Promotion(id="storewide", kind="percentage", value=500)

    assert promo.is_unscoped
    assert promo.covers("anything", None)


def test_an_unscoped_fixed_amount_is_refused() -> None:
    """₹10 off *every line in every basket* is a cash leak, not a promotion."""
    with pytest.raises(PromotionError, match="must name the products"):
        Promotion(id="bad", kind="amount", value=1000)


def test_a_negative_promotion_is_refused() -> None:
    with pytest.raises(PromotionError, match="price increase"):
        Promotion(id="bad", kind="percentage", value=-100)


def test_a_float_value_is_refused() -> None:
    with pytest.raises(PromotionError):
        Promotion(id="bad", kind="percentage", value=10.0)  # type: ignore[arg-type]


# ── Date window ─────────────────────────────────────────────────────────────


def test_a_promotion_inside_its_window() -> None:
    promo = Promotion(
        id="p", kind="percentage", value=1000,
        starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(days=1),
    )

    assert promo.active_at(NOW)


def test_a_promotion_that_has_not_started() -> None:
    promo = Promotion(id="p", kind="percentage", value=1000, starts_at=NOW + timedelta(hours=1))

    assert not promo.active_at(NOW)


def test_a_promotion_that_has_ended() -> None:
    promo = Promotion(id="p", kind="percentage", value=1000, ends_at=NOW)

    assert not promo.active_at(NOW), "the end instant is exclusive"
    assert promo.active_at(NOW - timedelta(seconds=1))


def test_a_promotion_with_no_window_is_always_on() -> None:
    assert Promotion(id="p", kind="percentage", value=1000).active_at(NOW)


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(PromotionError, match="ends before it starts"):
        Promotion(
            id="p", kind="percentage", value=1000,
            starts_at=NOW, ends_at=NOW - timedelta(days=1),
        )


# ── Selection and ordering ──────────────────────────────────────────────────


def test_only_applicable_promotions_are_selected() -> None:
    promotions = [
        ten_percent_off_atta(),
        Promotion(id="p-milk", kind="percentage", value=500,
                  product_ids=frozenset({"milk"})),
        Promotion(id="p-expired", kind="percentage", value=9000,
                  product_ids=frozenset({"atta"}), ends_at=NOW - timedelta(days=1)),
    ]

    selected = applicable(promotions, product_id="atta", category_id=None, when=NOW)

    assert [p.id for p in selected] == ["p-atta"]


def test_promotions_apply_in_priority_order() -> None:
    promotions = [
        Promotion(id="b", kind="percentage", value=1000, priority=2),
        Promotion(id="a", kind="percentage", value=2000, priority=1),
    ]

    selected = applicable(promotions, product_id="x", category_id=None, when=NOW)

    assert [p.id for p in selected] == ["a", "b"]


def test_ties_break_deterministically() -> None:
    """The same basket must always produce the same receipt."""
    promotions = [
        Promotion(id="z", kind="percentage", value=1000),
        Promotion(id="a", kind="percentage", value=1000),
    ]

    for _ in range(5):
        selected = applicable(promotions, product_id="x", category_id=None, when=NOW)
        assert [p.id for p in selected] == ["a", "z"]


# ── Through the pricing engine ──────────────────────────────────────────────


def test_a_promotion_reaches_the_line_total() -> None:
    discounts = discounts_for(
        [ten_percent_off_atta()], product_id="atta", category_id=None, when=NOW
    )
    priced = price_line(
        LineInput(
            product_id="atta",
            description="Aashirvaad 5kg",
            unit_price=Money(27500),
            qty_milli=1000,
            tax_code=GST18,
            discounts=discounts,
        )
    )

    assert priced.discount_total == Money(2750)
    assert priced.line_total == Money(24750)
    assert priced.applied[0].discount.promotion_id == "p-atta"


def test_a_promotion_carries_its_reason_to_the_receipt() -> None:
    """A cashier has to be able to answer "why is this ₹27.50 off?"."""
    discount = ten_percent_off_atta().to_discount()

    assert discount.reason == "10% off atta"
    assert discount.source == "promotion"


def test_a_promotion_without_a_description_still_identifies_itself() -> None:
    promo = Promotion(id="p-42", kind="percentage", value=500)

    assert "p-42" in promo.to_discount().reason


def test_no_applicable_promotions_yields_no_discounts() -> None:
    assert discounts_for([], product_id="x", category_id=None, when=NOW) == ()

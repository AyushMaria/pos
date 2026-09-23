"""The day-close check's sentence — phase 8 slice 3."""

from __future__ import annotations

from app.domain.close_check import CheckLine, explain


def lines(**pairs: tuple[int, int]) -> list[CheckLine]:
    base = {
        "sales_count": (3, 3),
        "cash_sales": (11_100, 11_100),
        "upi_attested": (0, 0),
        "upi_verified": (0, 0),
        "cash_in": (0, 0),
        "cash_out": (0, 0),
        "rounding": (-120, -120),
        "under_review_count": (0, 0),
        "under_review_total": (0, 0),
    }
    base.update(pairs)
    return [CheckLine(name, till, cloud) for name, (till, cloud) in base.items()]


def test_agreement_is_one_sentence() -> None:
    assert explain(lines(), waiting=0, quarantined=0) == (
        "The cloud agrees with the till to the paisa."
    )


def test_a_quarantined_sale_is_named_as_the_reason() -> None:
    text = explain(
        lines(sales_count=(3, 2), cash_sales=(11_100, 7_400)), waiting=0, quarantined=1
    )
    assert "The till closed with 3 sales; the cloud has 2." in text
    assert "1 in the failures queue" in text


def test_a_sale_still_waiting_is_named() -> None:
    text = explain(lines(sales_count=(3, 2)), waiting=1, quarantined=0)
    assert "1 still waiting to push" in text


def test_a_gap_with_nothing_on_this_till_says_where_to_look() -> None:
    text = explain(lines(sales_count=(3, 2)), waiting=0, quarantined=0)
    assert "the gap is in the cloud" in text


def test_upi_verified_after_the_close_is_not_a_discrepancy_in_takings() -> None:
    text = explain(
        lines(upi_attested=(2_000, 0), upi_verified=(0, 2_000)), waiting=0, quarantined=0
    )
    assert "₹20.00 of UPI was verified after the close" in text
    assert "Differs" not in text


def test_any_other_difference_is_listed_with_both_figures() -> None:
    text = explain(lines(cash_out=(5_000, 0)), waiting=0, quarantined=0)
    assert "cash out till ₹50.00, cloud ₹0.00" in text

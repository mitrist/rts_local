from __future__ import annotations

from eis_winner_price_key import (
    compute_expected_pp_unit_price,
    notice_quantity_from_text,
    pp_unit_price_matches_target,
)


def test_compute_expected_pp_unit_price():
    unit, text = compute_expected_pp_unit_price(439_800, 6)
    assert text == "73300,00"
    assert abs(unit - 73300.0) < 0.001


def test_notice_quantity_from_text():
    assert notice_quantity_from_text("6") == 6.0
    assert notice_quantity_from_text("6,5") == 6.5
    assert notice_quantity_from_text("") is None


def test_pp_unit_price_matches_target_exact():
    _, target_text = compute_expected_pp_unit_price(439_800, 6)
    from participation_price import parse_money_amount

    pp_price = parse_money_amount(target_text)
    assert pp_price is not None
    assert pp_unit_price_matches_target(pp_price, 439_800 / 6)
    assert not pp_unit_price_matches_target(pp_price, 73301.0)


def test_compute_expected_pp_unit_price_zero_quantity_raises():
    unit, text = compute_expected_pp_unit_price(1000, 1)
    assert text == "1000,00"
    assert unit == 1000.0

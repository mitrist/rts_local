"""Compute EIS winner offer → expected PP unit price key."""

from __future__ import annotations

from participation_price import format_rts_unit_price_from_float, parse_money_amount, parse_quantity


def compute_expected_pp_unit_price(winner_total: float, quantity: float) -> tuple[float, str]:
    """Return (unit float, RTS-formatted string like '73300,00')."""
    unit = float(winner_total) / float(quantity)
    return unit, format_rts_unit_price_from_float(unit)


def pp_unit_price_matches_target(pp_unit_price: float | None, target: float) -> bool:
    formatted = parse_money_amount(format_rts_unit_price_from_float(target))
    if formatted is None or pp_unit_price is None:
        return False
    return abs(float(pp_unit_price) - formatted) < 0.001


def notice_quantity_from_text(quantity_text: str) -> float | None:
    return parse_quantity(quantity_text)

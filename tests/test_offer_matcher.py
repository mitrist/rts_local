from __future__ import annotations

from offer_matcher import rank_offer_matches
from rts_preoffers import RtsPreoffer, RtsPreofferCharacteristic


def _offer(*, color: str = "черный", has_width: bool = True) -> RtsPreoffer:
    chars = [
        RtsPreofferCharacteristic(match_key="name:цвет", label="Цвет", value=color),
        RtsPreofferCharacteristic(match_key="name:материал", label="Материал", value="металл"),
        RtsPreofferCharacteristic(match_key="name:лишняя", label="Лишняя", value="не мешает"),
    ]
    if has_width:
        chars.append(RtsPreofferCharacteristic(match_key="name:ширина", label="Ширина", value="100"))
    return RtsPreoffer(
        offer_id="offer",
        title="Предложение",
        rts_url="https://example.test",
        sku="SKU",
        ktru_code="KTRU",
        characteristics=chars,
    )


def _customer() -> list[dict[str, str]]:
    return [
        {"match_key": "name:цвет", "display_label": "Цвет", "value_display": "черный"},
        {"match_key": "name:материал", "display_label": "Материал", "value_display": "металл"},
        {"match_key": "name:ширина", "display_label": "Ширина", "value_display": "100"},
    ]


def test_offer_passes_when_all_customer_characteristics_match():
    result = rank_offer_matches(_customer(), [_offer()])[0]

    assert result.passes is True
    assert result.matched == 3
    assert result.required == 3


def test_extra_offer_characteristics_do_not_fail_match():
    result = rank_offer_matches(_customer()[:2], [_offer()])[0]

    assert result.passes is True
    assert result.matched == 2
    assert result.required == 2


def test_missing_customer_required_characteristic_fails():
    result = rank_offer_matches(_customer(), [_offer(has_width=False)])[0]

    assert result.passes is False
    assert result.missing == 1
    assert result.rows[-1].status == "missing"


def test_mismatch_value_fails_and_sorts_passing_offer_first():
    failing = _offer(color="белый")
    passing = _offer(color="черный")

    results = rank_offer_matches(_customer(), [failing, passing])

    assert results[0].passes is True
    assert results[1].passes is False
    assert results[1].mismatched == 1
    assert results[1].rows[0].status == "mismatch"

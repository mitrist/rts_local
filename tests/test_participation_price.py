from __future__ import annotations

from pathlib import Path

import participation_price as pp

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "price_ord_mini.csv"


def _catalog() -> pp.PriceOrdCatalog:
    return pp.load_price_ord_catalog(FIXTURE)


def test_parse_money_and_quantity():
    assert pp.parse_money_amount("2 200,00") == 2200.0
    assert pp.parse_quantity("1 053,00") == 1053.0
    assert pp.position_sum_amount(price_per_unit_text="2 200,00", quantity_text="1 053,00") == 2200.0 * 1053.0


def test_subject_candidates_from_oktmo():
    assert "34000000" in pp.subject_candidates_from_oktmo("34701000001")
    assert "1000000" in pp.subject_candidates_from_oktmo("01000000000")


def test_profitability_condition_thresholds():
    assert pp.profitability_condition(50.0) == 1
    assert pp.profitability_condition(49.99) == 2
    assert pp.profitability_condition(30.0) == 2
    assert pp.profitability_condition(29.99) == 3


def test_compute_gravel_altai():
    econ = pp.compute_participation_economics(
        ktru_code="08.12.12.140-00000005",
        delivery_place="01000000000 \u041c\u0443\u043d\u0438\u0446\u0438\u043f\u0430\u043b\u044c\u043d\u044b\u0435 \u043e\u0431\u0440\u0430\u0437\u043e\u0432\u0430\u043d\u0438\u044f \u0410\u043b\u0442\u0430\u0439\u0441\u043a\u043e\u0433\u043e \u043a\u0440\u0430\u044f",
        region="",
        price_per_unit_text="2 200,00",
        quantity_text="1 053,00",
        catalog=_catalog(),
    )
    assert econ["available"] is True
    assert econ["condition"] == 3
    assert econ["profitability_class"] == "offer-profit--red"
    assert econ["cpk"] == econ["cp"]
    assert float(econ["profitability_pct"]) < 30.0
    assert econ["unit_price_for_draft"]


def test_compute_with_oktmo_delivery():
    econ = pp.compute_participation_economics(
        ktru_code="26.20.16.120-00000104",
        delivery_place="34701000001 Муниципальные образования Костромской области",
        price_per_unit_text="10 000,00",
        quantity_text="10",
        catalog=_catalog(),
    )
    assert econ["available"] is True
    assert econ["condition"] in (1, 2, 3)


def test_compute_unavailable_without_row():
    econ = pp.compute_participation_economics(
        ktru_code="UNKNOWN-KTRU",
        delivery_place="34701000001 Муниципальные образования Костромской области",
        price_per_unit_text="1 000,00",
        quantity_text="1",
        catalog=_catalog(),
    )
    assert econ["available"] is False
    assert econ["error_reason"] == "no_price_ord_row"
    assert econ["unavailable_message"] == pp.NO_PRICE_ORD_ROW_MESSAGE
    assert econ["profitability_text"] == pp.NO_PRICE_ORD_ROW_MESSAGE


def test_cpk_green_applies_second_discount():
    cp = 100_000.0
    zc = 10_000.0
    nr = 1_000.0
    or_amount = cp - zc - nr
    rent = or_amount / cp * 100
    assert rent >= 50
    condition = pp.profitability_condition(rent)
    cpk = cp * 0.97 if condition == 1 else cp
    assert condition == 1
    assert cpk == 97_000.0

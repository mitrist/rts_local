from __future__ import annotations

from eis_oktmo_subjects import subject_oktmo_for_org_region
from ktru_notice_enrich import (
    enrich_row,
    extract_delivery,
    extract_inn10,
    extract_oktmo,
    extract_quantity,
    inn10_checksum_ok,
    summarize,
)


def test_inn10_checksum_sber():
    assert inn10_checksum_ok("7707083893")
    assert not inn10_checksum_ok("7707123456")


def test_extract_inn_labeled():
    inn, valid, src = extract_inn10("ООО Тест\nИНН 7707083893\nАдрес поставки: г Тверь")
    assert inn == "7707083893"
    assert valid is True
    assert src == "customer_text:ИНН"


def test_extract_inn_spz_not_inn():
    inn, valid, src = extract_inn10("ООО Тест\nКод заказчика по СПЗ: 03492000123")
    assert inn is None
    assert valid is None
    assert src == "no_inn_in_source"


def test_extract_inn_invalid_labeled():
    inn, valid, src = extract_inn10("ИНН 7707123456")
    assert inn is None
    assert valid is False
    assert src == "inn_checksum_failed"


def test_quantity_calc_and_suspect():
    qty, src, suspect, _ = extract_quantity(
        price_per_unit_text="100,00",
        line_sum_text="5300,00",
        characteristics_json="[]",
        notice_total_text="1 000 000",
    )
    assert qty == 53
    assert src == "calc"
    assert suspect is False


def test_quantity_suspect_over_5pct():
    qty, src, suspect, _ = extract_quantity(
        price_per_unit_text="100,00",
        line_sum_text="108,00",
        characteristics_json="[]",
        notice_total_text="",
    )
    assert qty == 1
    assert src == "calc"
    assert suspect is True


def test_quantity_missing():
    qty, src, suspect, reason = extract_quantity(
        price_per_unit_text="",
        line_sum_text="",
        characteristics_json="[]",
        notice_total_text="500000",
    )
    assert qty is None
    assert src == "null"
    assert suspect is None
    assert reason == "no_source"


def test_delivery_days_and_date():
    date, days, src, _ = extract_delivery("поставка в течение 30 календарных дней")
    assert date is None
    assert days == 30
    assert src == "days_pattern"
    iso, days2, src2, _ = extract_delivery("исполнить до 15.03.2026 по адресу")
    assert iso == "2026-03-15"
    assert days2 is None
    assert src2 == "date_pattern"
    empty = extract_delivery("Адрес поставки: г. Тверь, ул. Примерная, д. 1")
    assert empty[0] is None and empty[1] is None
    assert empty[2] == "null"


def test_oktmo_place_then_region():
    code, prec, _ = extract_oktmo(
        "Адрес поставки: 34701000001 Муниципальные образования Костромской области",
        "44",
    )
    assert code == "34701000001"
    assert prec == "place"
    code2, prec2, _ = extract_oktmo("Адрес поставки: г Тверь", "69")
    assert code2 == "28000000"
    assert prec2 == "region"
    code3, prec3, reason = extract_oktmo("", "99")
    assert code3 is None
    assert prec3 is None
    assert reason == "no_official_oktmo_for_region"


def test_subject_oktmo_moscow_krasnodar():
    assert subject_oktmo_for_org_region("77") == "45000000"
    assert subject_oktmo_for_org_region("23") == "03000000"
    assert subject_oktmo_for_org_region("63") == "36000000"


def test_enrich_row_idempotent():
    row = {
        "purchase_number": "0111200000126000001",
        "line_key": "x",
        "price_per_unit_text": "1000,00",
        "line_sum_text": "5000,00",
        "notice_total_text": "5000,00",
        "customer_text": "ГБУ\nИНН 7707083893\nАдрес поставки: г Москва",
        "characteristics_json": "[]",
        "org_region": "77",
    }
    a = enrich_row(row)
    b = enrich_row(row)
    assert a == b
    assert a["quantity"] == 5
    assert a["inn"] == "7707083893"
    assert a["oktmo"] == "45000000"
    assert a["oktmo_precision"] == "region"
    assert a["delivery_source"] == "null"
    stats = summarize([a])
    assert stats["total"] == 1
    assert stats["quantity_filled"] == 1
    assert stats["inn_filled"] == 1

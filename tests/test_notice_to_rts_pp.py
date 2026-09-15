from __future__ import annotations

import json
from pathlib import Path

import pytest

from notice_to_rts_pp import (
    ApplyResult,
    build_notice_create_payload,
    build_notice_ktru_characteristics,
    compare_notice_to_draft,
    compare_notice_to_template,
    find_template_characteristic,
    group_notice_characteristics,
    parse_notice_value,
    split_value_display,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "notice_rts"


@pytest.fixture()
def magistral_notice_chars() -> list[dict]:
    return json.loads((_FIXTURES / "magistral_notice_chars.json").read_text(encoding="utf-8"))


@pytest.fixture()
def magistral_ktru_template() -> dict:
    return json.loads((_FIXTURES / "magistral_ktru_template.json").read_text(encoding="utf-8"))


@pytest.fixture()
def mfp_ktru_template() -> dict:
    return json.loads((_FIXTURES / "mfp_ktru_template.json").read_text(encoding="utf-8"))


def test_parse_notice_value_range():
    parsed = parse_notice_value("не менее 105 и не более 150 см")
    assert parsed["minValue"] == "105"
    assert parsed["maxValue"] == "150"
    assert parsed["minMathNotation"] == "ge"
    assert parsed["maxMathNotation"] == "le"


def test_parse_notice_value_quality():
    parsed = parse_notice_value("Прямая")
    assert parsed["qualityDescription"] == "Прямая"


def test_parse_notice_value_contrast_ratio():
    parsed = parse_notice_value("≥4000:1")
    assert parsed["qualityDescription"] == "≥4000:1"
    assert parsed["minValue"] is None
    assert parsed["maxValue"] is None


def test_parse_notice_value_strict_less_than():
    parsed = parse_notice_value("< 400 Миллиметр")
    assert parsed["maxValue"] == "400"
    assert parsed["maxMathNotation"] == "lt"
    assert parsed["qualityDescription"] is None


def test_parse_notice_value_ge_and_strict_lt():
    parsed = parse_notice_value("≥ 40 и < 50 Миллиметр")
    assert parsed["minValue"] == "40"
    assert parsed["minMathNotation"] == "ge"
    assert parsed["maxValue"] == "50"
    assert parsed["maxMathNotation"] == "lt"


def test_find_template_by_code(magistral_notice_chars, magistral_ktru_template):
    ch = find_template_characteristic(magistral_ktru_template, magistral_notice_chars[0])
    assert ch is not None
    assert ch["code"] == "f0d0406b-f"
    assert ch["name"] == "Длина трубки"


def test_build_notice_ktru_characteristics_magistral(magistral_notice_chars, magistral_ktru_template):
    chars, result = build_notice_ktru_characteristics(magistral_ktru_template, magistral_notice_chars)
    assert result.matched == 2
    assert result.unmatched_notice == []
    codes = {c["code"] for c in chars}
    assert "f0d0406b-f" in codes
    assert "004271bf-5" in codes

    length = next(c for c in chars if c["code"] == "f0d0406b-f")
    vals = length["characteristicValues"]
    assert vals
    assert vals[0]["minValue"] == "105"
    assert vals[0]["maxValue"] == "150"
    assert vals[0]["id"] == 5342065

    variant = next(c for c in chars if c["code"] == "004271bf-5")
    assert variant["characteristicValues"][0]["qualityDescription"] == "Прямая"
    assert variant["characteristicValues"][0]["id"] == 5342071
    assert result.value_match_exact >= 1


def test_build_notice_create_payload(magistral_notice_chars, magistral_ktru_template):
    payload, result = build_notice_create_payload(
        magistral_ktru_template,
        magistral_notice_chars,
        unit_from_line="шт",
    )
    assert payload["unit"]["name"] == "шт"
    assert payload["ktru"]["code"] == "32.50.13.110-00005820"
    assert len(payload["ktruCharacteristics"]) == len(magistral_ktru_template["characteristics"])
    length = next(c for c in payload["ktruCharacteristics"] if c["code"] == "f0d0406b-f")
    assert length["characteristicValues"][0]["minValue"] == "105"
    assert result.matched == 2


def test_compare_notice_to_template(magistral_notice_chars, magistral_ktru_template):
    rows = compare_notice_to_template(magistral_notice_chars, magistral_ktru_template)
    assert len(rows) == 2
    assert all(r["char_match"] != "missing" for r in rows)


def test_unmatched_notice_reported(magistral_ktru_template):
    notice = [{"display_label": "Неизвестная", "value_display": "X", "match_key": "name:неизвестная"}]
    _, result = build_notice_ktru_characteristics(magistral_ktru_template, notice)
    assert result.matched == 0
    assert len(result.unmatched_notice) == 1
    assert result.as_dict()["unmatched_notice"][0]["label"] == "Неизвестная"


def test_apply_result_fill_summary():
    r = ApplyResult(matched=2, unmatched_notice=[{"label": "a", "value": "b", "code": ""}])
    assert r.fill_summary == "2/3"


def test_monitor_refresh_rate_matches_75hz_preset():
    from rts_ktru_cache import load_cached_template

    tpl = load_cached_template(Path(__file__).resolve().parents[1] / "ktru_rts_cache", "26.20.17.110-00000007", ttl_sec=0)
    assert tpl is not None
    notice_char = {
        "display_label": "Частота обновления экрана",
        "match_key": "name:частота обновления экрана",
        "characteristic_code": "1623785d-f",
        "value_display": ">= 75.00000000000 and Герц",
    }
    chars, result = build_notice_ktru_characteristics(tpl, [notice_char])
    ch = next(c for c in chars if c.get("name") == "Частота обновления экрана")
    val = ch["characteristicValues"][0]
    assert val["id"] == 5368990
    assert _parse_rts_num_local(val.get("minValue")) == 75.0
    assert result.value_match_exact >= 1


def test_parse_notice_value_xml_greater_or_equal():
    parsed = parse_notice_value("ОКЕИ Герц greaterOrEqual мин 75.00000000000 [тип 2] вид 2")
    assert parsed["minValue"] == "75,00000000000"
    assert parsed["minMathNotation"] == "ge"


def _parse_rts_num_local(value: object) -> float | None:
    from rts_pp_api import _parse_rts_num

    return _parse_rts_num(value)


def test_split_value_display_pipe():
    assert split_value_display("Windows | Linux | MacOS") == ["Windows", "Linux", "MacOS"]


def test_split_value_display_keeps_numeric():
    raw = "≥ 75.00000000000 и Герц"
    assert split_value_display(raw) == [raw]


def test_group_split_pipe_values():
    notice = [
        {
            "display_label": "Совместимость",
            "match_key": "name:совместимость",
            "value_display": "Windows | Linux | MacOS",
        }
    ]
    groups = group_notice_characteristics(notice)
    assert len(groups) == 1
    assert groups[0].values == ["Windows", "Linux", "MacOS"]


def test_multi_select_compatibility(mfp_ktru_template):
    notice = [
        {
            "display_label": "Совместимость",
            "match_key": "name:совместимость",
            "characteristic_code": "bd89cf9f-6",
            "value_display": "Windows | Linux | MacOS",
        }
    ]
    chars, result = build_notice_ktru_characteristics(mfp_ktru_template, notice)
    ch = next(c for c in chars if c["code"] == "bd89cf9f-6")
    ids = {row["id"] for row in ch["characteristicValues"]}
    assert ids == {5365560, 5365562, 5365563}
    assert result.matched == 1


def test_multi_select_scan_type_rows(mfp_ktru_template):
    notice = [
        {"display_label": "Тип сканирования", "match_key": "name:тип сканирования", "value_display": "Планшетный"},
        {"display_label": "", "match_key": "name:", "value_display": "Протяжный"},
    ]
    chars, _ = build_notice_ktru_characteristics(mfp_ktru_template, notice)
    ch = next(c for c in chars if c["code"] == "d44b3b40-f")
    ids = {row["id"] for row in ch["characteristicValues"]}
    assert ids == {5365521, 5365522}


def test_multi_select_connection(mfp_ktru_template):
    notice = [
        {"display_label": "Способ подключения", "match_key": "name:способ подключения", "value_display": "USB"},
        {"display_label": "", "match_key": "name:", "value_display": "Ethernet (RJ-45)"},
    ]
    chars, _ = build_notice_ktru_characteristics(mfp_ktru_template, notice)
    ch = next(c for c in chars if c["code"] == "01a79ac9-6")
    ids = {row["id"] for row in ch["characteristicValues"]}
    assert ids == {5365492, 5365491}


def test_single_select_stays_one(mfp_ktru_template):
    notice = [
        {
            "display_label": "Цветность печати",
            "match_key": "name:цветность печати",
            "characteristic_code": "21804b9d-8",
            "value_display": "Цветная | Монохромная",
        }
    ]
    chars, _ = build_notice_ktru_characteristics(mfp_ktru_template, notice)
    ch = next(c for c in chars if c["code"] == "21804b9d-8")
    assert len(ch["characteristicValues"]) == 1
    assert ch["characteristicValues"][0]["id"] == 5365356


def _draft_from_built(chars: list[dict]) -> dict:
    return {"ktruCharacteristics": chars}


def test_compare_notice_to_draft_multi_select_ok(mfp_ktru_template):
    notice = [
        {
            "display_label": "Совместимость",
            "match_key": "name:совместимость",
            "characteristic_code": "bd89cf9f-6",
            "value_display": "Windows | Linux | MacOS",
        }
    ]
    built, _ = build_notice_ktru_characteristics(mfp_ktru_template, notice)
    compat = next(c for c in built if c["code"] == "bd89cf9f-6")
    report = compare_notice_to_draft(notice, _draft_from_built([compat]), ktru_item=mfp_ktru_template)
    assert report["issues"] == 0
    assert report["ok"] == 1
    assert report["rows"][0]["status"] == "ok"


def test_compare_notice_to_draft_count_mismatch(mfp_ktru_template):
    notice = [
        {
            "display_label": "Совместимость",
            "match_key": "name:совместимость",
            "characteristic_code": "bd89cf9f-6",
            "value_display": "Windows | Linux | MacOS",
        }
    ]
    built, _ = build_notice_ktru_characteristics(mfp_ktru_template, notice)
    ch = next(c for c in built if c["code"] == "bd89cf9f-6")
    ch["characteristicValues"] = ch["characteristicValues"][:1]
    report = compare_notice_to_draft(notice, _draft_from_built(built), ktru_item=mfp_ktru_template)
    row = report["rows"][0]
    assert row["status"] == "count_mismatch"
    assert "ожидалось 3" in row["detail"]


def test_compare_notice_to_draft_numeric_value_mismatch():
    from rts_ktru_cache import load_cached_template

    tpl = load_cached_template(
        Path(__file__).resolve().parents[1] / "ktru_rts_cache",
        "26.20.17.110-00000007",
        ttl_sec=0,
    )
    assert tpl is not None
    notice = [
        {
            "display_label": "Частота обновления экрана",
            "match_key": "name:частота обновления экрана",
            "characteristic_code": "1623785d-f",
            "value_display": ">= 75.00000000000 and Герц",
        }
    ]
    built, _ = build_notice_ktru_characteristics(tpl, notice)
    ch = next(c for c in built if c.get("name") == "Частота обновления экрана")
    wrong = dict(ch["characteristicValues"][0])
    wrong["id"] = 5368989
    ch["characteristicValues"] = [wrong]
    report = compare_notice_to_draft(notice, _draft_from_built(built), ktru_item=tpl)
    assert report["rows"][0]["status"] == "value_mismatch"


def test_compare_notice_to_draft_magistral_ok(magistral_notice_chars, magistral_ktru_template):
    built, _ = build_notice_ktru_characteristics(magistral_ktru_template, magistral_notice_chars)
    matched = [c for c in built if c.get("characteristicValues")]
    report = compare_notice_to_draft(
        magistral_notice_chars,
        _draft_from_built(matched),
        ktru_item=magistral_ktru_template,
    )
    assert report["issues"] == 0
    assert report["ok"] == 2

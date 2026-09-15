from __future__ import annotations

import json
from pathlib import Path

import pytest

from eis_pp_link import compare_notice_to_find

FIND_FIXTURE = Path(__file__).resolve().parents[1] / ".cursor" / "supplier" / "find.json"


@pytest.fixture
def find_item() -> dict:
    if not FIND_FIXTURE.is_file():
        pytest.skip("find.json fixture missing")
    return json.loads(FIND_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def ktru_template() -> dict:
    return {
        "code": "26.20.13.000-00000002",
        "characteristics": [
            {
                "id": 1001,
                "code": "d82d240a-8",
                "name": "Условия эксплуатации",
                "characteristicValues": [{"id": 9001, "qualityDescription": "В помещении"}],
            },
            {
                "id": 1002,
                "code": "56c89d80-0",
                "name": "Диагональ матрицы дисплея",
                "characteristicValues": [
                    {
                        "id": 9002,
                        "minValue": "75,0000",
                        "maxValue": "80,0000",
                        "minMathNotation": "ge",
                        "maxMathNotation": "lt",
                    }
                ],
            },
        ],
    }


def test_compare_notice_to_find_full_match_subset(find_item: dict, ktru_template: dict):
    notice_chars = [
        {
            "characteristic_code": "d82d240a-8",
            "display_label": "Условия эксплуатации",
            "value_display": "В помещении",
        },
        {
            "characteristic_code": "56c89d80-0",
            "display_label": "Диагональ матрицы дисплея",
            "value_display": "не менее 75 и не более 80",
        },
    ]
    subset_find = {
        "ktruCharacteristics": (find_item.get("ktruCharacteristics") or [])[:2],
    }
    result = compare_notice_to_find(notice_chars, subset_find, ktru_item=ktru_template)
    assert result.total_compared == 2
    assert result.matched_count == 2
    assert result.notice_only_count == 0
    assert result.pp_only_count == 0


def test_compare_notice_to_find_one_mismatch(find_item: dict, ktru_template: dict):
    notice_chars = [
        {
            "characteristic_code": "d82d240a-8",
            "display_label": "Условия эксплуатации",
            "value_display": "В помещении",
        },
        {
            "characteristic_code": "only-in-notice",
            "display_label": "Дополнительное требование",
            "value_display": "Да",
        },
    ]
    subset_find = {
        "ktruCharacteristics": [(find_item.get("ktruCharacteristics") or [])[0]],
    }
    result = compare_notice_to_find(notice_chars, subset_find, ktru_item=ktru_template)
    assert result.total_compared == 2
    assert result.matched_count == 1
    assert result.notice_only_count == 1


def test_compare_notice_to_find_notice_only(find_item: dict, ktru_template: dict):
    notice_chars = [
        {
            "characteristic_code": "d82d240a-8",
            "display_label": "Условия эксплуатации",
            "value_display": "В помещении",
        },
        {
            "display_label": "Только в извещении",
            "match_key": "name:только в извещении",
            "value_display": "X",
        },
    ]
    subset_find = {
        "ktruCharacteristics": [(find_item.get("ktruCharacteristics") or [])[0]],
    }
    result = compare_notice_to_find(notice_chars, subset_find, ktru_item=ktru_template)
    assert result.notice_only_count >= 1
    assert result.total_compared >= 2


def test_compare_notice_to_find_pp_only(find_item: dict, ktru_template: dict):
    notice_chars = [
        {
            "characteristic_code": "d82d240a-8",
            "display_label": "Условия эксплуатации",
            "value_display": "В помещении",
        },
    ]
    subset_find = {
        "ktruCharacteristics": (find_item.get("ktruCharacteristics") or [])[:2],
    }
    result = compare_notice_to_find(notice_chars, subset_find, ktru_item=ktru_template)
    assert result.pp_only_count >= 1
    assert result.matched_count >= 1


def test_compare_notice_to_find_as_dict(find_item: dict, ktru_template: dict):
    result = compare_notice_to_find([], find_item, ktru_item=ktru_template)
    payload = result.as_dict()
    assert "matched_count" in payload
    assert "total_compared" in payload
    assert "match_ratio" not in payload


def test_is_exact_match_notice_driven_allows_pp_only(find_item: dict, ktru_template: dict):
    from eis_pp_link import is_exact_match_notice_driven

    notice_chars = [
        {
            "characteristic_code": "d82d240a-8",
            "display_label": "Условия эксплуатации",
            "value_display": "В помещении",
        },
    ]
    subset_find = {
        "ktruCharacteristics": (find_item.get("ktruCharacteristics") or [])[:2],
    }
    result = compare_notice_to_find(notice_chars, subset_find, ktru_item=ktru_template)
    assert is_exact_match_notice_driven(result)


def test_is_exact_match_notice_driven_rejects_notice_only(find_item: dict, ktru_template: dict):
    from eis_pp_link import is_exact_match_notice_driven

    notice_chars = [
        {
            "characteristic_code": "d82d240a-8",
            "display_label": "Условия эксплуатации",
            "value_display": "В помещении",
        },
        {"display_label": "Extra", "match_key": "name:extra", "value_display": "X"},
    ]
    subset_find = {
        "ktruCharacteristics": [(find_item.get("ktruCharacteristics") or [])[0]],
    }
    result = compare_notice_to_find(notice_chars, subset_find, ktru_item=ktru_template)
    assert not is_exact_match_notice_driven(result)


def test_compare_notice_to_find_display_label_alias():
    from eis_pp_link import is_exact_match_notice_driven

    notice_chars = [
        {
            "display_label": "Наличие дисплея",
            "match_key": "name:наличие дисплея",
            "value_display": "Да",
        },
    ]
    ktru_template = {
        "code": "26.20.18.000-00000069",
        "characteristics": [
            {
                "id": 1,
                "code": "ba8259b8-b",
                "name": "Наличие ЖК-дисплея",
                "characteristicValues": [{"id": 1, "qualityDescription": "Да"}],
            }
        ],
    }
    find_item = {
        "ktruCharacteristics": [
            {
                "code": "ba8259b8-b",
                "name": "Наличие ЖК-дисплея",
                "characteristicValues": [{"qualityDescription": "Да"}],
            }
        ],
    }
    result = compare_notice_to_find(notice_chars, find_item, ktru_item=ktru_template)
    assert result.notice_only_count == 0
    assert result.pp_only_count == 0
    assert result.matched_count == 1
    assert is_exact_match_notice_driven(result)


def test_compare_notice_to_find_contrast_ratio_value():
    from eis_pp_link import is_exact_match_notice_driven

    notice_chars = [
        {
            "display_label": "Статическая контрастность дисплея",
            "match_key": "name:статическая контрастность дисплея",
            "value_display": "≥4000:1",
        },
    ]
    ktru_template = {
        "code": "26.20.13.000-00000002",
        "characteristics": [
            {
                "id": 851724,
                "code": "402af74c-5",
                "name": "Статическая контрастность дисплея",
                "characteristicValues": [{"id": 5190883, "qualityDescription": "≥4000:1"}],
            }
        ],
    }
    find_item = {
        "ktruCharacteristics": [
            {
                "code": "402af74c-5",
                "name": "Статическая контрастность дисплея",
                "characteristicValues": [{"id": 5190883, "qualityDescription": "≥4000:1"}],
            }
        ],
    }
    result = compare_notice_to_find(notice_chars, find_item, ktru_item=ktru_template)
    contrast = next(r for r in result.rows if "контраст" in r["label"].lower())
    assert contrast["status"] == "ok"
    assert is_exact_match_notice_driven(result)


def test_compare_notice_to_find_strict_less_than_dimensions():
    from eis_pp_link import is_exact_match_notice_driven

    notice_chars = [
        {
            "display_label": "Высота корпуса",
            "match_key": "name:высота корпуса",
            "value_display": "< 400 Миллиметр",
        },
        {
            "display_label": "Длина корпуса",
            "match_key": "name:длина корпуса",
            "value_display": "< 400 Миллиметр",
        },
    ]
    ktru_template = {
        "code": "26.20.15.000-00000028",
        "characteristics": [
            {
                "id": 895260,
                "code": "65bb2997-1",
                "name": "Высота корпуса",
                "characteristicValues": [
                    {"id": 5394451, "maxValue": "400,0000", "maxMathNotation": "lt"},
                ],
            },
            {
                "id": 895262,
                "code": "5d3cf27c-d",
                "name": "Длина корпуса",
                "characteristicValues": [
                    {"id": 5394467, "maxValue": "400,0000", "maxMathNotation": "lt"},
                ],
            },
        ],
    }
    find_item = {
        "ktruCharacteristics": [
            {
                "code": "65bb2997-1",
                "name": "Высота корпуса",
                "characteristicValues": [
                    {"id": 5394451, "maxValue": "400,0000", "maxMathNotation": "lt"},
                ],
            },
            {
                "code": "5d3cf27c-d",
                "name": "Длина корпуса",
                "characteristicValues": [
                    {"id": 5394467, "maxValue": "400,0000", "maxMathNotation": "lt"},
                ],
            },
        ],
    }
    result = compare_notice_to_find(notice_chars, find_item, ktru_item=ktru_template)
    assert result.matched_count == 2
    assert all(r["status"] == "ok" for r in result.rows)
    assert is_exact_match_notice_driven(result)


def test_compute_lag_minutes_and_window():
    from eis_pp_link import compute_lag_minutes, lag_within_window

    lag = compute_lag_minutes("2026-07-01T12:00:00Z", "2026-07-01T12:30:00Z")
    assert lag == pytest.approx(30.0)
    assert lag_within_window(lag, 60)
    assert not lag_within_window(lag, 20)
    lag_before = compute_lag_minutes("2026-07-01T12:00:00Z", "2026-07-01T11:30:00Z")
    assert lag_before == pytest.approx(-30.0)
    assert lag_within_window(lag_before, 60)

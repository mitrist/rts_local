from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from monitor_find_to_draft import (
    apply_find_to_draft_item,
    build_find_create_payload,
    build_find_rts_values,
    compare_find_to_draft,
    competitor_draft_unit_price,
    competitor_unit_price_amount,
    delivery_params_from_find,
    find_template_char,
)
from notice_to_rts_pp import ApplyResult

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
        "name": "Интерактивная панель",
        "version": 16,
        "characteristics": [
            {
                "id": 1001,
                "code": "d82d240a-8",
                "name": "Условия эксплуатации",
                "isRequired": True,
                "characteristicValues": [{"id": 9001, "qualityDescription": "В помещении"}],
            },
            {
                "id": 1002,
                "code": "56c89d80-0",
                "name": "Диагональ матрицы дисплея",
                "isRequired": True,
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


def test_delivery_params_from_find(find_item: dict):
    params = delivery_params_from_find(find_item)
    assert params["competitor_unit_price"]
    assert params["quantity_text"]
    assert params["region"]


def test_find_template_char_by_code(ktru_template: dict, find_item: dict):
    find_char = find_item["ktruCharacteristics"][0]
    template = find_template_char(ktru_template, find_char)
    assert template is not None
    assert template["code"] == "d82d240a-8"


def test_build_find_rts_values_uses_template_ids(ktru_template: dict, find_item: dict):
    find_char = find_item["ktruCharacteristics"][0]
    template = find_template_char(ktru_template, find_char)
    assert template is not None
    result = ApplyResult()
    values = build_find_rts_values(ktru_template, template, find_char, result)
    assert values
    assert values[0]["characteristicId"] == 1001
    assert values[0]["id"] == 9001
    assert values[0]["characteristicId"] != find_char["id"]
    assert values[0].get("id") != find_char["characteristicValues"][0]["id"]


def test_build_find_create_payload_overlays_values(ktru_template: dict, find_item: dict):
    payload, result = build_find_create_payload(ktru_template, find_item)
    assert result.matched >= 1
    chars = payload.get("ktruCharacteristics") or []
    by_code = {str(c.get("code")): c for c in chars if isinstance(c, dict)}
    indoor = by_code.get("d82d240a-8")
    assert indoor and indoor.get("characteristicValues")
    vals = indoor["characteristicValues"]
    assert vals[0]["characteristicId"] == 1001
    assert vals[0]["id"] == 9001


def test_apply_find_to_draft_item_no_registry_ids(find_item: dict, ktru_template: dict):
    draft = {
        "ktruCharacteristics": [
            {
                "id": 1001,
                "code": "d82d240a-8",
                "name": "Условия эксплуатации",
                "isRequired": True,
                "characteristicValues": [],
            }
        ],
        "ktru": {"code": "26.20.13.000-00000002", "name": "x", "version": 16},
    }
    result = apply_find_to_draft_item(draft, find_item, ktru_template)
    assert result.matched >= 1
    filled = draft["ktruCharacteristics"][0]["characteristicValues"]
    assert filled
    assert filled[0]["characteristicId"] == 1001
    assert filled[0]["id"] not in (851714, 5190771)


def test_apply_find_unmatched_template(ktru_template: dict):
    draft = {"ktruCharacteristics": [], "ktru": {"code": "x", "version": 1}}
    find_item = {
        "ktruCharacteristics": [
            {
                "id": 99999,
                "code": "unknown-code-xyz",
                "name": "Неизвестная",
                "characteristicValues": [{"qualityDescription": "X"}],
            }
        ]
    }
    result = apply_find_to_draft_item(draft, find_item, ktru_template)
    assert result.unmatched_find
    assert not any(ch.get("code") == "unknown-code-xyz" for ch in draft["ktruCharacteristics"])


def test_compare_find_to_draft(find_item: dict, ktru_template: dict):
    draft = {
        "ktruCharacteristics": find_item.get("ktruCharacteristics") or [],
        "ktru": find_item.get("ktru") or {},
    }
    report = compare_find_to_draft(find_item, draft, ktru_item=ktru_template)
    assert report["total"] >= 1
    assert report["ok"] >= 1


def test_competitor_draft_unit_price_from_find():
    find_item = {
        "deliveryPlaces": [
            {
                "unitPrice": "100000,00",
                "oktmo": {"code": "45000000", "name": "Москва"},
            }
        ],
    }
    assert competitor_unit_price_amount(find_item) == 100000.0
    assert competitor_draft_unit_price(find_item) == "95000,00"


def test_competitor_draft_unit_price_fallback():
    assert competitor_draft_unit_price({}, fallback_unit_price=200000.0) == "190000,00"


def test_competitor_draft_unit_price_missing():
    assert competitor_draft_unit_price({}) is None
    assert competitor_draft_unit_price({}, fallback_unit_price=None) is None


def test_apply_find_delivery_sets_discounted_price(find_item: dict):
    from monitor_find_to_draft import apply_find_delivery

    item: dict = {"deliveryPlaces": [{}]}
    price = competitor_draft_unit_price(find_item)
    assert price
    apply_find_delivery(item, find_item, price_per_unit=price)
    assert item["deliveryPlaces"][0]["unitPrice"] == price

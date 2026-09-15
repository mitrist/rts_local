from __future__ import annotations

import json

from pp_stack import (
    EMPTY_HASH,
    find_characteristics_fingerprint,
    mark_our_pp,
    prices_match,
    stack_has_our_pp,
    stack_key,
)


def _base_find(**overrides: object) -> dict:
    item = {
        "ktruCharacteristics": [
            {
                "code": "d82d240a-8",
                "name": "Условия эксплуатации",
                "characteristicValues": [{"qualityDescription": "В помещении"}],
            },
            {
                "code": "56c89d80-0",
                "name": "Диагональ",
                "characteristicValues": [{"concreteValue": "65"}],
            },
        ],
        "deliveryPlaces": [
            {"oktmo": {"code": "45000000"}, "unitPrice": "100000,00"},
        ],
    }
    item.update(overrides)
    return item


def test_fingerprint_same_chars_different_delivery():
    a = _base_find()
    b = _base_find(
        deliveryPlaces=[
            {"oktmo": {"code": "40000000"}, "unitPrice": "200000,00"},
            {"oktmo": {"code": "77000000"}, "unitPrice": "150000,00"},
        ]
    )
    ha, _ = find_characteristics_fingerprint(a)
    hb, _ = find_characteristics_fingerprint(b)
    assert ha == hb
    assert ha != EMPTY_HASH


def test_fingerprint_different_values():
    a = _base_find()
    b = _base_find(
        ktruCharacteristics=[
            {
                "code": "d82d240a-8",
                "characteristicValues": [{"qualityDescription": "На улице"}],
            },
            {
                "code": "56c89d80-0",
                "characteristicValues": [{"concreteValue": "65"}],
            },
        ]
    )
    ha, _ = find_characteristics_fingerprint(a)
    hb, _ = find_characteristics_fingerprint(b)
    assert ha != hb


def test_fingerprint_empty_chars():
    digest, _ = find_characteristics_fingerprint({"ktruCharacteristics": []})
    assert digest == EMPTY_HASH


def test_stack_key_format():
    assert stack_key("26.20.13.000-00000002", "abc123") == "26.20.13.000-00000002:abc123"


def test_mark_our_pp_competitor_and_ours():
    members = [
        {"offer_price_list_item_id": "c1", "unit_price": 100000.0},
        {"offer_price_list_item_id": "o1", "unit_price": 95000.0},
    ]
    marked = mark_our_pp(members)
    by_id = {m["offer_price_list_item_id"]: m for m in marked}
    assert by_id["c1"]["is_our_pp"] is False
    assert by_id["o1"]["is_our_pp"] is True


def test_prices_match_rts_format():
    assert prices_match(95000.0, 100000.0 * 0.95)


def test_stack_has_our_pp():
    members = [
        {"offer_price_list_item_id": "c1", "unit_price": 100000.0},
        {"offer_price_list_item_id": "o1", "unit_price": 95000.0},
    ]
    assert stack_has_our_pp(members) is True
    assert stack_has_our_pp([{"offer_price_list_item_id": "x", "unit_price": 100000.0}]) is False


def test_fingerprint_stable_json_roundtrip():
    raw = _base_find()
    h1, c1 = find_characteristics_fingerprint(raw)
    h2, c2 = find_characteristics_fingerprint(json.loads(json.dumps(raw)))
    assert h1 == h2
    assert c1 == c2

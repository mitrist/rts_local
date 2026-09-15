from __future__ import annotations

import json
from pathlib import Path

import pytest

from rts_customer_pp_api import build_compare_payload, parse_compare_response
from rts_pp_monitor_settings import (
    load_settings,
    parse_form_payload,
    parse_ktru_codes,
    passes_monitor_filters,
    poll_interval_sec_from_settings,
    save_settings,
    settings_path,
)


def test_parse_ktru_codes_validates_format():
    assert parse_ktru_codes(["26.20.13.000-00000002"]) == ["26.20.13.000-00000002"]
    with pytest.raises(ValueError, match="Некорректный код КТРУ"):
        parse_ktru_codes(["bad-code"])


def test_settings_roundtrip(tmp_path: Path):
    path = settings_path(tmp_path)
    payload = parse_form_payload(
        {
            "enabled": "on",
            "poll_interval_min": "25",
            "ktru_codes_text": "26.20.13.000-00000002",
            "price_min": "100000",
            "price_max": "500000",
            "skip_expired": "on",
            "notify_telegram": "on",
            "notify_max": "on",
        }
    )
    save_settings(path, payload)
    loaded = load_settings(path)
    assert loaded["enabled"] is True
    assert loaded["poll_interval_min"] == 25
    assert loaded["ktru_codes"] == ["26.20.13.000-00000002"]
    assert loaded["filters"]["price_min"] == 100000.0
    assert loaded["filters"]["price_max"] == 500000.0


def test_settings_preserves_poll_meta(tmp_path: Path):
    path = settings_path(tmp_path)
    payload = parse_form_payload(
        {
            "enabled": "on",
            "poll_interval_min": "20",
            "ktru_codes_text": "26.20.13.000-00000002",
        }
    )
    payload["last_new_poll_started_at_utc"] = "2026-07-07T10:00:00Z"
    payload["last_new_poll_finished_at_utc"] = "2026-07-07T10:05:00Z"
    payload["last_new_poll_items_new"] = 3
    save_settings(path, payload)
    loaded = load_settings(path)
    assert loaded["last_new_poll_started_at_utc"] == "2026-07-07T10:00:00Z"
    assert loaded["last_new_poll_finished_at_utc"] == "2026-07-07T10:05:00Z"
    assert loaded["last_new_poll_items_new"] == 3


def test_settings_poll_interval_min_bounds(tmp_path: Path):
    path = settings_path(tmp_path)
    save_settings(
        path,
        parse_form_payload(
            {
                "enabled": "on",
                "poll_interval_min": "3",
                "ktru_codes_text": "26.20.13.000-00000002",
            }
        ),
    )
    assert load_settings(path)["poll_interval_min"] == 5
    save_settings(
        path,
        parse_form_payload(
            {
                "enabled": "on",
                "poll_interval_min": "7",
                "ktru_codes_text": "26.20.13.000-00000002",
            }
        ),
    )
    assert load_settings(path)["poll_interval_min"] == 7


def test_poll_interval_sec_from_settings_min_five():
    assert poll_interval_sec_from_settings({"poll_interval_min": 5}) == 300
    assert poll_interval_sec_from_settings({"poll_interval_min": 4}) is None


def test_passes_monitor_filters_price():
    settings = {
        "filters": {"price_min": 100000, "price_max": 200000, "skip_expired": False, "region_ids": []},
    }
    assert passes_monitor_filters({"unit_price": 150000}, settings)
    assert not passes_monitor_filters({"unit_price": 50000}, settings)


def test_build_compare_payload_from_template():
    template = {
        "code": "26.20.13.000-00000002",
        "name": "Интерактивная панель",
        "version": 16,
        "okeis": [{"fullName": "Штука"}],
        "characteristics": [
            {
                "id": 851714,
                "isRequired": True,
                "characteristicValues": [{"id": 5190771}],
            }
        ],
    }
    payload = build_compare_payload(template)
    assert payload["ktruCode"] == "26.20.13.000-00000002"
    assert payload["ktruVersion"] == 16
    assert payload["ktruCharacteristicRequirements"] == [{"dictionaryId": 851714, "dictionaryValuesIds": [5190771]}]


def test_parse_compare_response_extracts_ids():
    raw = json.loads(Path(".cursor/supplier/1.json").read_text(encoding="utf-8"))
    items = parse_compare_response(raw, ktru_code="26.20.13.000-00000002")
    assert len(items) == 20
    assert items[0].offer_price_list_item_id == "RyvkyKuzMWAq"
    assert items[0].ktru_code == "26.20.13.000-00000002"
    assert items[0].card_url.endswith("/RyvkyKuzMWAq")

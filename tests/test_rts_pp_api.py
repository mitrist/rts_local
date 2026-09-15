from __future__ import annotations

from pathlib import Path

import pytest

from rts_pp_api import (
    RtsPpApiError,
    apply_notice_delivery,
    build_create_payload,
    build_delivery_place_from_notice,
    create_and_save_pp_draft,
    draft_edit_url,
    format_rts_quantity,
    format_rts_unit_price,
    load_offers_cookie,
    parse_oktmo_from_delivery_place,
    resolve_ktru_for_create,
    search_ktru,
)


def test_build_create_payload_maps_ktru_item():
    ktru_item = {
        "code": "31.09.12.110-00000004",
        "name": "Диван",
        "version": 5,
        "okpd2": {"code": "31.09.12.110", "name": "Диваны"},
        "okeis": [{"code": "796", "fullName": "Штука"}],
        "characteristics": [
            {
                "id": 1,
                "code": "572ee567-0",
                "name": "Наличие механизма",
                "isRequired": True,
                "isActual": True,
                "characteristicKind": {"name": "Неизменяемая заказчиком"},
                "characteristicValues": [{"id": 99, "qualityDescription": "Нет"}],
            }
        ],
    }
    payload = build_create_payload(ktru_item, unit_from_line="шт")
    assert payload["ktru"]["code"] == "31.09.12.110-00000004"
    assert payload["unit"]["name"] == "шт"
    assert len(payload["ktruCharacteristics"]) == 1
    assert payload["ktruCharacteristics"][0]["characteristicValues"]


def test_draft_edit_url():
    assert "WpSv" in draft_edit_url("WpSvO8VcSrxV")
    assert draft_edit_url("", draft_id=495778).endswith("/supplier/price-lists/positions")


def test_parse_oktmo_from_delivery_place():
    oktmo = parse_oktmo_from_delivery_place(
        "34701000001 Муниципальные образования Костромской области"
    )
    assert oktmo == {
        "code": "34701000001",
        "name": "Муниципальные образования Костромской области",
    }


def test_format_rts_unit_price():
    assert format_rts_unit_price("2 200,00") == "2200,00"
    assert format_rts_unit_price("10 500,00") == "10500,00"


def test_format_rts_quantity():
    assert format_rts_quantity("1 053,00") == "1053,00"
    assert format_rts_quantity("10") == "10,0"


def test_build_delivery_place_from_notice():
    place = build_delivery_place_from_notice(
        delivery_place="34701000001 Муниципальные образования Костромской области",
        price_per_unit="15 432,50",
        delivery_days="30",
        quantity_text="1 053,00",
    )
    assert place["oktmo"]["code"] == "34701000001"
    assert place["unitPrice"] == "15432,50"
    assert place["vatRateCode"] == "R22"
    assert place["minDeliveryDays"] == 30
    assert place["maxDeliveryDays"] == 30
    assert place["minQuantity"] == "1053,00"
    assert place["maxQuantity"] == "1053,00"
    assert place["available"] == "1053,00"
    assert place["blocked"] == "0,0"


def test_apply_notice_delivery_overrides_stub():
    item: dict = {"deliveryPlaces": []}
    apply_notice_delivery(
        item,
        delivery_place="34701000001 Муниципальные образования Костромской области",
        price_per_unit="2 200,00",
        quantity_text="1 053,00",
    )
    dp = item["deliveryPlaces"][0]
    assert dp["oktmo"]["code"] == "34701000001"
    assert dp["unitPrice"] == "2200,00"
    assert dp["vatRateCode"] == "R22"
    assert dp["oktmo"]["code"] != "04500000"
    assert dp["minQuantity"] == "1053,00"
    assert dp["maxQuantity"] == "1053,00"


def test_apply_draft_defaults_replaces_empty_lists():
    from rts_pp_api import apply_draft_defaults

    item = {
        "countriesOfOrigin": [],
        "nationalModeInfoGroups": [],
        "softwareNationalModeInfoGroups": [],
    }
    apply_draft_defaults(item)
    assert item["countriesOfOrigin"] == [{"code": "643", "name": "Российская Федерация"}]
    assert item["nationalModeInfoGroups"][0]["nationalModeInfoTypeId"] == "IndustrialProductsRussianFederation"
    assert item["softwareNationalModeInfoGroups"][0]["nationalModeInfoTypeId"] == "SoftwareRussianFederation"


def test_apply_draft_defaults_with_china_country():
    from ktru_country_origin import CountryOfOrigin
    from rts_pp_api import apply_draft_defaults

    item = {
        "countriesOfOrigin": [],
        "nationalModeInfoGroups": [],
        "softwareNationalModeInfoGroups": [],
    }
    apply_draft_defaults(item, country=CountryOfOrigin("156", "Китайская Народная Республика"))
    assert item["countriesOfOrigin"] == [{"code": "156", "name": "Китайская Народная Республика"}]
    assert item["nationalModeInfoGroups"] == []
    assert item["softwareNationalModeInfoGroups"] == []


def test_load_offers_cookie_missing(tmp_path):
    with pytest.raises(RtsPpApiError) as exc:
        load_offers_cookie(tmp_path / "missing.txt")
    assert exc.value.reason == "no_cookie"


def test_resolve_ktru_for_create_stub_only_with_allow_stub(monkeypatch):
    def fake_request(method, url, **kwargs):
        class Resp:
            status_code = 200

            def json(self):
                return {"page": [], "total": 1}

        return Resp()

    monkeypatch.setattr("rts_pp_api.requests.request", fake_request)
    with pytest.raises(RtsPpApiError) as exc:
        resolve_ktru_for_create(
            "cookie=value",
            "32.50.13.110-00005820",
            ktru_name="Тест",
        )
    assert exc.value.reason == "ktru_catalog_unavailable"

    item = resolve_ktru_for_create(
        "cookie=value",
        "32.50.13.110-00005820",
        ktru_name="Тест",
        allow_stub=True,
    )
    assert item["code"] == "32.50.13.110-00005820"
    assert item["name"] == "Тест"


def test_resolve_ktru_for_create_uses_cache(tmp_path, monkeypatch):
    from rts_ktru_cache import save_cached_template

    save_cached_template(
        tmp_path,
        "32.50.13.110-00005820",
        {"code": "32.50.13.110-00005820", "name": "Из кэша", "version": 1, "characteristics": []},
    )

    def fail_request(*args, **kwargs):
        raise AssertionError("requests must not be called when cache hits")

    monkeypatch.setattr("rts_pp_api.requests.request", fail_request)
    item = resolve_ktru_for_create(
        "cookie=value",
        "32.50.13.110-00005820",
        cache_dir=tmp_path,
    )
    assert item["name"] == "Из кэша"


def test_search_ktru_exact_match(monkeypatch):
    def fake_request(method, url, **kwargs):
        class Resp:
            status_code = 200

            def json(self):
                return {
                    "page": [
                        {"code": "08.12.12.140-00000005", "name": "Щебень"},
                        {"code": "99.99.99.999-00000001", "name": "Other"},
                    ]
                }

        return Resp()

    monkeypatch.setattr("rts_pp_api.requests.request", fake_request)
    item = search_ktru("cookie=value", "08.12.12.140-00000005")
    assert item["name"] == "Щебень"


def test_fix_characteristic_value_ids_resolves_null_id():
    from rts_pp_api import _fix_characteristic_value_ids

    ktru_item = {
        "characteristics": [
            {
                "id": 1,
                "code": "572ee567-0",
                "name": "Test",
                "characteristicValues": [{"id": 9, "qualityDescription": "Да"}],
            }
        ]
    }
    item = {
        "ktruCharacteristics": [
            {
                "id": 1,
                "code": "572ee567-0",
                "name": "Test",
                "characteristicValues": [{"qualityDescription": "Да"}],
            }
        ]
    }
    _fix_characteristic_value_ids(item, ktru_item)
    vals = item["ktruCharacteristics"][0]["characteristicValues"]
    assert vals[0]["id"] == 9


def test_create_and_save_pp_draft_flow(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        class Resp:
            status_code = 200

            def json(self):
                if url.endswith("/draft/create"):
                    return {"id": 12345}
                if url.endswith("/12345") and method == "GET":
                    return {
                        "id": 12345,
                        "protectedId": "ABC123",
                        "ktruCharacteristics": [
                            {
                                "id": 1,
                                "code": "572ee567-0",
                                "name": "Test",
                                "isRequired": True,
                                "characteristicValues": [],
                            }
                        ],
                        "deliveryPlaces": [],
                    }
                if "Ktru/search" in url:
                    return {
                        "page": [
                            {
                                "code": "08.12.12.140-00000005",
                                "name": "Щебень",
                                "version": 1,
                                "okeis": [{"code": "796", "fullName": "Штука"}],
                                "characteristics": [
                                    {
                                        "id": 1,
                                        "code": "572ee567-0",
                                        "name": "Test",
                                        "isRequired": True,
                                        "characteristicValues": [{"id": 9, "qualityDescription": "Да"}],
                                    }
                                ],
                            }
                        ]
                    }
                if "draft/search" in url:
                    return {"page": [{"protectedId": "ABC123"}]}
                if "draft/update" in url:
                    return {}
                return {}

        return Resp()

    monkeypatch.setattr("rts_pp_api.requests.request", fake_request)
    result = create_and_save_pp_draft("cookie=value", ktru_code="08.12.12.140-00000005")
    assert result["rts_draft_id"] == 12345
    assert result["protected_id"] == "ABC123"
    assert "ABC123" in result["edit_url"]
    assert any(m == "POST" and "draft/update" in u for m, u in calls)


def test_create_and_save_pp_draft_with_notice_chars(monkeypatch, tmp_path):
    notice_chars = [
        {
            "characteristic_code": "572ee567-0",
            "display_label": "Test",
            "value_display": "Да",
            "match_key": "572ee567-0",
        }
    ]
    update_bodies: list[dict] = []

    def fake_request(method, url, **kwargs):
        class Resp:
            status_code = 200

            def json(self):
                if url.endswith("/draft/create"):
                    return {"id": 99}
                if url.endswith("/99") and method == "GET":
                    return {
                        "id": 99,
                        "protectedId": "PID99",
                        "ktruCharacteristics": [
                            {
                                "id": 1,
                                "code": "572ee567-0",
                                "name": "Test",
                                "isRequired": True,
                                "characteristicValues": [],
                            }
                        ],
                        "deliveryPlaces": [],
                    }
                if "draft/update" in url:
                    update_bodies.append(kwargs.get("json") or {})
                    return {}
                if "draft/search" in url:
                    return {"page": []}
                return {}

        return Resp()

    monkeypatch.setattr("rts_pp_api.requests.request", fake_request)
    from rts_ktru_cache import save_cached_template

    save_cached_template(
        tmp_path,
        "08.12.12.140-00000005",
        {
            "code": "08.12.12.140-00000005",
            "name": "Щебень",
            "version": 1,
            "okeis": [{"code": "796", "fullName": "Штука"}],
            "characteristics": [
                {
                    "id": 1,
                    "code": "572ee567-0",
                    "name": "Test",
                    "isRequired": True,
                    "characteristicValues": [{"id": 9, "qualityDescription": "Да"}],
                }
            ],
        },
    )

    result = create_and_save_pp_draft(
        "cookie=value",
        ktru_code="08.12.12.140-00000005",
        notice_characteristics=notice_chars,
        ktru_cache_dir=tmp_path,
    )
    assert result["rts_draft_id"] == 99
    assert result.get("fill_report", {}).get("matched", 0) >= 1
    assert update_bodies
    updated_chars = update_bodies[0].get("ktruCharacteristics") or []
    filled = next(c for c in updated_chars if c.get("code") == "572ee567-0")
    assert filled.get("characteristicValues")
    assert filled["characteristicValues"][0]["id"] == 9

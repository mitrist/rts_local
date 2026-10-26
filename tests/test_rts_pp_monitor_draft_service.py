from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from eis_analytics_db import connect, init_db, save_pp_monitor_draft
from rts_pp_api import RtsPpApiError
from rts_pp_draft_service import create_pp_draft_from_monitor


def _finding(oid: str = "oid1") -> dict:
    return {
        "offer_price_list_item_id": oid,
        "ktru_code": "26.20.13.000-00000002",
        "unit_price": 100000.0,
        "find": {
            "ktru": {"code": "26.20.13.000-00000002", "name": "Панель", "version": 16},
            "unit": {"code": "796", "name": "Штука"},
            "ktruCharacteristics": [
                {
                    "code": "d82d240a-8",
                    "name": "Условия эксплуатации",
                    "characteristicValues": [{"qualityDescription": "В помещении"}],
                }
            ],
            "deliveryPlaces": [{"unitPrice": "100000,00", "oktmo": {"code": "45000000", "name": "Москва"}}],
        },
    }


def _ktru_item() -> dict:
    return {
        "code": "26.20.13.000-00000002",
        "name": "Панель",
        "version": 16,
        "okeis": [{"code": "796", "fullName": "Штука"}],
        "characteristics": [
            {
                "id": 1,
                "code": "d82d240a-8",
                "name": "Условия эксплуатации",
                "isRequired": True,
                "characteristicValues": [{"id": 9, "qualityDescription": "В помещении"}],
            }
        ],
    }


def _draft_item() -> dict:
    return {
        "id": 555,
        "protectedId": "PID555",
        "ktruCharacteristics": [
            {
                "id": 1,
                "code": "d82d240a-8",
                "name": "Условия эксплуатации",
                "isRequired": True,
                "characteristicValues": [],
            }
        ],
        "deliveryPlaces": [],
        "ktru": {"code": "26.20.13.000-00000002", "name": "Панель", "version": 16},
    }


@pytest.fixture
def env(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    preoffers.write_text(
        json.dumps(
            [
                {
                    "enabled": True,
                    "ktru_code": "26.20.13.000-00000002",
                    "title": "Preoffer TM",
                    "offer_id": "off1",
                }
            ]
        ),
        encoding="utf-8",
    )
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("session=1", encoding="utf-8")
    ktru_cache = meta / "ktru_rts_cache"
    ktru_cache.mkdir(parents=True)
    (ktru_cache / "26.20.13.000-00000002.json").write_text(json.dumps(_ktru_item()), encoding="utf-8")
    return {
        "db": db,
        "meta": meta,
        "preoffers": preoffers,
        "cookie": cookie,
    }


def test_monitor_draft_update_failure_saves_draft_id(monkeypatch, env: dict):
    create_calls = {"n": 0}

    def fake_create(_cookie, _payload):
        create_calls["n"] += 1
        return 555

    def fake_update(_cookie, _item):
        raise RtsPpApiError("РТС API 400: characteristic 822769", reason="api_error")

    monkeypatch.setattr("rts_pp_draft_service._ensure_monitor_finding", lambda *_a, **_k: _finding())
    monkeypatch.setattr("rts_pp_draft_service.load_offers_cookie", lambda _p: "c=1")
    monkeypatch.setattr("rts_pp_draft_service.validate_cookie_session", lambda _c: None)
    monkeypatch.setattr("rts_pp_draft_service.resolve_ktru_for_create", lambda *_a, **_k: _ktru_item())
    monkeypatch.setattr("rts_pp_draft_service.create_draft", fake_create)
    monkeypatch.setattr("rts_pp_draft_service.get_draft", lambda _c, _i: _draft_item())
    monkeypatch.setattr("rts_pp_draft_service.update_draft", fake_update)
    monkeypatch.setattr("rts_pp_draft_service.fetch_national_mode_info_types", lambda *_a, **_k: {})
    monkeypatch.setattr("rts_pp_draft_service.pp_participate_eligibility", lambda *_a, **_k: {"can_participate": True})
    monkeypatch.setattr(
        "rts_pp_draft_service.resolve_country_for_draft",
        lambda **_k: type("C", (), {"code": "156", "name": "Китай"})(),
    )

    result = create_pp_draft_from_monitor(
        analytics_db=env["db"],
        meta_dir=env["meta"],
        preoffers_path=env["preoffers"],
        cookie_path=env["cookie"],
        offer_price_list_item_id="oid1",
        rts_profile="IPSokolova",
    )
    assert result.ok is False
    assert result.rts_draft_id == 555
    assert create_calls["n"] == 1

    from eis_analytics_db import get_pp_monitor_draft

    row = get_pp_monitor_draft(env["db"], "oid1", rts_profile="IPSokolova")
    assert row is not None
    assert row["status"] == "error"
    assert row["rts_draft_id"] == 555


def test_monitor_draft_retry_skips_create(monkeypatch, env: dict):
    save_pp_monitor_draft(
        env["db"],
        offer_price_list_item_id="oid1",
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=555,
        status="error",
        error_text="prev fail",
        rts_profile="IPSokolova",
    )
    create_calls = {"n": 0}

    def fake_create(_cookie, _payload):
        create_calls["n"] += 1
        return 999

    monkeypatch.setattr("rts_pp_draft_service._ensure_monitor_finding", lambda *_a, **_k: _finding())
    monkeypatch.setattr("rts_pp_draft_service.load_offers_cookie", lambda _p: "c=1")
    monkeypatch.setattr("rts_pp_draft_service.validate_cookie_session", lambda _c: None)
    monkeypatch.setattr("rts_pp_draft_service.resolve_ktru_for_create", lambda *_a, **_k: _ktru_item())
    monkeypatch.setattr("rts_pp_draft_service.create_draft", fake_create)
    monkeypatch.setattr("rts_pp_draft_service.get_draft", lambda _c, _i: _draft_item())
    monkeypatch.setattr("rts_pp_draft_service.update_draft", MagicMock())
    monkeypatch.setattr("rts_pp_draft_service.draft_search_by_id", lambda *_a, **_k: {"protectedId": "PID555"})
    monkeypatch.setattr("rts_pp_draft_service.fetch_national_mode_info_types", lambda *_a, **_k: {})
    monkeypatch.setattr("rts_pp_draft_service.pp_participate_eligibility", lambda *_a, **_k: {"can_participate": True})
    monkeypatch.setattr(
        "rts_pp_draft_service.resolve_country_for_draft",
        lambda **_k: type("C", (), {"code": "156", "name": "Китай"})(),
    )

    result = create_pp_draft_from_monitor(
        analytics_db=env["db"],
        meta_dir=env["meta"],
        preoffers_path=env["preoffers"],
        cookie_path=env["cookie"],
        offer_price_list_item_id="oid1",
        rts_profile="IPSokolova",
    )
    assert result.ok is True
    assert create_calls["n"] == 0
    assert result.rts_draft_id == 555


def test_monitor_draft_duplicate_create(monkeypatch, env: dict):
    monkeypatch.setattr("rts_pp_draft_service._ensure_monitor_finding", lambda *_a, **_k: _finding())
    monkeypatch.setattr("rts_pp_draft_service.load_offers_cookie", lambda _p: "c=1")
    monkeypatch.setattr("rts_pp_draft_service.validate_cookie_session", lambda _c: None)
    monkeypatch.setattr("rts_pp_draft_service.resolve_ktru_for_create", lambda *_a, **_k: _ktru_item())
    monkeypatch.setattr(
        "rts_pp_draft_service.create_draft",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RtsPpApiError('РТС API 400: {"message": "Позиция ... уже существует"}', reason="api_error")
        ),
    )
    monkeypatch.setattr("rts_pp_draft_service.pp_participate_eligibility", lambda *_a, **_k: {"can_participate": True})

    result = create_pp_draft_from_monitor(
        analytics_db=env["db"],
        meta_dir=env["meta"],
        preoffers_path=env["preoffers"],
        cookie_path=env["cookie"],
        offer_price_list_item_id="oid1",
        rts_profile="IPSokolova",
    )
    assert result.ok is False
    assert result.reason == "duplicate_draft"
    assert result.http_status == 409


def test_monitor_draft_copies_country_from_find_china(monkeypatch, env: dict):
    update_bodies: list[dict] = []

    def finding_china(*_a, **_k):
        f = _finding()
        f["find"] = {
            **f["find"],
            "countriesOfOrigin": [
                {
                    "code": "156",
                    "name": "Китайская Народная Республика",
                    "description": None,
                    "version": None,
                    "actual": None,
                    "isTemplate": None,
                    "category": "Undefined",
                }
            ],
            "nationalModeInfoGroups": [],
            "softwareNationalModeInfoGroups": [],
            "softwareCountriesOfOrigin": [],
            "tradeMark": None,
        }
        return f

    def capture_update(_cookie, item):
        update_bodies.append(item)

    monkeypatch.setattr("rts_pp_draft_service._ensure_monitor_finding", finding_china)
    monkeypatch.setattr("rts_pp_draft_service.load_offers_cookie", lambda _p: "c=1")
    monkeypatch.setattr("rts_pp_draft_service.validate_cookie_session", lambda _c: None)
    monkeypatch.setattr("rts_pp_draft_service.resolve_ktru_for_create", lambda *_a, **_k: _ktru_item())
    monkeypatch.setattr("rts_pp_draft_service.create_draft", lambda *_a, **_k: 555)
    monkeypatch.setattr("rts_pp_draft_service.get_draft", lambda _c, _i: _draft_item())
    monkeypatch.setattr("rts_pp_draft_service.update_draft", capture_update)
    monkeypatch.setattr("rts_pp_draft_service.draft_search_by_id", lambda *_a, **_k: {"protectedId": "PID555"})
    monkeypatch.setattr(
        "rts_pp_draft_service.fetch_national_mode_info_types",
        lambda *_a, **_k: {
            "canFillSoftwareCountries": True,
            "isIndustrial": True,
            "isProhibited": False,
            "isRestricted": True,
            "nationalModeInfoTypes": [
                {
                    "includeCountryCodes": ["643"],
                    "nationalModeInfoType": "IndustrialProductsRussianFederation",
                    "softwareNationalModeInfoType": "SoftwareRussianFederation",
                },
                {
                    "includeCountryCodes": [],
                    "excludeCountryCodes": ["643", "051", "112", "398", "417"],
                    "nationalModeInfoType": "IndustrialProductsForeignCountries",
                    "softwareNationalModeInfoType": None,
                },
            ],
        },
    )
    monkeypatch.setattr("rts_pp_draft_service.pp_participate_eligibility", lambda *_a, **_k: {"can_participate": True})
    monkeypatch.setattr(
        "rts_pp_draft_service.resolve_country_for_draft",
        lambda **_k: type("C", (), {"code": "643", "name": "Российская Федерация"})(),
    )

    result = create_pp_draft_from_monitor(
        analytics_db=env["db"],
        meta_dir=env["meta"],
        preoffers_path=env["preoffers"],
        cookie_path=env["cookie"],
        offer_price_list_item_id="oid1",
        rts_profile="IPSokolova",
    )
    assert result.ok is True
    assert update_bodies
    body = update_bodies[0]
    assert body["countriesOfOrigin"][0]["code"] == "156"
    assert body["countriesOfOrigin"][0]["name"] == "Китайская Народная Республика"
    assert body["nationalModeInfoGroups"][0]["nationalModeInfoTypeId"] == "IndustrialProductsForeignCountries"
    assert body["nationalModeInfoGroups"][0]["countryCode"] == "156"


def test_monitor_draft_copies_national_mode_from_find(monkeypatch, env: dict):
    update_bodies: list[dict] = []

    def finding_with_nm(*_a, **_k):
        f = _finding()
        f["find"] = {
            **f["find"],
            "countriesOfOrigin": [{"code": "643", "name": "Российская Федерация"}],
            "nationalModeInfoGroups": [
                {
                    "id": 77,
                    "countryCode": "643",
                    "nationalModeInfoTypeId": "IndustrialProductsRussianFederation",
                    "nationalModeInfos": [{"id": 1, "registryNumberNotProvided": True}],
                }
            ],
            "softwareNationalModeInfoGroups": [],
            "softwareCountriesOfOrigin": [],
        }
        return f

    def capture_update(_cookie, item):
        update_bodies.append(item)

    monkeypatch.setattr("rts_pp_draft_service._ensure_monitor_finding", finding_with_nm)
    monkeypatch.setattr("rts_pp_draft_service.load_offers_cookie", lambda _p: "c=1")
    monkeypatch.setattr("rts_pp_draft_service.validate_cookie_session", lambda _c: None)
    monkeypatch.setattr("rts_pp_draft_service.resolve_ktru_for_create", lambda *_a, **_k: _ktru_item())
    monkeypatch.setattr("rts_pp_draft_service.create_draft", lambda *_a, **_k: 555)
    monkeypatch.setattr("rts_pp_draft_service.get_draft", lambda _c, _i: _draft_item())
    monkeypatch.setattr("rts_pp_draft_service.update_draft", capture_update)
    monkeypatch.setattr("rts_pp_draft_service.draft_search_by_id", lambda *_a, **_k: {"protectedId": "PID555"})
    monkeypatch.setattr(
        "rts_pp_draft_service.fetch_national_mode_info_types",
        lambda *_a, **_k: {
            "canFillSoftwareCountries": True,
            "isIndustrial": True,
            "isProhibited": False,
            "isRestricted": True,
            "nationalModeInfoTypes": [
                {
                    "includeCountryCodes": ["643"],
                    "nationalModeInfoType": "IndustrialProductsRussianFederation",
                    "softwareNationalModeInfoType": "SoftwareRussianFederation",
                }
            ],
        },
    )
    monkeypatch.setattr("rts_pp_draft_service.pp_participate_eligibility", lambda *_a, **_k: {"can_participate": True})
    monkeypatch.setattr(
        "rts_pp_draft_service.resolve_country_for_draft",
        lambda **_k: type("C", (), {"code": "643", "name": "Российская Федерация"})(),
    )

    result = create_pp_draft_from_monitor(
        analytics_db=env["db"],
        meta_dir=env["meta"],
        preoffers_path=env["preoffers"],
        cookie_path=env["cookie"],
        offer_price_list_item_id="oid1",
        rts_profile="IPSokolova",
    )
    assert result.ok is True
    assert update_bodies
    body = update_bodies[0]
    assert body["countriesOfOrigin"][0]["code"] == "643"
    assert body["countriesOfOrigin"][0]["category"] == "Undefined"
    nm = body["nationalModeInfoGroups"]
    assert nm[0]["nationalModeInfoTypeId"] == "IndustrialProductsRussianFederation"
    assert nm[0]["nationalModeInfos"][0]["registryNumberNotProvided"] is True
    assert body["softwareNationalModeInfoGroups"] == []
    assert result.fill_report.get("national_mode_source") == "find"
    assert body.get("tradeMark") in (None, "")

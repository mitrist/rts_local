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

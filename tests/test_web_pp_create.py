from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from eis_analytics_db import analytics_db_path, save_rts_pp_draft
from rts_pp_draft_service import DraftResult
from web_ui import main as web_main

CHAIR_CODE = "31.01.11.150-00000003"


def test_pp_create_page_200():
    client = TestClient(web_main.app)
    r = client.get("/pp-create")
    assert r.status_code == 200
    assert "Создать ПП" in r.text
    assert "Наименование КТРУ" in r.text
    assert "Нет в реестре" in r.text
    assert "Количество" in r.text
    assert "20–40" in r.text


def test_ktru_search_and_template_and_oktmo(monkeypatch, tmp_path):
    cache = tmp_path / "ktru_rts_cache"
    cache.mkdir()
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "load_offers_cookie", lambda _p: "cookie")
    monkeypatch.setattr(
        web_main,
        "search_ktru_for_wizard",
        lambda _cookie, q, _cache: (
            [{"code": CHAIR_CODE, "name": "Стул на металлическом каркасе", "version": 5}],
            "",
        ),
    )
    monkeypatch.setattr(
        web_main,
        "get_ktru_template",
        lambda _cookie, code, _cache: {
            "code": code,
            "name": "Стул на металлическом каркасе",
            "version": 5,
            "okeis": [{"code": "796", "fullName": "Штука"}],
            "characteristics": [
                {
                    "code": "8e50a5cd-4",
                    "name": "Наличие мягкого сидения",
                    "isActual": True,
                    "isRequired": True,
                    "characteristicKind": {"id": 1, "name": "Неизменяемая заказчиком"},
                    "characteristicValues": [{"valueCode": "b9a85776-1", "qualityDescription": "Да"}],
                }
            ],
        },
    )
    client = TestClient(web_main.app)
    search = client.get("/api/rts/ktru/search", params={"q": "Стул"})
    assert search.status_code == 200
    assert search.json()["items"][0]["code"] == CHAIR_CODE

    tpl = client.get("/api/rts/ktru/template", params={"code": CHAIR_CODE})
    assert tpl.status_code == 200
    body = tpl.json()["template"]
    assert body["code"] == CHAIR_CODE
    assert body["characteristics"][0]["locked"] is True

    oktmo = client.get("/api/rts/pp/oktmo-subjects")
    assert oktmo.status_code == 200
    codes = {row["code"] for row in oktmo.json()["items"]}
    assert "01000000" in codes
    assert "03000000" in codes


def test_draft_from_catalog_api(monkeypatch, tmp_path):
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "a.sqlite3")
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_cookie_path_for_request", lambda _r: tmp_path / "c.txt")
    monkeypatch.setattr(web_main, "_require_operator_rts_profile", lambda _r: "IPSokolova")
    result = DraftResult(
        ok=True,
        rts_draft_id=701001,
        protected_id="p1",
        ktru_code=CHAIR_CODE,
        purchase_number="catalog",
        line_key="lk-1",
        fill_report={"ktru_name": "Стул", "regions_count": 2},
    )
    mock = MagicMock(return_value=result)
    monkeypatch.setattr(web_main, "create_pp_draft_from_catalog", mock)
    client = TestClient(web_main.app)
    r = client.post(
        "/api/rts/pp/draft-from-catalog",
        json={
            "ktru_code": CHAIR_CODE,
            "characteristics": [{"code": "94b59978-4", "value_code": "c98c9acd-2"}],
            "regions": [{"code": "01000000", "unit_price": "4500", "quantity": "10"}],
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["rts_draft_id"] == 701001
    assert data["source"] == "catalog"
    assert data["source_key"] == "catalog:lk-1"
    mock.assert_called_once()


def test_pp_drafts_lists_catalog_source(monkeypatch, tmp_path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="catalog",
        line_key="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ktru_code=CHAIR_CODE,
        rts_draft_id=701001,
        status="created",
        rts_profile="IPSokolova",
        created_at_utc=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        fill_report_json='{"source":"catalog","ktru_name":"Стул на металлическом каркасе","regions_count":2}',
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: False)
    monkeypatch.setattr(web_main, "_rts_profile_for_request", lambda _req: "IPSokolova")
    monkeypatch.delenv("RTS_PP_DRAFTS_ENRICH_RTS", raising=False)
    client = TestClient(web_main.app)
    r = client.get("/pp-drafts")
    assert r.status_code == 200
    assert "Справочник КТРУ" in r.text
    assert "Стул на металлическом каркасе" in r.text
    assert "Создать ПП" in r.text
    assert "2 рег." in r.text

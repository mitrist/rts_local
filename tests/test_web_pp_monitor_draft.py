from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from eis_analytics_db import upsert_pp_monitor_finding
from rts_pp_draft_service import DraftResult
from web_ui import auth as web_auth
from web_ui import main as web_main


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _seed_finding(db: Path, oid: str = "sample1") -> None:
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id=oid,
        find_json=json.dumps(
            {
                "offerPriceListItemId": oid,
                "ktru": {"code": "26.20.13.000-00000002", "name": "Панель"},
                "ktruCharacteristics": [],
            }
        ),
        ktru_code="26.20.13.000-00000002",
    )


def test_draft_from_monitor_api(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_finding(db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)

    mock_result = DraftResult(
        ok=True,
        rts_draft_id=555,
        edit_url="https://offers.rts-tender.ru/supplier/price-lists/positions/555/edit",
        ktru_code="26.20.13.000-00000002",
        verify_report={"ok": 3, "issues": 0, "total": 3},
    )
    create_mock = MagicMock(return_value=mock_result)
    monkeypatch.setattr(web_main, "create_pp_draft_from_monitor", create_mock)

    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")
    r = client.post(
        "/api/rts/pp/draft-from-monitor",
        json={"offer_price_list_item_id": "sample1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["rts_draft_id"] == 555
    create_mock.assert_called_once()


def test_draft_from_monitor_not_found(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "empty.sqlite3")
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")
    r = client.post(
        "/api/rts/pp/draft-from-monitor",
        json={"offer_price_list_item_id": "missing"},
    )
    assert r.status_code == 404


def test_draft_from_monitor_idempotent(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_finding(db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)

    mock_result = DraftResult(ok=True, already_existed=True, rts_draft_id=777, edit_url="https://x.test")
    monkeypatch.setattr(web_main, "create_pp_draft_from_monitor", MagicMock(return_value=mock_result))

    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")
    r = client.post(
        "/api/rts/pp/draft-from-monitor",
        json={"offer_price_list_item_id": "sample1"},
    )
    assert r.status_code == 200
    assert r.json()["already_existed"] is True

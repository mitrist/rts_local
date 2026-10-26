from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from web_ui import auth as web_auth
from web_ui import main as web_main


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_admin_html_fetch_min_price_save(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    monkeypatch.delenv("EIS_HTML_FETCH_MIN_PRICE", raising=False)
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    saved = client.post(
        "/action/admin/html-fetch-min-price-save",
        data={"html_fetch_min_price": "150000"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    path = tmp_path / "eis_rss_cron_settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["html_fetch_min_price"] == 150000.0
    assert web_main._html_fetch_min_price() == 150000.0

    cleared = client.post(
        "/action/admin/html-fetch-min-price-save",
        data={"html_fetch_min_price": ""},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    data2 = json.loads(path.read_text(encoding="utf-8"))
    assert data2.get("html_fetch_min_price") is None
    assert web_main._html_fetch_min_price() is None

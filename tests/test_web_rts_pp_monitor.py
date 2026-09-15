from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from rts_pp_monitor_settings import load_settings, settings_path
from web_ui import auth as web_auth
from web_ui import main as web_main


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_rts_pp_monitor_page_admin_only(monkeypatch, tmp_path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\nIPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)

    _login(client, "IPSokolova", "pass")
    blocked = client.get("/rts-pp-monitor", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/analytics"

    client.post("/logout", follow_redirects=False)
    _login(client, "admin", "secret")
    page = client.get("/rts-pp-monitor")
    assert page.status_code == 200
    assert "Монитор ПП РТС" in page.text
    assert "Backfill реестра" in page.text


def test_rts_pp_monitor_save_form(monkeypatch, tmp_path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    r = client.post(
        "/action/rts-pp-monitor-save",
        data={
            "enabled": "on",
            "poll_interval_min": "30",
            "ktru_codes_text": "26.20.13.000-00000002",
            "notify_telegram": "on",
            "notify_max": "on",
            "skip_expired": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    saved = load_settings(settings_path(tmp_path))
    assert saved["enabled"] is True
    assert saved["poll_interval_min"] == 30
    assert saved["ktru_codes"] == ["26.20.13.000-00000002"]

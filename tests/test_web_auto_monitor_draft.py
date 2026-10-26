"""Web admin tests for auto monitor draft settings."""

from __future__ import annotations

from pathlib import Path

from auto_monitor_draft_settings import load_settings, settings_path
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


def test_admin_auto_monitor_draft_save(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\nIPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin,IPSokolova")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "a.sqlite3")
    client = TestClient(web_main.app)

    _login(client, "IPSokolova", "pass")
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Авточерновик из монитора ПП" in page.text

    save = client.post(
        "/action/admin/auto-monitor-draft-save",
        data={
            "enabled": "1",
            "auto_publish": "1",
            "ktru_codes_text": "27.51.11.110-00000009\n08.12.12.140-00000005",
            "min_pp_amount": "250000",
            "max_per_hour": "4",
            "rts_profile": "IPSokolova",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303
    assert "#auto-monitor-draft-runtime" in save.headers["location"]
    saved = load_settings(settings_path(tmp_path))
    assert saved["enabled"] is True
    assert saved["auto_publish"] is True
    assert saved["min_pp_amount"] == 250000
    assert saved["max_per_hour"] == 4
    assert len(saved["ktru_codes"]) == 2

    off = client.post(
        "/action/admin/auto-monitor-draft-toggle",
        data={"enabled": "0"},
        follow_redirects=False,
    )
    assert off.status_code == 303
    assert load_settings(settings_path(tmp_path))["enabled"] is False

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from rts_pp_monitor_cron import cron_status_payload, poll_interval_sec, run_poll_once, save_cron_state
from rts_pp_monitor_settings import apply_admin_runtime_settings, default_settings, load_settings, save_settings, settings_path
from web_ui import auth as web_auth
from web_ui import main as web_main


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_admin_pp_monitor_runtime_section(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    monkeypatch.setenv("RTS_PP_MONITOR", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_CRON", "1")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    save_settings(settings_path(tmp_path), {**default_settings(), "enabled": True, "poll_interval_min": 25})
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    page = client.get("/admin")
    assert page.status_code == 200
    assert "Настройка монитора ПП" in page.text
    assert "Выключить монитор" in page.text
    assert 'value="25"' in page.text


def test_admin_pp_monitor_toggle_and_interval(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    save_settings(settings_path(tmp_path), {**default_settings(), "enabled": False, "poll_interval_min": 20})

    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    on = client.post("/action/admin/pp-monitor-toggle", data={"enabled": "1"}, follow_redirects=False)
    assert on.status_code == 303
    assert load_settings(settings_path(tmp_path))["enabled"] is True

    off = client.post("/action/admin/pp-monitor-toggle", data={"enabled": "0"}, follow_redirects=False)
    assert off.status_code == 303
    assert load_settings(settings_path(tmp_path))["enabled"] is False

    interval = client.post(
        "/action/admin/pp-monitor-interval-save",
        data={"poll_interval_min": "45"},
        follow_redirects=False,
    )
    assert interval.status_code == 303
    assert load_settings(settings_path(tmp_path))["poll_interval_min"] == 45


def test_poll_interval_sec_prefers_settings(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    monkeypatch.setenv("RTS_PP_MONITOR_INTERVAL_SEC", "9999")
    save_settings(settings_path(tmp_path), {**default_settings(), "poll_interval_min": 30})
    assert poll_interval_sec() == 30 * 60


def test_run_poll_once_skips_until_interval_elapsed(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    save_settings(settings_path(tmp_path), {**default_settings(), "enabled": True, "poll_interval_min": 20})
    finished = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    save_cron_state({"running": False, "finished_at_utc": finished, "ok": True})

    skipped = run_poll_once(force=False)
    assert skipped.get("skipped") is True
    assert skipped.get("reason") == "interval_wait"

    forced = run_poll_once(force=True)
    assert forced.get("reason") != "interval_wait"


def test_apply_admin_runtime_preserves_notify_fields(tmp_path: Path):
    base = {
        **default_settings(),
        "enabled": False,
        "poll_interval_min": 20,
        "ktru_codes": ["26.20.13.000-00000002"],
        "telegram_chat_id": "-100123",
        "filters": {"price_min": 50000.0, "price_max": None, "region_ids": [], "skip_expired": True},
    }
    save_settings(settings_path(tmp_path), base)
    existing = load_settings(settings_path(tmp_path))
    updated = apply_admin_runtime_settings(existing, enabled=True, poll_interval_min=35)
    assert updated["enabled"] is True
    assert updated["poll_interval_min"] == 35
    assert updated["ktru_codes"] == ["26.20.13.000-00000002"]
    assert updated["telegram_chat_id"] == "-100123"
    assert updated["filters"]["price_min"] == 50000.0

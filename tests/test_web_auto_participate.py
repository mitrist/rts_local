from __future__ import annotations

from pathlib import Path

from auto_participate_settings import load_settings, settings_path
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


def test_auto_participate_page_admin_only(monkeypatch, tmp_path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\nIPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)

    blocked = client.get("/auto-participate", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"].startswith("/login")

    _login(client, "IPSokolova", "pass")
    operator = client.get("/auto-participate", follow_redirects=False)
    assert operator.status_code == 303
    assert operator.headers["location"] == "/analytics"

    client.post("/logout", follow_redirects=False)
    _login(client, "admin", "secret")
    page = client.get("/auto-participate")
    assert page.status_code == 200
    assert "Авто-участие" in page.text
    assert "IPSokolova" in page.text
    assert "Nacpro" in page.text


def test_auto_participate_save_form(monkeypatch, tmp_path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    r = client.post(
        "/action/auto-participate-save",
        data={
            "profile": "IPSokolova",
            "enabled": "on",
            "min_purchase_amount": "300000",
            "ktru_mode": "whitelist",
            "ktru_codes_text": "08.12.12.140-00000005",
            "min_match_ratio": "50",
            "max_per_hour": "7",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "profile=IPSokolova" in r.headers["location"]

    saved = load_settings(settings_path(tmp_path))
    rules = saved["profiles"]["IPSokolova"]
    assert rules["enabled"] is True
    assert rules["min_purchase_amount"] == 300000
    assert rules["ktru_mode"] == "whitelist"
    assert rules["ktru_codes"] == ["08.12.12.140-00000005"]
    assert rules["min_match_ratio"] == 0.5
    assert rules["max_per_hour"] == 7


def test_sidebar_hides_auto_participate_for_operator(monkeypatch, tmp_path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\nIPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)

    _login(client, "IPSokolova", "pass")
    page = client.get("/pp-drafts")
    assert page.status_code == 200
    assert "Авто-участие" not in page.text

    client.post("/logout", follow_redirects=False)
    _login(client, "admin", "secret")
    admin_page = client.get("/pp-drafts")
    assert "Авто-участие" in admin_page.text


def test_admin_auto_participate_toggle(monkeypatch, tmp_path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    page = client.get("/admin")
    assert page.status_code == 200
    assert "Настройка автоучастия" in page.text
    assert "Включить для всех профилей" in page.text

    on = client.post("/action/admin/auto-participate-toggle", data={"enabled": "1"}, follow_redirects=False)
    assert on.status_code == 303
    saved = load_settings(settings_path(tmp_path))
    assert saved["enabled"] is True
    assert saved["profiles"]["IPSokolova"]["enabled"] is True
    assert saved["profiles"]["Nacpro"]["enabled"] is True

    off = client.post("/action/admin/auto-participate-toggle", data={"enabled": "0"}, follow_redirects=False)
    assert off.status_code == 303
    saved2 = load_settings(settings_path(tmp_path))
    assert saved2["enabled"] is False
    assert saved2["profiles"]["IPSokolova"]["enabled"] is False
    assert saved2["profiles"]["Nacpro"]["enabled"] is False

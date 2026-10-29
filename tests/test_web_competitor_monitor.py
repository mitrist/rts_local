from __future__ import annotations

from fastapi.testclient import TestClient

from web_ui import auth as web_auth
from web_ui import main as web_main


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _client(monkeypatch, tmp_path, users: str) -> TestClient:
    users_file = tmp_path / "web_users.txt"
    users_file.write_text(users, encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    return TestClient(web_main.app)


def test_competitor_monitor_admin_only_and_menu(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, "admin:secret\nIPSokolova:pass\n")
    _login(client, "IPSokolova", "pass")
    blocked = client.get("/competitor-monitor", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/analytics"

    client.post("/logout", follow_redirects=False)
    _login(client, "admin", "secret")
    page = client.get("/competitor-monitor")
    assert page.status_code == 200
    assert "Монитор конкурентов" in page.text
    assert "Реестр предложений" not in page.text
    assert "Монитор ПП РТС" not in page.text
    assert "Цепочка ПП" not in page.text
    assert "31.01.12.122-00000004" in page.text


def test_competitor_monitor_save_schedule(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, "admin:secret\n")
    _login(client, "admin", "secret")
    response = client.post(
        "/action/competitor-monitor-save",
        data={"enabled": "1", "interval_days": "7", "weekday": "2", "hour_msk": "8"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/competitor-monitor")
    assert "Расписание сохранено" in page.text
    assert 'value="7" selected' in page.text
    assert 'value="2" selected' in page.text
    assert 'name="hour_msk" min="0" max="23" value="8"' in page.text

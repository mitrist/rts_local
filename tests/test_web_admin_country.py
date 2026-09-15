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


def test_admin_page_admin_only(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\nIPSokolova:pass\n", encoding="utf-8")
    country_file = tmp_path / "country_origin.csv"
    country_file.write_text(
        "position_display_name;Столбец1\n"
        "Диван;Российская Федерация\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    monkeypatch.setenv("RTS_KTRU_COUNTRY_FILE", str(country_file))
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)

    _login(client, "IPSokolova", "pass")
    denied = client.get("/admin", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"] == "/analytics"

    _login(client, "admin", "secret")
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Страна происхождения" in page.text
    assert "Диван" in page.text


def test_admin_country_origin_crud(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    country_file = tmp_path / "country_origin.csv"
    country_file.write_text("position_display_name;country_name\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    monkeypatch.setenv("RTS_KTRU_COUNTRY_FILE", str(country_file))
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    save = client.post(
        "/action/admin/country-origin-save",
        data={
            "title": "3D-принтер FDM",
            "ktru_code": "26.20.16.120-00000104",
            "country_name": "Китайская Народная Республика",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303
    page = client.get("/admin")
    assert "3D-принтер FDM" in page.text
    assert "26.20.16.120-00000104" in page.text

    from ktru_country_origin import load_country_origin_rows

    rows = load_country_origin_rows(country_file)
    assert len(rows) == 1
    row_id = rows[0].row_id

    delete = client.post(
        "/action/admin/country-origin-delete",
        data={"row_id": row_id},
        follow_redirects=False,
    )
    assert delete.status_code == 303
    assert load_country_origin_rows(country_file) == []


def test_admin_notices_and_web_users(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_EIS_ENV", tmp_path / "env.txt")
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    page = client.get("/admin")
    assert page.status_code == 200
    assert "Уведомления: извещения" in page.text
    assert "Web users" in page.text
    assert 'class="admin-section' in page.text

    save = client.post(
        "/action/admin/notices-notify-save",
        data={
            "telegram_chat_id": "-100111",
            "max_chat_id": "max-1",
            "notify_telegram": "1",
            "min_purchase_amount": "100000",
            "max_purchase_amount": "5000000",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303
    from notification_settings import load_settings, settings_path

    data = load_settings(settings_path(tmp_path))
    assert data["telegram_chat_id"] == "-100111"
    assert data["max_purchase_amount"] == 5000000.0

    user_save = client.post(
        "/action/admin/web-user-save",
        data={"login": "IPSokolova", "password": "pass123"},
        follow_redirects=False,
    )
    assert user_save.status_code == 303
    assert "IPSokolova" in users_file.read_text(encoding="utf-8")


def test_admin_ktru_cache_extend(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    cache_dir = tmp_path / "ktru_rts_cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "26.20.18.000-00000069.json"
    cache_file.write_text(
        json.dumps(
            {
                "_cache_meta": {
                    "cached_at_unix": 1_700_000_000,
                    "ktru_code": "26.20.18.000-00000069",
                    "version": 23,
                },
                "item": {
                    "code": "26.20.18.000-00000069",
                    "name": "МФУ",
                    "version": 23,
                    "characteristics": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache_dir)
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    page = client.get("/admin")
    assert page.status_code == 200
    assert "Кэш шаблонов КТРУ" in page.text
    assert "26.20.18.000-00000069" in page.text
    assert "+7 дней" in page.text

    extend = client.post(
        "/action/admin/ktru-cache-extend",
        data={"ktru_code": "26.20.18.000-00000069"},
        follow_redirects=False,
    )
    assert extend.status_code == 303
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    from rts_ktru_cache import load_cached_template

    assert load_cached_template(cache_dir, "26.20.18.000-00000069") is not None
    assert raw["_cache_meta"]["cached_at_unix"] > 1_700_000_000


def test_admin_ktru_cache_extend_all(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    cache_dir = tmp_path / "ktru_rts_cache"
    cache_dir.mkdir()
    for code in ("K-1", "K-2"):
        (cache_dir / f"{code}.json").write_text(
            json.dumps(
                {
                    "_cache_meta": {"cached_at_unix": 1_700_000_000, "ktru_code": code, "version": 1},
                    "item": {"code": code, "name": code, "version": 1, "characteristics": []},
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache_dir)
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    page = client.get("/admin")
    assert "Продлить все на 7 дней" in page.text

    extend = client.post("/action/admin/ktru-cache-extend-all", follow_redirects=False)
    assert extend.status_code == 303
    from rts_ktru_cache import load_cached_template

    assert load_cached_template(cache_dir, "K-1") is not None
    assert load_cached_template(cache_dir, "K-2") is not None

"""Тесты веб-авторизации (/login, сессия, web_users.txt)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_ui import auth as web_auth
from web_ui import main as web_main


def test_parse_web_users_file(tmp_path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text(
        "# comment\nalice:pass1\nbob:secret:with:colons\nalice:pass2\n",
        encoding="utf-8",
    )
    users = web_auth.parse_web_users_file(users_file)
    assert users["alice"] == "pass2"
    assert users["bob"] == "secret:with:colons"


def test_auth_disabled_without_users(monkeypatch, tmp_path):
    missing = tmp_path / "no_users.txt"
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(missing))
    monkeypatch.delenv("RTS_WEB_USER", raising=False)
    monkeypatch.delenv("RTS_WEB_PASSWORD", raising=False)
    web_auth.configure_web_auth(missing)
    assert web_auth.auth_enabled() is False

    client = TestClient(web_main.app)
    r = client.get("/", follow_redirects=True)
    assert r.status_code == 200
    assert "Извещения о закупке" in r.text


def test_login_flow(monkeypatch, tmp_path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("alice:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.delenv("RTS_WEB_USER", raising=False)
    monkeypatch.delenv("RTS_WEB_PASSWORD", raising=False)
    web_auth.configure_web_auth(users_file)

    client = TestClient(web_main.app)
    blocked = client.get("/", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"].startswith("/login")

    bad = client.post(
        "/login",
        data={"username": "alice", "password": "wrong", "next": "/"},
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert "login" in bad.headers["location"]

    ok = client.post("/login", data={"username": "alice", "password": "secret", "next": "/"}, follow_redirects=False)
    assert ok.status_code == 303
    assert ok.headers["location"] == "/analytics"

    home = client.get("/", follow_redirects=True)
    assert home.status_code == 200
    assert "Извещения о закупке" in home.text

    assert client.get("/api/status", follow_redirects=False).status_code == 200

    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/api/status", follow_redirects=False).status_code == 401


def test_env_fallback_single_user(monkeypatch, tmp_path):
    missing = tmp_path / "missing.txt"
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(missing))
    monkeypatch.setenv("RTS_WEB_USER", "legacy")
    monkeypatch.setenv("RTS_WEB_PASSWORD", "legacy-pass")
    web_auth.configure_web_auth(missing)
    assert web_auth.auth_enabled() is True
    assert web_auth.verify_credentials("legacy", "legacy-pass")

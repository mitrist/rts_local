"""API tests for PP publish endpoints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from rts_pp_publish_service import PublishJobResult
from web_ui import auth as web_auth
from web_ui import main as web_main


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _auth_client(monkeypatch, tmp_path: Path) -> TestClient:
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "eis.sqlite3")
    return TestClient(web_main.app)


def test_publish_start_disabled(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("RTS_PP_PUBLISH_ENABLED", raising=False)
    client = _auth_client(monkeypatch, tmp_path)
    _login(client, "IPSokolova", "pass")
    r = client.post("/api/rts/pp/publish/start", json={"rts_draft_id": 1})
    assert r.status_code == 404


def test_publish_start_and_submit(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RTS_PP_PUBLISH_ENABLED", "1")
    client = _auth_client(monkeypatch, tmp_path)
    _login(client, "IPSokolova", "pass")

    start_mock = MagicMock(
        return_value=PublishJobResult(
            ok=True,
            job_id=10,
            status="awaiting_signature",
            rts_draft_id=55,
            sign_requests=[{"kind": "card", "file_guid": None, "data": "QQ==", "encoding": "base64"}],
        )
    )
    submit_mock = MagicMock(
        return_value=PublishJobResult(
            ok=True,
            job_id=10,
            status="published",
            rts_draft_id=55,
        )
    )
    monkeypatch.setattr(web_main, "start_publish_job", start_mock)
    monkeypatch.setattr(web_main, "submit_publish_signatures", submit_mock)

    r = client.post(
        "/api/rts/pp/publish/start",
        json={"rts_draft_id": 55, "source": "monitor"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == 10
    assert data["status"] == "awaiting_signature"
    assert len(data["sign_requests"]) == 1
    start_mock.assert_called_once()

    r2 = client.post(
        "/api/rts/pp/publish/submit-signatures",
        json={"job_id": 10, "signatures": [{"signatureWithTimeStamp": "CMS"}]},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "published"
    submit_mock.assert_called_once()


def test_publish_status(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RTS_PP_PUBLISH_ENABLED", "1")
    client = _auth_client(monkeypatch, tmp_path)
    _login(client, "IPSokolova", "pass")
    monkeypatch.setattr(
        web_main,
        "get_publish_job_status",
        MagicMock(
            return_value=PublishJobResult(
                ok=True, job_id=3, status="awaiting_signature", rts_draft_id=9
            )
        ),
    )
    r = client.get("/api/rts/pp/publish/status", params={"job_id": 3})
    assert r.status_code == 200
    assert r.json()["job_id"] == 3

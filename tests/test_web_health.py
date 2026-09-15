from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from web_ui import main as web_main


def test_health_live_no_auth():
    client = TestClient(web_main.app)
    r = client.get("/health/live")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data.get("degraded") is False
    assert "event_loop_lag_sec" in data
    assert "uptime_sec" in data


def test_health_ready_without_db(monkeypatch, tmp_path: Path):
    db = tmp_path / "missing.sqlite3"
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    with client:
        r = client.get("/health/ready")
    assert r.status_code == 503
    data = r.json()
    assert data.get("ready") is False

"""Тесты приёма cookie РТС (rts_cookie_ingest + HTTP API)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rts_cookie_ingest import (
    cookie_has_auth_markers,
    cookie_has_customer_auth_markers,
    parse_cookie_payload,
    probe_rts_session,
    save_cookie_from_ingest,
    validate_ingest_api_key,
)
from web_ui import main as web_main


def _sample_cookie() -> str:
    return ".Auth.Sso=abc; .Auth.Rts.P=xyz; other=1"


def test_parse_cookie_payload_variants():
    c = _sample_cookie()
    assert parse_cookie_payload({"cookie": c}) == c
    assert parse_cookie_payload({"output": c}) == c
    assert parse_cookie_payload({"cookies": [{"name": ".Auth.Sso", "value": "abc"}, {"name": ".Auth.Rts.P", "value": "xyz"}]}) == (
        ".Auth.Sso=abc; .Auth.Rts.P=xyz"
    )


def _sample_customer_cookie() -> str:
    return ".Auth.Sso=abc; .Auth.Rts.C=xyz; other=1"


def test_cookie_has_auth_markers():
    assert cookie_has_auth_markers(_sample_cookie()) is True
    assert cookie_has_auth_markers(_sample_customer_cookie()) is False
    assert cookie_has_auth_markers("foo=bar") is False


def test_cookie_has_customer_auth_markers():
    assert cookie_has_customer_auth_markers(_sample_customer_cookie()) is True
    assert cookie_has_customer_auth_markers(_sample_cookie()) is True
    assert cookie_has_customer_auth_markers(".Auth.Sso=x") is False


def test_save_customer_cookie_from_ingest(tmp_path):
    cookie_path = tmp_path / "cookies" / "customer_monitor.txt"
    meta_dir = tmp_path / "meta"
    with patch("rts_pp_api.probe_offers_session", return_value={"ok": False, "channel": "offers"}):
        result = save_cookie_from_ingest(
            cookie_path=cookie_path,
            meta_dir=meta_dir,
            body={"cookie": _sample_customer_cookie()},
            probe=False,
            rts_profile="customer_monitor",
        )
    assert result["ok"] is True
    assert cookie_path.is_file()
    assert ".Auth.Rts.C=xyz" in cookie_path.read_text(encoding="utf-8")


def test_save_cookie_from_ingest_writes_file_and_meta(tmp_path):
    cookie_path = tmp_path / "cookies_rts.txt"
    meta_dir = tmp_path / "meta"
    with patch("rts_pp_api.probe_offers_session", return_value={"ok": True, "channel": "offers"}):
        result = save_cookie_from_ingest(
            cookie_path=cookie_path,
            meta_dir=meta_dir,
            body={"cookie": _sample_cookie()},
            probe=True,
        )
    assert result["ok"] is True
    assert cookie_path.is_file()
    assert ".Auth.Sso=abc" in cookie_path.read_text(encoding="utf-8")
    assert (meta_dir / "rts_cookie_ingest_meta.json").is_file()


def test_api_rts_cookie_save_requires_key(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_COOKIE_INGEST_KEY", "secret-key")
    monkeypatch.setattr(web_main, "_META", tmp_path / "meta")

    client = TestClient(web_main.app)
    r_bad = client.post(
        "/api/rts/cookie/save",
        headers={"api-key": "wrong-key"},
        json={"cookie": _sample_cookie(), "profile": "IPSokolova"},
    )
    assert r_bad.status_code == 403

    with patch("rts_pp_api.probe_offers_session", return_value={"ok": True, "channel": "offers"}):
        r = client.post(
            "/api/rts/cookie/save",
            headers={"api-key": "secret-key"},
            json={"cookie": _sample_cookie(), "profile": "IPSokolova"},
        )
    assert r.status_code == 200

    with patch("rts_pp_api.probe_offers_session", return_value={"ok": True, "channel": "offers"}):
        r2 = client.post(
            "/api/rts/cookie/save",
            headers={"api-key": "secret-key"},
            json={"cookie": _sample_cookie(), "probe": False, "profile": "IPSokolova"},
        )
    assert r2.status_code == 200
    assert r2.json()["ok"] is True
    assert r2.json()["rts_profile"] == "IPSokolova"
    cookie_file = (tmp_path / "meta" / "cookies" / "IPSokolova.txt")
    assert cookie_file.is_file()


def test_api_rts_cookie_save_disabled_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv("RTS_COOKIE_INGEST_KEY", raising=False)
    monkeypatch.setattr(web_main, "_META", tmp_path / "meta")
    client = TestClient(web_main.app)
    r = client.post(
        "/api/rts/cookie/save",
        headers={"api-key": "x"},
        json={"cookie": _sample_cookie(), "profile": "Nacpro"},
    )
    assert r.status_code == 503


def test_probe_rts_session_prefers_offers(monkeypatch):
    monkeypatch.setattr(
        "rts_pp_api.probe_offers_session",
        lambda *a, **k: {"ok": True, "channel": "offers"},
    )
    monkeypatch.setattr(
        "rts_cookie_ingest._probe_lk_session",
        lambda *a, **k: {"ok": False, "channel": "lk", "reason": "login_html"},
    )
    result = probe_rts_session(_sample_cookie())
    assert result["ok"] is True
    assert result["channel"] == "offers"


def test_validate_ingest_api_key():
    with patch("rts_cookie_ingest.cookie_ingest_api_key", return_value="k"):
        assert validate_ingest_api_key("k") is True
        assert validate_ingest_api_key("wrong") is False

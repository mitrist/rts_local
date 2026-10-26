from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import auto_participate as ap
from auto_participate_settings import parse_profile_rules, save_settings, settings_path
from eis_analytics_db import analytics_db_path, record_notice_html_page, record_rss_ktru_hints, record_rss_poll_run
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from fastapi.testclient import TestClient
from rts_ktru_cache import save_cached_template
from rts_pp_draft_service import DraftResult
from rts_preoffers import upsert_preoffer

from web_ui import auth as web_auth
from web_ui import main as web_main


KTRU = "08.12.12.140-00000005"


def _seed_catalog_rss(db: Path, items: list[dict], *, ktru: str = KTRU) -> None:
    record_rss_poll_run(
        db,
        items,
        query_label="t",
        rss_url="https://example.test/rss",
        started_at_utc=datetime.now(timezone.utc),
        finished_at_utc=datetime.now(timezone.utc),
        ok=True,
        errors=[],
    )
    record_rss_ktru_hints(db, [str(x["reg_number"]) for x in items], ktru)


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_rss_interval_accepts_20(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")
    r = client.post(
        "/action/eis-rss-cron-start",
        data={
            "rss_url": "https://example.test/rss",
            "rss_interval_sec": "20",
            "rss_query_label": "test",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert web_main._eis_rss_cron.interval_sec == 20


def test_rss_poll_does_not_call_html_fetch(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path / "data")
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    called = {"n": 0}

    def _boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("HTML must not run from RSS poll")

    monkeypatch.setattr(web_main, "_auto_fetch_notice_html_after_rss_poll", _boom)

    class _Res:
        ok = True
        total_items = 1
        new_items = 1
        run_id = 1
        errors: list[str] = []

    monkeypatch.setattr(web_main, "run_eis_rss_poll", lambda *_a, **_k: _Res())
    monkeypatch.setattr(web_main, "_catalog_ktru_codes", lambda: [])
    web_main._eis_rss_cron.rss_url = "https://example.test/rss"
    web_main._eis_rss_cron.query_label = "t"
    web_main._run_eis_rss_ui_cron_poll()
    assert called["n"] == 0


def test_html_fetch_uses_thread_pool(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH", "1")
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH_WORKERS", "3")
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH_DELAY_SEC", "0")
    db = analytics_db_path(tmp_path / "data")
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_html_fetch_min_price", lambda: None)
    monkeypatch.setattr(web_main, "_catalog_ktru_codes", lambda: [KTRU])
    monkeypatch.setattr(web_main, "_notify_preoffer_watch_matches", lambda: None)
    seen = {"n": 0}

    def _fetch(pn, url, timeout=30.0):
        seen["n"] += 1
        return (1, 1)

    monkeypatch.setattr(web_main, "_fetch_and_store_notice_html", _fetch)
    _seed_catalog_rss(
        db,
        [{"reg_number": f"PN{i}", "link": f"https://example.test/{i}", "price_text": "200000"} for i in range(3)],
    )
    web_main._auto_fetch_notice_html_after_rss_poll()
    assert seen["n"] == 3


def test_html_fetch_includes_small_notices_below_min_price(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH", "1")
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH_DELAY_SEC", "0")
    db = analytics_db_path(tmp_path / "data")
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_html_fetch_min_price", lambda: 150000.0)
    monkeypatch.setattr(web_main, "_catalog_ktru_codes", lambda: [KTRU])
    monkeypatch.setattr(web_main, "_notify_preoffer_watch_matches", lambda: None)
    fetched: list[str] = []

    def _fetch(pn, url, timeout=30.0):
        fetched.append(pn)
        return (1, 1)

    monkeypatch.setattr(web_main, "_fetch_and_store_notice_html", _fetch)
    _seed_catalog_rss(
        db,
        [
            {
                "reg_number": "0342300012026000185",
                "link": "https://example.test/small",
                "price_text": "12000,00",
            }
        ],
    )
    web_main._auto_fetch_notice_html_after_rss_poll()
    assert fetched == ["0342300012026000185"]


def test_html_auto_fetch_skipped_when_disabled(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH", "0")
    db = analytics_db_path(tmp_path / "data")
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_catalog_ktru_codes", lambda: [KTRU])
    fetched: list[str] = []
    monkeypatch.setattr(
        web_main,
        "_fetch_and_store_notice_html",
        lambda pn, url, timeout=30.0: fetched.append(pn) or (1, 1),
    )
    _seed_catalog_rss(
        db,
        [{"reg_number": "PN1", "link": "https://example.test/1", "price_text": "200000"}],
    )
    web_main._auto_fetch_notice_html_after_rss_poll()
    assert fetched == []


def test_html_auto_fetch_skips_notices_without_catalog_ktru(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH", "1")
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH_DELAY_SEC", "0")
    db = analytics_db_path(tmp_path / "data")
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_catalog_ktru_codes", lambda: [KTRU])
    monkeypatch.setattr(web_main, "_notify_preoffer_watch_matches", lambda: None)
    fetched: list[str] = []
    monkeypatch.setattr(
        web_main,
        "_fetch_and_store_notice_html",
        lambda pn, url, timeout=30.0: fetched.append(pn) or (1, 1),
    )
    record_rss_poll_run(
        db,
        [{"reg_number": "PN-OTHER", "link": "https://example.test/other", "price_text": "200000"}],
        query_label="t",
        rss_url="https://example.test/rss",
        started_at_utc=datetime.now(timezone.utc),
        finished_at_utc=datetime.now(timezone.utc),
        ok=True,
        errors=[],
    )
    web_main._auto_fetch_notice_html_after_rss_poll()
    assert fetched == []


def test_catalog_ktru_rss_poll_rotates_codes(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path / "data")
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_catalog_ktru_codes", lambda: ["K1", "K2", "K3"])
    monkeypatch.setenv("EIS_RSS_CATALOG_KTRU_PER_POLL", "2")
    seen: list[str] = []

    class _Res:
        ok = True
        total_items = 1
        new_items = 1
        errors: list[str] = []

    def _poll(cfg, path, code):
        seen.append(code)
        return _Res()

    monkeypatch.setattr(web_main, "run_eis_rss_catalog_ktru_poll", _poll)
    web_main._catalog_ktru_rss_offset = 0
    cfg = web_main.EisRssConfig(rss_url="https://example.test/rss", query_label="t")
    web_main._run_catalog_ktru_rss_polls(cfg)
    web_main._run_catalog_ktru_rss_polls(cfg)
    assert seen == ["K1", "K2", "K3", "K1"]


def test_html_fetch_does_not_call_auto_participate(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH", "1")
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    db = analytics_db_path(tmp_path / "data")
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_html_fetch_min_price", lambda: None)
    monkeypatch.setattr(web_main, "_catalog_ktru_codes", lambda: [KTRU])
    called = {"n": 0}

    def _boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("auto participate must not run from HTML queue")

    monkeypatch.setattr(web_main, "_maybe_auto_participate_for_notice", _boom)
    monkeypatch.setattr(web_main, "_run_auto_participate_after_html", _boom)
    monkeypatch.setattr(web_main, "_notify_preoffer_watch_matches", lambda: None)
    monkeypatch.setattr(
        web_main,
        "_fetch_and_store_notice_html",
        lambda pn, url, timeout=30.0: (1, 1),
    )
    _seed_catalog_rss(
        db,
        [{"reg_number": "PN1", "link": "https://example.test/1", "price_text": "200000"}],
    )
    web_main._auto_fetch_notice_html_after_rss_poll()
    assert called["n"] == 0


def test_auto_participate_amount_floor(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "o1",
            "title": "t",
            "sku": "s",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [{"match_key": "name:параметр", "label": "Параметр", "value": "да"}],
        },
    )
    cache = meta / "ktru_rts_cache"
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "x", "version": 1, "characteristics": []})
    cookie = meta / "cookies" / "IPSokolova.txt"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    save_settings(
        settings_path(meta),
        {
            "enabled": True,
            "profiles": {
                "IPSokolova": parse_profile_rules(
                    {"enabled": True, "min_purchase_amount": 100000}
                ),
                "Nacpro": parse_profile_rules({"enabled": False}),
            },
        },
    )
    pn = "0117300004426000999"
    line = KtruPurchaseLine(
        line_key=f"{pn}|{KTRU}|line1",
        purchase_number=pn,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=KTRU,
        position_display_name="x",
        line_sum_display="120000,00",
        characteristics=[
            CustomerCharacteristic(
                match_key="name:параметр",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="Параметр",
                value_display="да",
            )
        ],
    )
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": f"https://example.test/{pn}", "price_text": "120000"}],
        query_label="t",
        rss_url="https://example.test/rss",
        started_at_utc=datetime.now(timezone.utc),
        finished_at_utc=datetime.now(timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url=f"https://example.test/{pn}",
        html_text="<html></html>",
        lines=[line],
        fetched_at_utc=datetime.now(timezone.utc),
        ok=True,
        http_status=200,
        page_context={"etp_name": "РТС-тендер"},
    )
    creates: list[str] = []

    def _create(**kwargs):
        creates.append(str(kwargs.get("purchase_number") or ""))
        return DraftResult(
            ok=True,
            already_existed=False,
            rts_draft_id=1,
            edit_url="https://example.test/e",
            reason="",
            error="",
            http_status=200,
        )

    monkeypatch.setattr(ap, "validate_cookie_session", lambda cookie: None)
    monkeypatch.setattr(ap, "load_offers_cookie", lambda path: "c")
    monkeypatch.setattr(ap, "create_pp_draft_from_notice", _create)
    monkeypatch.setattr(ap, "publish_enabled", lambda: False)

    blocked = ap.run_auto_participate_after_html(
        analytics_db=db,
        history_db=tmp_path / "h.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=preoffers,
        amount_floor=150000,
    )
    assert blocked.created == 0
    assert creates == []

    ok = ap.run_auto_participate_after_html(
        analytics_db=db,
        history_db=tmp_path / "h.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=preoffers,
        amount_floor=100000,
    )
    assert ok.created == 1
    assert creates == [pn]


def test_api_offer_matching_rows(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    db = analytics_db_path(tmp_path / "data")
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_catalog_ktru_codes", lambda: [KTRU])
    pn = "0117300004426000888"
    line = KtruPurchaseLine(
        line_key=f"{pn}|{KTRU}|line1",
        purchase_number=pn,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=KTRU,
        position_display_name="x",
        line_sum_display="200000,00",
        characteristics=[],
    )
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": f"https://example.test/{pn}"}],
        query_label="t",
        rss_url="https://example.test/rss",
        started_at_utc=datetime.now(timezone.utc),
        finished_at_utc=datetime.now(timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url=f"https://example.test/{pn}",
        html_text="<html></html>",
        lines=[line],
        fetched_at_utc=datetime.now(timezone.utc),
        ok=True,
        http_status=200,
    )
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")
    r = client.get("/api/offer-matching/rows?catalog=1")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["total_rows"] >= 1
    assert data["fingerprint"]
    assert any(KTRU in k for k in data["line_keys"])

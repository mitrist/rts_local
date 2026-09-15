from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import participation_price as pp
from fastapi.testclient import TestClient

from eis_analytics_db import record_notice_html_page, record_rss_poll_run, save_rts_pp_draft
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from eis_notice_html_parse import parse_notice_common_info_html
from rts_ktru_cache import save_cached_template
from rts_preoffers import upsert_preoffer
import rts_pp_draft_service
from web_ui import main as web_main

KTRU = "08.12.12.140-00000005"
PRICE_ORD_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "price_ord_mini.csv"


def _use_price_ord_fixture(monkeypatch) -> None:
    monkeypatch.setenv("RTS_PRICE_ORD_FILE", str(PRICE_ORD_FIXTURE))
    pp.reset_catalog_cache()


def _seed_notice(db: Path) -> tuple[str, str]:
    pn = "0117300004426000001"
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": pn,
                "link": f"https://example.test/{pn}",
                "pub_date_utc": "2026-05-21T10:00:00Z",
                "customer_name": "Заказчик",
            }
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    body = Path(".cursor/plans/common_info.html").read_text(encoding="utf-8")
    lines = parse_notice_common_info_html(body, purchase_number=pn, source_url="https://example.test/card")
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url="https://example.test/card",
        html_text=body,
        lines=lines,
        fetched_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    line = next(x for x in lines if x.ktru_code == KTRU)
    return pn, line.line_key


def _seed_preoffer(path: Path) -> None:
    upsert_preoffer(
        path,
        {
            "offer_id": "pp-offer",
            "title": "Щебень тест",
            "sku": "SKU",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [],
        },
    )


def _seed_ktru_cache(cache_dir: Path) -> None:
    save_cached_template(
        cache_dir,
        KTRU,
        {"code": KTRU, "name": "Щебень", "version": 1, "characteristics": []},
    )


def test_draft_from_notice_idempotent(monkeypatch, tmp_path):
    db = tmp_path / "analytics.sqlite3"
    preoffers = tmp_path / "preoffers.json"
    cache = tmp_path / "ktru_rts_cache"
    cookie = tmp_path / "cookies.txt"
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    _seed_preoffer(preoffers)
    _seed_ktru_cache(cache)
    pn, line_key = _seed_notice(db)

    save_rts_pp_draft(
        db,
        purchase_number=pn,
        line_key=line_key,
        ktru_code=KTRU,
        rts_draft_id=777,
        protected_id="saved",
        edit_url="https://example.test/edit/saved",
        offer_id="pp-offer",
        status="created",
    )

    monkeypatch.setattr(web_main, "_cookie_path_for_request", lambda request: cookie)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    _use_price_ord_fixture(monkeypatch)
    client = TestClient(web_main.app)

    r = client.post(
        "/api/rts/pp/draft-from-notice",
        json={"purchase_number": pn, "line_key": line_key},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["already_existed"] is True
    assert data["rts_draft_id"] == 777
    assert data["edit_url"] == "https://example.test/edit/saved"


def test_draft_from_notice_creates(monkeypatch, tmp_path):
    db = tmp_path / "analytics.sqlite3"
    preoffers = tmp_path / "preoffers.json"
    cache = tmp_path / "ktru_rts_cache"
    cookie = tmp_path / "cookies.txt"
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    _seed_preoffer(preoffers)
    _seed_ktru_cache(cache)
    pn, line_key = _seed_notice(db)

    monkeypatch.setattr(web_main, "_cookie_path_for_request", lambda request: cookie)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    monkeypatch.setattr(rts_pp_draft_service, "validate_cookie_session", lambda cookie: None)
    _use_price_ord_fixture(monkeypatch)
    captured: dict = {}

    def fake_create(cookie, **kwargs):
        captured.update(kwargs)
        return {
            "rts_draft_id": 555,
            "protected_id": "newid",
            "edit_url": "https://offers.rts-tender.ru/supplier/price-lists/positions/offer-item/edit/newid",
            "ktru_code": KTRU,
            "fill_report": {"matched": 1, "unmatched_notice": []},
            "verify_report": {"total": 1, "ok": 1, "issues": 0, "rows": []},
        }

    monkeypatch.setattr(rts_pp_draft_service, "create_and_save_pp_draft", fake_create)
    client = TestClient(web_main.app)

    r = client.post(
        "/api/rts/pp/draft-from-notice",
        json={"purchase_number": pn, "line_key": line_key},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["already_existed"] is False
    assert data["rts_draft_id"] == 555
    assert "notice_characteristics" in captured
    assert isinstance(captured["notice_characteristics"], list)
    assert captured.get("ktru_cache_dir") is not None
    assert captured.get("price_per_unit")
    assert captured["price_per_unit"] != "2 200,00"
    assert data.get("fill_report", {}).get("matched") == 1
    assert data.get("verify_report", {}).get("ok") == 1

    api = client.get(f"/api/offer-matching/{pn}?line_key={line_key}")
    assert api.status_code == 200
    assert api.json()["rts_pp_draft"]["rts_draft_id"] == 555


def test_draft_from_notice_no_cookie(monkeypatch, tmp_path):
    db = tmp_path / "analytics.sqlite3"
    preoffers = tmp_path / "preoffers.json"
    cache = tmp_path / "ktru_rts_cache"
    _seed_preoffer(preoffers)
    _seed_ktru_cache(cache)
    pn, line_key = _seed_notice(db)

    monkeypatch.setattr(web_main, "_cookie_path_for_request", lambda request: tmp_path / "missing_cookie.txt")
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    r = client.post(
        "/api/rts/pp/draft-from-notice",
        json={"purchase_number": pn, "line_key": line_key},
    )
    assert r.status_code == 503


def test_draft_from_notice_rejects_ineligible_ktru(monkeypatch, tmp_path):
    db = tmp_path / "analytics.sqlite3"
    preoffers = tmp_path / "preoffers.json"
    cookie = tmp_path / "cookies.txt"
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    pn, line_key = _seed_notice(db)

    monkeypatch.setattr(web_main, "_cookie_path_for_request", lambda request: cookie)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    r = client.post(
        "/api/rts/pp/draft-from-notice",
        json={"purchase_number": pn, "line_key": line_key},
    )
    assert r.status_code == 400
    assert "справочнике предложений" in r.json()["detail"]


def test_api_draft_verify(monkeypatch, tmp_path):
    db = tmp_path / "analytics.sqlite3"
    preoffers = tmp_path / "preoffers.json"
    cache = tmp_path / "ktru_rts_cache"
    cookie = tmp_path / "cookies.txt"
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    _seed_preoffer(preoffers)
    _seed_ktru_cache(cache)
    pn, line_key = _seed_notice(db)
    save_rts_pp_draft(
        db,
        purchase_number=pn,
        line_key=line_key,
        ktru_code=KTRU,
        rts_draft_id=888,
        status="created",
    )

    monkeypatch.setattr(web_main, "_cookie_path_for_request", lambda request: cookie)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "validate_cookie_session", lambda cookie: None)
    monkeypatch.setattr(
        web_main,
        "get_draft",
        lambda cookie, draft_id, timeout=20: {
            "ktruCharacteristics": [
                {
                    "code": "test-code",
                    "name": "Тест",
                    "characteristicValues": [{"qualityDescription": "A", "id": 1}],
                }
            ]
        },
    )
    client = TestClient(web_main.app)
    r = client.get(
        "/api/rts/pp/draft-verify",
        params={"purchase_number": pn, "line_key": line_key},
    )
    assert r.status_code == 200
    report = r.json()["verify_report"]
    assert "rows" in report
    assert "total" in report

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from eis_analytics_db import record_notice_html_page, record_rss_poll_run
from eis_notice_html_parse import parse_notice_common_info_html
from web_ui import main as web_main


def test_shelf_purchases_page_smoke(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "0117300004426000001",
                "link": "https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber=0117300004426000001",
                "pub_date_utc": "2026-05-21T10:00:00Z",
                "customer_name": "Тестовый заказчик",
                "price_text": "2316600.00",
            }
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)

    r = client.get("/shelf-purchases")

    assert r.status_code == 200
    assert "Закупки ч.12 ст.93" in r.text
    assert "0117300004426000001" in r.text
    assert "RSS-закупки и карточки ЕИС" not in r.text


def test_shelf_purchases_page_limits_rows(monkeypatch, tmp_path):
    captured = {}

    def fake_rows(path, *, limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "eis_analytics.sqlite3")
    monkeypatch.setattr(web_main, "shelf_purchase_rows", fake_rows)
    client = TestClient(web_main.app)

    r = client.get("/shelf-purchases")

    assert r.status_code == 200
    assert captured["limit"] == 30


def test_notice_html_api_not_loaded(monkeypatch, tmp_path):
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "eis_analytics.sqlite3")
    client = TestClient(web_main.app)

    r = client.get("/api/eis-notice-html/0117300004426000001")

    assert r.status_code == 200
    assert r.json()["status"] == "not_loaded"


def test_notice_html_api_loaded(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    html = (tmp_path / "common_info.html")
    html.write_text(Path(".cursor/plans/common_info.html").read_text(encoding="utf-8"), encoding="utf-8")
    body = html.read_text(encoding="utf-8")
    lines = parse_notice_common_info_html(body, purchase_number="0117300004426000001", source_url="https://example.test/card")
    record_notice_html_page(
        db,
        purchase_number="0117300004426000001",
        source_url="https://example.test/card",
        html_text=body,
        lines=lines,
        fetched_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)

    r = client.get("/api/eis-notice-html/0117300004426000001")

    data = r.json()
    assert data["status"] == "loaded"
    assert len(data["lines"]) == 1
    assert data["lines"][0]["ktru_code"] == "08.12.12.140-00000005"
    assert data["lines"][0]["characteristics"][0]["display_label"] == "Вид"

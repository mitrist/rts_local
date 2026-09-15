from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from eis_analytics_db import record_notice_html_page, record_rss_poll_run
from eis_notice_html_parse import parse_notice_common_info_html
from rts_preoffers import load_preoffers_raw
from web_ui import main as web_main


def test_preoffers_page_save_edit_disable(monkeypatch, tmp_path):
    preoffers_path = tmp_path / "rts_preoffers.json"
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers_path)
    client = TestClient(web_main.app)

    r = client.get("/preoffers")
    assert r.status_code == 200
    assert "Справочник предложений" in r.text

    r = client.post(
        "/action/preoffers-save",
        data={
            "title": "Тестовое предложение",
            "ktru_code": "08.12.12.140-00000005",
            "sku": "SKU-1",
            "rts_url": "https://example.test/offer",
            "enabled": "1",
            "characteristics_text": "Вид=Гранитный\nМарка=M1200",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    raw = load_preoffers_raw(preoffers_path)
    offer_id = raw["offers"][0]["offer_id"]
    assert raw["offers"][0]["characteristics"][0]["match_key"] == "name:вид"

    r = client.get(f"/preoffers?edit_offer_id={offer_id}")
    assert r.status_code == 200
    assert "Тестовое предложение" in r.text

    r = client.post("/action/preoffers-disable", data={"offer_id": offer_id}, follow_redirects=False)
    assert r.status_code == 303
    assert load_preoffers_raw(preoffers_path)["offers"][0]["enabled"] is False

    r = client.get("/preoffers")
    assert "preoffer-open-modal" in r.text
    assert "preoffer-modal" in r.text
    assert "Удалить" in r.text
    assert "characteristics_text" not in r.text.split("data-preoffer=")[1].split(">")[0]

    r = client.post("/action/preoffers-delete", data={"offer_id": offer_id}, follow_redirects=False)
    assert r.status_code == 303
    assert load_preoffers_raw(preoffers_path)["offers"] == []


def test_preoffers_create_from_purchase_draft(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers_path = tmp_path / "rts_preoffers.json"
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "0117300004426000001",
                "link": "https://example.test/card",
                "pub_date_utc": "2026-05-21T10:00:00Z",
            }
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    body = Path(".cursor/plans/common_info.html").read_text(encoding="utf-8")
    lines = parse_notice_common_info_html(body, purchase_number="0117300004426000001", source_url="https://example.test/card")
    record_notice_html_page(
        db,
        purchase_number="0117300004426000001",
        source_url="https://example.test/card",
        html_text=body,
        lines=lines,
        fetched_at_utc=datetime(2026, 5, 21, 10, 2, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers_path)
    client = TestClient(web_main.app)

    r = client.get(f"/preoffers?from_purchase=0117300004426000001&line_key={lines[0].line_key}")

    assert r.status_code == 200
    assert "Черновик создан из закупки" in r.text
    assert "08.12.12.140-00000005" in r.text
    assert "Вид=Гранитный" in r.text

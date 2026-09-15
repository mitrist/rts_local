"""Страница «Итоги дня»."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from eis_analytics_db import msk_day_bounds_utc, record_notice_html_page, record_rss_poll_run
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from rts_preoffers import upsert_preoffer
from web_ui import main as web_main


CATALOG_KTRU = "08.12.12.140-00000005"
OTHER_KTRU = "99.99.99.999-00000001"


def _line(*, purchase: str, ktru: str) -> KtruPurchaseLine:
    return KtruPurchaseLine(
        line_key=f"{purchase}|{ktru}|1",
        purchase_number=purchase,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=ktru,
        position_display_name="Позиция",
        line_sum_display="1000",
        characteristics=[
            CustomerCharacteristic(
                match_key="name:x",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="X",
                value_display="1",
            )
        ],
    )


def test_day_summary_page_filters_by_date_and_catalog(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "o1",
            "title": "Предложение каталога",
            "rts_url": "https://example.test/rts",
            "sku": "SKU",
            "ktru_code": CATALOG_KTRU,
            "enabled": True,
            "characteristics": [{"label": "X", "value": "1"}],
        },
    )
    day = date(2026, 5, 26)
    pub_in, _ = msk_day_bounds_utc(day)
    pub_out, _ = msk_day_bounds_utc(date(2026, 5, 25))

    for pn, ktru, pub in (
        ("PN-TODAY-OK", CATALOG_KTRU, pub_in),
        ("PN-TODAY-OTHER", OTHER_KTRU, pub_in),
        ("PN-OLD", CATALOG_KTRU, pub_out),
    ):
        record_rss_poll_run(
            db,
            [{"reg_number": pn, "link": f"https://example.test/{pn}", "pub_date_utc": pub, "customer_name": "Заказчик"}],
            query_label="test",
            rss_url="https://example.test/rss",
            started_at_utc=datetime(2026, 5, 26, 8, 0, tzinfo=timezone.utc),
            finished_at_utc=datetime(2026, 5, 26, 8, 0, tzinfo=timezone.utc),
            ok=True,
            errors=[],
        )
        record_notice_html_page(
            db,
            purchase_number=pn,
            source_url=f"https://example.test/{pn}",
            html_text="<html></html>",
            lines=[_line(purchase=pn, ktru=ktru)],
            fetched_at_utc=datetime(2026, 5, 26, 8, 1, tzinfo=timezone.utc),
            ok=True,
            http_status=200,
        )

    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    monkeypatch.setattr(web_main, "_parse_msk_date_param", lambda _raw: day)

    client = TestClient(web_main.app)
    r = client.get("/day-summary")

    assert r.status_code == 200
    assert "Итоги дня" in r.text
    assert "PN-TODAY-OK" in r.text
    assert "PN-TODAY-OTHER" not in r.text
    assert "PN-OLD" not in r.text
    assert 'name="date"' in r.text


def test_day_summary_page_opens_compare_from_query(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    monkeypatch.setattr(web_main, "_parse_msk_date_param", lambda _raw: date(2026, 5, 26))

    client = TestClient(web_main.app)
    r = client.get(
        "/day-summary",
        params={
            "date": "2026-05-26",
            "purchase": "PN-X",
            "line_key": "PN-X|K|1",
            "open_compare": "1",
        },
    )
    assert r.status_code == 200
    assert "openCompareModal" in r.text
    assert "tryDeepLinkCompare" in r.text

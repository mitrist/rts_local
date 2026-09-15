from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from eis_analytics_db import record_notice_html_page, record_rss_poll_run
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from fastapi.testclient import TestClient

from notification_settings import (
    load_settings,
    passes_min_purchase_filter,
    save_settings,
    settings_path,
    watch_row_purchase_amount,
)
from rts_preoffers import upsert_preoffer
from web_ui import main as web_main

WATCH_KTRU = "26.20.16.120-00000101"


def _watch_line(*, purchase_number: str, line_sum: str) -> KtruPurchaseLine:
    return KtruPurchaseLine(
        line_key=f"{purchase_number}|{WATCH_KTRU}|line1",
        purchase_number=purchase_number,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=WATCH_KTRU,
        position_display_name="Тестовая позиция",
        line_sum_display=line_sum,
        characteristics=[
            CustomerCharacteristic(
                match_key="name:параметр 1",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="Параметр 1",
                value_display="да",
            )
        ],
    )


def _seed_watch_preoffer(path: Path) -> None:
    upsert_preoffer(
        path,
        {
            "offer_id": "watch-offer",
            "title": "Щебень фракция 5-20",
            "rts_url": "https://example.test/rts",
            "sku": "SKU",
            "ktru_code": WATCH_KTRU,
            "enabled": True,
            "characteristics": [
                {"match_key": "name:параметр 1", "label": "Параметр 1", "value": "да"},
            ],
        },
    )


def test_watch_row_purchase_amount_prefers_line_sum():
    assert watch_row_purchase_amount({"line_sum_text": "243605,44", "price_text": "1,00"}) == 243605.44
    assert watch_row_purchase_amount({"price_text": "500000"}) == 500000.0
    assert watch_row_purchase_amount({}) is None


def test_passes_min_purchase_filter():
    row = {"line_sum_text": "100000"}
    assert passes_min_purchase_filter(row, None) is True
    assert passes_min_purchase_filter(row, 100000) is True
    assert passes_min_purchase_filter(row, 100001) is False
    assert passes_min_purchase_filter({"line_sum_text": ""}, 1) is False


def test_notification_settings_page_save_and_load(monkeypatch, tmp_path):
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)

    r = client.get("/notification-settings")
    assert r.status_code == 200
    assert "Настройка уведомлений" in r.text
    assert "Стоимость закупки от" in r.text

    r = client.post(
        "/action/notification-settings-save",
        data={"min_purchase_amount": "250 000,50"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    saved = load_settings(settings_path(tmp_path))
    assert saved["min_purchase_amount"] == 250000.5

    r = client.get("/notification-settings")
    assert "250000,5" in r.text or "250000.5" in r.text


def test_preoffer_watch_respects_min_purchase_amount(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    _seed_watch_preoffer(preoffers)
    save_settings(settings_path(tmp_path), {"min_purchase_amount": 300000})
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "PN-LOW",
                "title": "Закупка",
                "link": "https://example.test/PN-LOW",
                "customer_name": "ООО Низкая",
                "pub_date_utc": "2026-05-21T10:00:00Z",
            },
            {
                "reg_number": "PN-HIGH",
                "title": "Закупка",
                "link": "https://example.test/PN-HIGH",
                "customer_name": "ООО Высокая",
                "pub_date_utc": "2026-05-21T10:00:00Z",
            },
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number="PN-LOW",
        source_url="https://example.test/low",
        html_text="<html></html>",
        lines=[_watch_line(purchase_number="PN-LOW", line_sum="100000,00")],
        fetched_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    record_notice_html_page(
        db,
        purchase_number="PN-HIGH",
        source_url="https://example.test/high",
        html_text="<html></html>",
        lines=[_watch_line(purchase_number="PN-HIGH", line_sum="500000,00")],
        fetched_at_utc=datetime(2026, 5, 21, 10, 2, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    sent_messages: list[str] = []
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    monkeypatch.setattr(web_main, "telegram_creds_from_env_file", lambda path: ("bot", "chat"))
    monkeypatch.setattr(web_main, "max_creds_from_env_file", lambda path: ("", ""))
    monkeypatch.setattr(
        web_main,
        "send_telegram_html_long",
        lambda text, token, chat_id, timeout: sent_messages.append(text),
    )

    sent, failed = web_main._notify_preoffer_watch_matches()

    assert (sent, failed) == (1, 0)
    assert len(sent_messages) == 1
    assert "PN-HIGH" in sent_messages[0] or "ООО Высокая" in sent_messages[0]
    assert "PN-LOW" not in sent_messages[0]

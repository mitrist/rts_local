"""Тесты отправки в MAX."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import notify


def test_max_creds_from_env_file(tmp_path):
    env = tmp_path / "env.txt"
    env.write_text(
        "MAX_BOT_TOKEN=token123\nMAX_CHAT_ID=-12138541\n",
        encoding="utf-8",
    )
    token, chat_id = notify.max_creds_from_env_file(env)
    assert token == "token123"
    assert chat_id == "-12138541"


def test_send_max_posts_to_platform_api(monkeypatch):
    response = MagicMock()
    response.status_code = 200
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return response

    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify.send_max("Привет", "tok", "-99", 10.0)

    assert captured["url"] == notify.MAX_MESSAGES_URL
    assert captured["params"] == {"chat_id": "-99"}
    assert captured["headers"]["Authorization"] == "tok"
    assert captured["json"] == {"text": "Привет"}


def test_send_max_markdown_format(monkeypatch):
    response = MagicMock()
    response.status_code = 200
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return response

    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify.send_max("[link](https://example.test)", "tok", "-99", 10.0, text_format="markdown")

    assert captured["json"] == {"text": "[link](https://example.test)", "format": "markdown"}


def test_preoffer_watch_max_channel(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from eis_analytics_db import record_notice_html_page, record_rss_poll_run
    from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
    from rts_preoffers import upsert_preoffer
    from web_ui import main as web_main

    watch_ktru = "26.20.16.120-00000101"

    def watch_line(*, purchase_number: str, line_sum: str) -> KtruPurchaseLine:
        return KtruPurchaseLine(
            line_key=f"{purchase_number}|{watch_ktru}|line1",
            purchase_number=purchase_number,
            object_sid="",
            object_external_sid="",
            position_index=0,
            ktru_code=watch_ktru,
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

    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "watch-offer",
            "title": "Щебень фракция 5-20",
            "rts_url": "https://example.test/rts",
            "sku": "SKU",
            "ktru_code": watch_ktru,
            "enabled": True,
            "characteristics": [
                {"match_key": "name:параметр 1", "label": "Параметр 1", "value": "да"},
            ],
        },
    )
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "PN-MAX",
                "title": "Закупка",
                "link": "https://example.test/PN-MAX",
                "customer_name": "ООО MAX",
                "pub_date_utc": "2026-05-21T10:00:00Z",
            }
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
        purchase_number="PN-MAX",
        source_url="https://example.test/card",
        html_text="<html></html>",
        lines=[watch_line(purchase_number="PN-MAX", line_sum="100,00")],
        fetched_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
        page_context={"region": "г. Москва", "delivery_days": "10 дней"},
    )
    max_messages: list[str] = []
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    monkeypatch.setattr(web_main, "telegram_creds_from_env_file", lambda path: ("", ""))
    monkeypatch.setattr(web_main, "max_creds_from_env_file", lambda path: ("maxtok", "-1"))
    monkeypatch.setattr(
        web_main,
        "send_max_markdown_long",
        lambda text, token, chat_id, timeout: max_messages.append(text),
    )

    sent, failed = web_main._notify_preoffer_watch_matches()

    assert (sent, failed) == (1, 0)
    assert len(max_messages) == 1
    assert watch_ktru in max_messages[0]
    assert "ООО MAX" in max_messages[0]
    assert "Рентабельность, %:" in max_messages[0]
    msg = max_messages[0]
    assert "[Ссылка на ПП РТС](https://example.test/rts)" in msg
    assert (
        "[Ссылка на ПП Заказчика](https://offers.rts-tender.ru/supplier/price-lists/positions/"
        "offer-item/create?notificationNumber=PN-MAX)"
    ) in msg
    assert "[Экономика процедуры](" in msg
    assert "[Закупка на ЕИС](https://example.test/card)" in msg
    assert "Закупка:" not in msg

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from eis_analytics_db import record_notice_html_page, record_rss_poll_run
from eis_history_db import record_eis_history_poll_run
from eis_ezt_archive import NoticeMatch
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from rts_preoffers import upsert_preoffer
from web_ui import main as web_main


WATCH_KTRU = "26.20.16.120-00000101"


def _watch_line(*, purchase_number: str, line_sum: str = "1000.00") -> KtruPurchaseLine:
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


def _seed_watch_preoffer(path) -> None:
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


def _seed_history_analytics(db) -> str:
    ktru = "31.01.12.160-00000005"
    record_eis_history_poll_run(
        db,
        [
            NoticeMatch(
                purchase_number="PN1",
                ktru_codes=[ktru],
                purchase_method_codes=[],
                source_file="n.xml",
                publish_dt_in_eis="2026-05-19T08:00:00+03:00",
                customer_ktru_lines=[
                    KtruPurchaseLine(
                        line_key=f"PN1|{ktru}|line1",
                        purchase_number="PN1",
                        object_sid="100",
                        object_external_sid="",
                        position_index=0,
                        ktru_code=ktru,
                        position_display_name="Кресло",
                        characteristics=[
                            CustomerCharacteristic(
                                match_key="chr_ref_1",
                                characteristic_kind="reference",
                                characteristic_code="CHR_REF_1",
                                display_label="Материал",
                                value_display="Текстиль",
                            )
                        ],
                    )
                ],
            )
        ],
        finished_at_utc=datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc),
        org_region="72",
        document_type_44="epNotificationEZT2020",
        ktru_filter="",
        exact_dates=["2026-05-19+03:00"],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )
    return ktru


def test_analytics_page_smoke(monkeypatch, tmp_path):
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "eis_analytics.sqlite3")
    web_main._eis_rss_cron.enabled = False
    web_main._eis_rss_cron.interval_sec = 30
    client = TestClient(web_main.app)

    r = client.get("/analytics")

    assert r.status_code == 200
    assert "Извещения о закупке" in r.text
    assert "Наименование" in r.text
    assert "Товарный знак" in r.text
    assert "Мониторинг уведомлений" in r.text
    assert "Аналитический обход ЕИС" not in r.text
    assert 'id="rss_interval_sec" value="30"' in r.text
    assert "Приглашения" not in r.text
    assert "Логи опросов" not in r.text
    assert "Уведомления" not in r.text


def test_ktru_analytics_v2_redirects_to_ktru_analytics(monkeypatch, tmp_path):
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_history_db_path", lambda: tmp_path / "eis_history.sqlite3")
    client = TestClient(web_main.app)

    r = client.get("/ktru-analytics-v2?ktru=31.01.12.160-00000005&q=стул", follow_redirects=False)

    assert r.status_code == 302
    assert "/ktru-analytics" in r.headers["location"]
    assert "ktru=31.01.12.160" in r.headers["location"]


def test_ktru_analytics_page_smoke(monkeypatch, tmp_path):
    hist_db = tmp_path / "eis_history.sqlite3"
    _seed_history_analytics(hist_db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_history_db_path", lambda: hist_db)
    client = TestClient(web_main.app)

    r = client.get("/ktru-analytics")

    assert r.status_code == 200
    assert "Аналитика КТРУ" in r.text
    assert "analytics-v2-filters" in r.text
    assert 'name="date_from"' in r.text
    assert "analytics-v2-tabs" in r.text
    assert "Закупок в периоде" in r.text
    assert "analytics-v2-top-ktru" in r.text
    assert "Частые позиции из RSS/HTML" not in r.text
    assert "Аналитический обход ЕИС" not in r.text
    assert "RSS/HTML" not in r.text or "eis_history" in r.text


def test_ktru_analytics_summary_tab(monkeypatch, tmp_path):
    hist_db = tmp_path / "eis_history.sqlite3"
    _seed_history_analytics(hist_db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_history_db_path", lambda: hist_db)
    client = TestClient(web_main.app)

    r = client.get("/ktru-analytics?tab=summary&date_from=2026-05-01&date_to=2026-05-31")

    assert r.status_code == 200
    assert "analytics-v2-tabs" in r.text
    assert "Сводка" in r.text
    assert "Регионов" in r.text
    assert "Закупки по месяцам" in r.text
    assert "purchases-by-month" in r.text
    assert "Типовой профиль" not in r.text


def test_ktru_analytics_drilldown_layout(monkeypatch, tmp_path):
    hist_db = tmp_path / "eis_history.sqlite3"
    ktru = _seed_history_analytics(hist_db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_history_db_path", lambda: hist_db)
    client = TestClient(web_main.app)

    r = client.get(
        f"/ktru-analytics?date_from=2026-05-01&date_to=2026-05-31&tab=analysis&ktru={ktru}",
    )

    assert r.status_code == 200
    assert "Типовой профиль" in r.text
    assert "Частота по параметрам" in r.text
    assert "analytics-v2-compare" in r.text
    profile_pos = r.text.find("Типовой профиль")
    matrix_pos = r.text.find("data-analytics-v2-matrix")
    compare_pos = r.text.find("analytics-v2-compare")
    assert profile_pos >= 0
    assert matrix_pos >= 0
    assert profile_pos < compare_pos < matrix_pos


def test_rss_cron_start_stop_and_status(monkeypatch, tmp_path):
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "eis_analytics.sqlite3")
    web_main._eis_rss_cron.enabled = False
    web_main._eis_rss_polling_now = False
    web_main._eis_rss_next_poll_unix = None
    client = TestClient(web_main.app)
    rss_url = "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html?test=1"

    r = client.post(
        "/action/eis-rss-cron-start",
        data={
            "rss_url": rss_url,
            "rss_interval_sec": "45",
            "rss_query_label": "test_label",
        },
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert web_main._eis_rss_cron.enabled is True
    assert web_main._eis_rss_cron.interval_sec == 45
    status = client.get("/api/eis-rss-cron-status")
    assert status.status_code == 200
    assert status.json()["enabled"] is True
    assert status.json()["query_label"] == "test_label"
    saved = web_main._read_eis_rss_settings()
    assert saved["rss_url"] == rss_url
    assert saved["query_label"] == "test_label"
    assert saved["interval_sec"] == 45
    assert saved["enabled"] is True

    r = client.post("/action/eis-rss-cron-stop", follow_redirects=False)

    assert r.status_code == 303
    assert web_main._eis_rss_cron.enabled is False
    assert web_main._read_eis_rss_settings()["enabled"] is False


def test_rss_compare_rows_api_smoke(monkeypatch, tmp_path):
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "eis_analytics.sqlite3")
    client = TestClient(web_main.app)

    r = client.get("/api/eis-rss-compare-rows")

    assert r.status_code == 200
    data = r.json()
    assert data["rows"] == []
    assert "last_run" in data
    assert "status" in data


def test_rss_poll_auto_fetches_notice_html(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH", "1")
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH_DELAY_SEC", "0")

    class FakeResult:
        ok = True
        total_items = 1
        new_items = 1
        run_id = 123
        errors = []

    def fake_rss_poll(cfg, db_path):
        from datetime import datetime, timezone

        from eis_analytics_db import record_rss_poll_run

        record_rss_poll_run(
            db_path,
            [
                {
                    "reg_number": "0117300004426000001",
                    "link": "https://example.test/card",
                    "pub_date_utc": "2026-05-21T10:00:00Z",
                }
            ],
            query_label=cfg.query_label,
            rss_url=cfg.rss_url,
            started_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
            finished_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
            ok=True,
            errors=[],
        )
        return FakeResult()

    monkeypatch.setattr(web_main, "run_eis_rss_poll", fake_rss_poll)
    monkeypatch.setattr(
        web_main,
        "fetch_notice_common_info_html",
        lambda url, timeout=30.0: ("<html></html>", 200),
    )
    monkeypatch.setattr(web_main, "parse_notice_common_info_html", lambda html, purchase_number, source_url: [])

    web_main._run_eis_rss_ui_cron_poll()

    payload = web_main.notice_html_payload(db, "0117300004426000001")
    assert payload["status"] == "loaded"


def test_html_auto_fetch_processes_existing_rss_rows(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from eis_analytics_db import record_rss_poll_run

    db = tmp_path / "eis_analytics.sqlite3"
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "0117300004426000099",
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
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH", "1")
    monkeypatch.setenv("EIS_HTML_AUTO_FETCH_DELAY_SEC", "0")
    monkeypatch.setattr(
        web_main,
        "fetch_notice_common_info_html",
        lambda url, timeout=30.0: ("<html></html>", 200),
    )
    monkeypatch.setattr(web_main, "parse_notice_common_info_html", lambda html, purchase_number, source_url: [])

    web_main._auto_fetch_notice_html_after_rss_poll()

    payload = web_main.notice_html_payload(db, "0117300004426000099")
    assert payload["status"] == "loaded"


def test_preoffer_watch_telegram_after_html_only_for_catalog_ktru(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    _seed_watch_preoffer(preoffers)
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "PN-WATCH",
                "title": "Закупка",
                "link": "https://example.test/PN-WATCH",
                "customer_name": "ООО Заказчик",
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
        purchase_number="PN-WATCH",
        source_url="https://example.test/card",
        html_text="<html></html>",
        lines=[_watch_line(purchase_number="PN-WATCH", line_sum="243605,44")],
        fetched_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
        page_context={
            "region": "г. Москва",
            "delivery_days": "30 календарных дней",
        },
    )
    record_notice_html_page(
        db,
        purchase_number="PN-OTHER",
        source_url="https://example.test/other",
        html_text="<html></html>",
        lines=[
            KtruPurchaseLine(
                line_key="PN-OTHER|99.99.99.999-00000001|line1",
                purchase_number="PN-OTHER",
                object_sid="",
                object_external_sid="",
                position_index=0,
                ktru_code="99.99.99.999-00000001",
                position_display_name="Другая позиция",
                line_sum_display="1.00",
                characteristics=[],
            )
        ],
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
    sent_again, failed_again = web_main._notify_preoffer_watch_matches()

    assert (sent, failed) == (1, 0)
    assert (sent_again, failed_again) == (0, 0)
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert "Новая закупка" in msg
    assert "Наименование: Тестовая позиция" in msg
    assert "ООО Заказчик" in msg
    assert WATCH_KTRU in msg
    assert "243605,44" in msg
    assert "Рентабельность, %:" in msg
    assert "г. Москва" in msg
    assert "30 календарных дней" in msg
    assert '<a href="https://example.test/rts">Ссылка на ПП РТС</a>' in msg
    assert (
        '<a href="https://offers.rts-tender.ru/supplier/price-lists/positions/offer-item/create'
        '?notificationNumber=PN-WATCH">Ссылка на ПП Заказчика</a>'
    ) in msg
    assert "Сравнение характеристик КТРУ</a>" not in msg
    assert "Экономика процедуры</a>" in msg
    assert "tender.nacpro-web-service.ru/offer-matching?" in msg
    assert "open_compare=1" in msg
    assert "line_key=PN-WATCH" in msg
    assert "date=2026-05-21" not in msg


def test_preoffer_notify_message_includes_profitability_percent(monkeypatch):
    from pathlib import Path

    import participation_price as pp

    fixture = Path(__file__).resolve().parent / "fixtures" / "price_ord_mini.csv"
    monkeypatch.setenv("RTS_PRICE_ORD_FILE", str(fixture))
    pp.reset_catalog_cache()
    row = {
        "customer_name": "Test",
        "position_display_name": "3D-принтер FDM",
        "ktru_code": "26.20.16.120-00000104",
        "line_sum_text": "100 000,00",
        "quantity_text": "10",
        "price_per_unit_text": "10 000,00",
        "delivery_place": "34701000001 Муниципальные образования Костромской области",
        "region": "",
        "delivery_days": "10",
        "purchase_number": "PN1",
        "line_key": "lk",
        "source_url": "https://example.test/card",
    }
    msg = web_main._preoffer_notify_message(row, None, links="html")
    assert "Наименование: 3D-принтер FDM" in msg
    profit_line = next(line for line in msg.splitlines() if line.startswith("Рентабельность, %:"))
    assert profit_line != "Рентабельность, %: н/д"
    assert "%" in profit_line.split(":", 1)[1]
    assert '<a href="https://example.test/card">Закупка на ЕИС</a>' in msg
    assert "Закупка:" not in msg
    assert "Ссылка RTS:" not in msg
    assert "offer-matching" in msg
    assert "PN-OTHER" not in msg


def test_preoffer_notify_message_auto_participate_banner(monkeypatch, tmp_path):
    from eis_analytics_db import record_auto_participate_attempt

    db = tmp_path / "analytics.sqlite3"
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    record_auto_participate_attempt(
        db,
        purchase_number="PN1",
        line_key="lk",
        rts_profile="IPSokolova",
        status="created",
        edit_url="https://example.test/edit/555",
    )
    row = {
        "customer_name": "Test",
        "position_display_name": "Щебень",
        "ktru_code": "08.12.12.140-00000005",
        "line_sum_text": "500 000,00",
        "region": "Москва",
        "delivery_days": "30",
        "purchase_number": "PN1",
        "line_key": "lk",
        "source_url": "https://example.test/card",
    }
    html_msg = web_main._preoffer_notify_message(row, None, links="html")
    md_msg = web_main._preoffer_notify_message(row, None, links="markdown")
    assert html_msg.index("Авто-участие") < html_msg.index("Новая закупка")
    assert "создан черновик" in html_msg
    assert "<b>" in html_msg
    assert "🟢" in html_msg
    assert "IPSokolova" in html_msg
    assert '<a href="https://example.test/edit/555">Черновик ПП (IPSokolova)</a>' in html_msg
    assert md_msg.index("Авто-участие") < md_msg.index("Новая закупка")
    assert "**" in md_msg
    assert "[Черновик ПП (IPSokolova)](https://example.test/edit/555)" in md_msg


def test_preoffer_notify_message_without_auto_participate_has_no_banner(monkeypatch, tmp_path):
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "empty.sqlite3")
    row = {
        "customer_name": "Test",
        "position_display_name": "Щебень",
        "ktru_code": "08.12.12.140-00000005",
        "line_sum_text": "500 000,00",
        "purchase_number": "PN1",
        "line_key": "lk",
    }
    msg = web_main._preoffer_notify_message(row, None, links="html")
    assert "Авто-участие" not in msg
    assert msg.startswith("Новая закупка")

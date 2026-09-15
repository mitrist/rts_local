from __future__ import annotations

from datetime import date, datetime, timezone

from eis_analytics_db import (
    _HISTORY_ANALYTICS_MEMORY,
    _parse_rub_amount,
    characteristics_fingerprint,
    classify_shelf_confidence,
    enrich_notice_characteristics,
    history_analytics_v2_summary,
    history_dashboard_summary,
    html_analytics_summary,
    html_analytics_v2_summary,
    notice_html_fetch_candidates,
    offer_matching_ktru_rows,
    offer_watch_candidates,
    record_notice_html_page,
    record_offer_watch_notification,
    record_rss_poll_run,
    record_eis_poll_run,
    reparse_notice_html_ktru_lines,
    rss_poll_telegram_candidates,
    RSS_TELEGRAM_CHANNEL,
    RSS_TELEGRAM_LINE_KEY,
    rss_vs_getdocs_recent,
    top_ktru,
    top_ktru_characteristics,
)
from eis_ezt_archive import NoticeMatch
from eis_history_db import record_eis_history_poll_run
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from eis_notice_html_parse import parse_notice_common_info_html


def _line(*, value: str = "Текстиль") -> KtruPurchaseLine:
    return KtruPurchaseLine(
        line_key="PN1|31.01.12.160-00000005|sid:100",
        purchase_number="PN1",
        object_sid="100",
        object_external_sid="",
        position_index=0,
        ktru_code="31.01.12.160-00000005",
        position_display_name="Кресло офисное",
        price_per_unit_display="7612,67",
        line_sum_display="243605,44",
        notice_total_display="243605,44 RUB",
        customer_organization_display="Тестовый заказчик\nИНН 7707123456",
        characteristics=[
            CustomerCharacteristic(
                match_key="chr_ref_1",
                characteristic_kind="reference",
                characteristic_code="CHR_REF_1",
                display_label="Вид материала обивки сиденья",
                value_display=value,
            ),
            CustomerCharacteristic(
                match_key="name:цвет",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="Цвет",
                value_display="черный",
            ),
        ],
    )


def test_characteristics_fingerprint_is_order_stable_and_value_sensitive():
    line = _line()
    chars = line.characteristics

    h1, canon1 = characteristics_fingerprint(chars)
    h2, canon2 = characteristics_fingerprint(reversed(chars))
    h3, _ = characteristics_fingerprint(_line(value="Кожа").characteristics)

    assert h1 == h2
    assert canon1 == canon2
    assert h1 != h3


def test_classify_shelf_confidence_ezt_document():
    assert classify_shelf_confidence("epNotificationEZT2020", []) == "strong"
    assert classify_shelf_confidence("otherEZTDoc", []) == "medium"
    assert classify_shelf_confidence("epNotification", []) == "unknown"


def test_record_poll_upsert_and_top_queries(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    match = NoticeMatch(
        purchase_number="PN1",
        ktru_codes=["31.01.12.160-00000005"],
        purchase_method_codes=["METHOD1"],
        source_file="notice.xml",
        publish_dt_in_eis="2026-05-19T08:00:00+03:00",
        customer_ktru_lines=[_line()],
    )

    finished = datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc)
    run_id_1 = record_eis_poll_run(
        db,
        [match],
        finished_at_utc=finished,
        org_region="35",
        document_type_44="epNotificationEZT2020",
        ktru_filter=None,
        exact_dates=["2026-05-19+03:00"],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )
    run_id_2 = record_eis_poll_run(
        db,
        [match],
        finished_at_utc=finished,
        org_region="35",
        document_type_44="epNotificationEZT2020",
        ktru_filter=None,
        exact_dates=["2026-05-19+03:00"],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )

    assert run_id_2 > run_id_1
    top = top_ktru(db, days=3650)
    assert len(top) == 1
    assert top[0]["ktru_code"] == "31.01.12.160-00000005"
    assert top[0]["purchase_count"] == 1
    assert top[0]["line_count"] == 1

    top_chars = top_ktru_characteristics(db, days=3650)
    assert len(top_chars) == 1
    assert top_chars[0]["characteristics_count"] == 2
    assert top_chars[0]["purchase_count"] == 1


def test_html_analytics_summary_uses_parsed_notice_cards(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    record_rss_poll_run(
        db,
        [{"reg_number": "PN1", "title": "rss", "pub_date_utc": "2026-05-19T10:00:00Z"}],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number="PN1",
        source_url="https://example.test/card",
        html_text="<html></html>",
        lines=[_line()],
        fetched_at_utc=datetime(2026, 5, 19, 10, 2, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )

    summary = html_analytics_summary(db, days=3650)

    assert summary["db_exists"] is True
    assert summary["top_ktru"][0]["ktru_code"] == "31.01.12.160-00000005"
    assert summary["top_ktru"][0]["purchase_count"] == 1
    assert summary["top_characteristics"][0]["characteristics_count"] == 2
    assert summary["top_characteristics"][0]["characteristics_preview"][0]["key"] == "Вид материала обивки сиденья"


def test_parse_rub_amount_formats():
    assert _parse_rub_amount("243605,44") == 243605.44
    assert _parse_rub_amount("2 316 600,00") == 2316600.0
    assert _parse_rub_amount("125.00") == 125.0
    assert _parse_rub_amount("") is None
    assert _parse_rub_amount("n/a") is None


def _history_line_with_sum(*, pn: str, ktru: str, line_sum: str, publish: str) -> KtruPurchaseLine:
    return KtruPurchaseLine(
        line_key=f"{pn}|{ktru}|line1",
        purchase_number=pn,
        object_sid="100",
        object_external_sid="",
        position_index=0,
        ktru_code=ktru,
        position_display_name="Кресло офисное",
        line_sum_display=line_sum,
        characteristics=[],
    )


def test_history_dashboard_summary_kpi_table_and_months(tmp_path):
    db = tmp_path / "eis_history.sqlite3"
    ktru_a = "31.01.12.160-00000005"
    ktru_b = "31.01.12.160-00000006"
    finished = datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc)
    record_eis_history_poll_run(
        db,
        [
            NoticeMatch(
                purchase_number="PN1",
                ktru_codes=[ktru_a],
                purchase_method_codes=[],
                source_file="n1.xml",
                publish_dt_in_eis="2026-05-19T08:00:00+03:00",
                customer_ktru_lines=[
                    _history_line_with_sum(pn="PN1", ktru=ktru_a, line_sum="1000,00", publish="")
                ],
            ),
            NoticeMatch(
                purchase_number="PN2",
                ktru_codes=[ktru_b],
                purchase_method_codes=[],
                source_file="n2.xml",
                publish_dt_in_eis="2026-06-10T08:00:00+03:00",
                customer_ktru_lines=[
                    _history_line_with_sum(pn="PN2", ktru=ktru_b, line_sum="2000,00", publish="")
                ],
            ),
        ],
        finished_at_utc=finished,
        org_region="72",
        document_type_44="epNotificationEZT2020",
        ktru_filter="",
        exact_dates=["2026-05-19+03:00", "2026-06-10+03:00"],
        zips_downloaded=2,
        ok=True,
        errors=[],
    )

    dash = history_dashboard_summary(
        db,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 6, 30),
    )

    assert dash["db_exists"] is True
    assert dash["kpi"]["total_purchases"] == 2
    assert dash["kpi"]["unique_ktru_count"] == 2
    assert dash["kpi"]["total_sum_rub"] == 3000.0
    assert dash["kpi"]["avg_per_purchase_rub"] == 1500.0
    assert len(dash["purchases_by_month"]) >= 2
    assert len(dash["ktru_table"]) == 2
    assert dash["ktru_table_totals"]["purchase_count"] == 2
    assert dash["single_ktru"] is None


def test_history_analytics_disk_cache_reuses_snapshot(tmp_path):
    db = tmp_path / "eis_history.sqlite3"
    ktru = "31.01.12.160-00000005"
    finished = datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc)
    record_eis_history_poll_run(
        db,
        [
            NoticeMatch(
                purchase_number="PN1",
                ktru_codes=[ktru],
                purchase_method_codes=[],
                source_file="n.xml",
                publish_dt_in_eis="2026-05-19T08:00:00+03:00",
                customer_ktru_lines=[_history_line_with_sum(pn="PN1", ktru=ktru, line_sum="100,00", publish="")],
            ),
        ],
        finished_at_utc=finished,
        org_region="72",
        document_type_44="epNotificationEZT2020",
        ktru_filter="",
        exact_dates=["2026-05-19+03:00"],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )
    period = (date(2026, 5, 1), date(2026, 5, 31))
    history_analytics_v2_summary(db, date_from=period[0], date_to=period[1])
    _HISTORY_ANALYTICS_MEMORY.clear()
    second = history_analytics_v2_summary(db, date_from=period[0], date_to=period[1])
    assert second["db_exists"] is True
    assert second["kpi"]["total_purchases"] == 1
    cache_dir = tmp_path / ".history_analytics_cache"
    assert cache_dir.is_dir()
    assert any(cache_dir.glob("*.json"))


def test_history_dashboard_summary_single_ktru_filter(tmp_path):
    db = tmp_path / "eis_history.sqlite3"
    ktru = "31.01.12.160-00000005"
    finished = datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc)
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
                    _history_line_with_sum(pn="PN1", ktru=ktru, line_sum="500,00", publish="")
                ],
            ),
        ],
        finished_at_utc=finished,
        org_region="72",
        document_type_44="epNotificationEZT2020",
        ktru_filter="",
        exact_dates=["2026-05-19+03:00"],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )

    dash = history_dashboard_summary(
        db,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        ktru_code=ktru,
    )

    assert dash["kpi"]["total_purchases"] == 1
    assert dash["ktru_table"] == []
    assert dash["single_ktru"]["ktru_code"] == ktru
    assert dash["single_ktru"]["total_sum_rub"] == 500.0


def test_history_analytics_v2_summary_shares_and_drilldown(tmp_path):
    from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine

    db = tmp_path / "eis_history.sqlite3"
    ktru = "31.01.12.160-00000005"
    finished = datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc)
    for pn, value in (("PN1", "Текстиль"), ("PN2", "Кожа")):
        record_eis_history_poll_run(
            db,
            [
                NoticeMatch(
                    purchase_number=pn,
                    ktru_codes=[ktru],
                    purchase_method_codes=[],
                    source_file="n.xml",
                    publish_dt_in_eis="2026-05-19T08:00:00+03:00",
                    customer_ktru_lines=[
                        KtruPurchaseLine(
                            line_key=f"{pn}|{ktru}|line1",
                            purchase_number=pn,
                            object_sid="100",
                            object_external_sid="",
                            position_index=0,
                            ktru_code=ktru,
                            position_display_name="Кресло офисное",
                            characteristics=[
                                CustomerCharacteristic(
                                    match_key="chr_ref_1",
                                    characteristic_kind="reference",
                                    characteristic_code="CHR_REF_1",
                                    display_label="Вид материала обивки сиденья",
                                    value_display=value,
                                )
                            ],
                        )
                    ],
                )
            ],
            finished_at_utc=finished,
            org_region="72",
            document_type_44="epNotificationEZT2020",
            ktru_filter="",
            exact_dates=["2026-05-19+03:00"],
            zips_downloaded=1,
            ok=True,
            errors=[],
        )

    summary = history_analytics_v2_summary(
        db,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        ktru_code=ktru,
    )

    assert summary["db_exists"] is True
    assert summary["kpi"]["total_purchases"] == 2
    assert summary["top_ktru"][0]["purchase_share_pct"] == 100.0
    detail = summary["selected_detail"]
    assert detail is not None
    assert len(detail["top_sets"]) == 2
    matrix = detail["purchase_matrix"]
    assert matrix["purchase_count"] == 2


def test_html_analytics_v2_summary_shares_and_drilldown(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    ktru = "31.01.12.160-00000005"
    for pn, value in (("PN1", "Текстиль"), ("PN2", "Кожа")):
        record_rss_poll_run(
            db,
            [{"reg_number": pn, "title": "rss", "pub_date_utc": "2026-05-19T10:00:00Z"}],
            query_label="test",
            rss_url="https://example.test/rss",
            started_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
            finished_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
            ok=True,
            errors=[],
        )
        record_notice_html_page(
            db,
            purchase_number=pn,
            source_url=f"https://example.test/card/{pn}",
            html_text="<html></html>",
            lines=[
                KtruPurchaseLine(
                    line_key=f"{pn}|{ktru}|line1",
                    purchase_number=pn,
                    object_sid="100",
                    object_external_sid="",
                    position_index=0,
                    ktru_code=ktru,
                    position_display_name="Кресло офисное",
                    characteristics=[
                        CustomerCharacteristic(
                            match_key="chr_ref_1",
                            characteristic_kind="reference",
                            characteristic_code="CHR_REF_1",
                            display_label="Вид материала обивки сиденья",
                            value_display=value,
                        )
                    ],
                )
            ],
            fetched_at_utc=datetime(2026, 5, 19, 10, 2, tzinfo=timezone.utc),
            ok=True,
            http_status=200,
        )

    summary = html_analytics_v2_summary(db, days=3650, ktru_code=ktru)

    assert summary["db_exists"] is True
    assert summary["kpi"]["total_purchases"] == 2
    assert summary["kpi"]["total_ktru_codes"] == 1
    assert summary["top_ktru"][0]["purchase_count"] == 2
    assert summary["top_ktru"][0]["purchase_share_pct"] == 100.0
    detail = summary["selected_detail"]
    assert detail is not None
    assert detail["ktru_code"] == ktru
    assert len(detail["top_sets"]) == 2
    assert sum(row["set_share_within_ktru_pct"] for row in detail["top_sets"]) == 100.0
    matrix = detail["purchase_matrix"]
    assert matrix["purchase_count"] == 2
    assert len(matrix["columns"]) == 2
    assert matrix["columns"][0]["ordinal"] == 1
    assert len(matrix["characteristics"]) == 1
    assert set(matrix["characteristics"][0]["cells"]) == {"Текстиль", "Кожа"}
    profile = detail["typical_profile"]
    assert len(profile) == 1
    assert profile[0]["value_display"] in ("Текстиль", "Кожа")
    assert detail["top_sets"][0]["rank"] == 1
    assert detail["top_sets"][0].get("preview_short")


def test_html_typical_profile_consensus_on_majority(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    ktru = "31.01.12.160-00000005"
    for pn, value in (("PN1", "Текстиль"), ("PN2", "Текстиль"), ("PN3", "Кожа")):
        record_rss_poll_run(
            db,
            [{"reg_number": pn, "title": "rss", "pub_date_utc": "2026-05-19T10:00:00Z"}],
            query_label="test",
            rss_url="https://example.test/rss",
            started_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
            finished_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
            ok=True,
            errors=[],
        )
        record_notice_html_page(
            db,
            purchase_number=pn,
            source_url=f"https://example.test/card/{pn}",
            html_text="<html></html>",
            lines=[
                KtruPurchaseLine(
                    line_key=f"{pn}|{ktru}|line1",
                    purchase_number=pn,
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
                            display_label="Вид материала обивки сиденья",
                            value_display=value,
                        )
                    ],
                )
            ],
            fetched_at_utc=datetime(2026, 5, 19, 10, 2, tzinfo=timezone.utc),
            ok=True,
            http_status=200,
        )

    detail = html_analytics_v2_summary(db, days=3650, ktru_code=ktru)["selected_detail"]
    profile = detail["typical_profile"]
    assert len(profile) == 1
    assert profile[0]["value_display"] == "Текстиль"
    assert profile[0]["has_consensus"] is True
    assert profile[0]["value_share_within_char_pct"] == 66.7


def test_html_purchase_matrix_orders_similar_purchases_first(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    ktru = "31.01.12.160-00000005"
    specs = (
        ("PN1", "Текстиль"),
        ("PN2", "Текстиль"),
        ("PN3", "Кожа"),
    )
    for pn, value in specs:
        record_rss_poll_run(
            db,
            [{"reg_number": pn, "title": "rss", "pub_date_utc": "2026-05-19T10:00:00Z"}],
            query_label="test",
            rss_url="https://example.test/rss",
            started_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
            finished_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
            ok=True,
            errors=[],
        )
        record_notice_html_page(
            db,
            purchase_number=pn,
            source_url=f"https://example.test/card/{pn}",
            html_text="<html></html>",
            lines=[
                KtruPurchaseLine(
                    line_key=f"{pn}|{ktru}|line1",
                    purchase_number=pn,
                    object_sid="100",
                    object_external_sid="",
                    position_index=0,
                    ktru_code=ktru,
                    position_display_name="Кресло офисное",
                    characteristics=[
                        CustomerCharacteristic(
                            match_key="chr_ref_1",
                            characteristic_kind="reference",
                            characteristic_code="CHR_REF_1",
                            display_label="Вид материала обивки сиденья",
                            value_display=value,
                        )
                    ],
                )
            ],
            fetched_at_utc=datetime(2026, 5, 19, 10, 2, tzinfo=timezone.utc),
            ok=True,
            http_status=200,
        )

    summary = html_analytics_v2_summary(db, days=3650, ktru_code=ktru)
    matrix = summary["selected_detail"]["purchase_matrix"]
    assert len(matrix["columns"]) == 2
    assert matrix["columns"][0]["collapsed_count"] == 2
    assert matrix["columns"][0]["purchase_numbers"] == ["PN1", "PN2"]
    assert matrix["columns"][1]["collapsed_count"] == 1
    assert matrix["characteristics"][0]["cells"] == ["Текстиль", "Кожа"]
    assert matrix["purchases_shown"] == 3


def test_offer_watch_candidates_skip_successfully_notified_lines(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    record_notice_html_page(
        db,
        purchase_number="PN1",
        source_url="https://example.test/card",
        html_text="<html></html>",
        lines=[_line()],
        fetched_at_utc=datetime(2026, 5, 19, 10, 2, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )

    rows = offer_watch_candidates(db)
    assert len(rows) == 1
    assert rows[0]["line_key"] == "PN1|31.01.12.160-00000005|sid:100"

    record_offer_watch_notification(
        db,
        purchase_number="PN1",
        line_key=rows[0]["line_key"],
        ktru_code=rows[0]["ktru_code"],
        offer_id="offer-1",
        match_ratio=1.0,
        matched=2,
        required=2,
        channel="telegram_preoffer_watch",
        status="sent",
        sent_at_utc=datetime(2026, 5, 19, 10, 3, tzinfo=timezone.utc),
    )

    assert offer_watch_candidates(db) == []


def test_rss_poll_telegram_candidates_only_new_in_run(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    item = {
        "reg_number": "0330300204926000024",
        "title": "Закупка №0330300204926000024",
        "link": "https://example.test/?regNumber=0330300204926000024",
        "pub_date_utc": "2026-05-19T12:44:52Z",
        "stage_text": "Опубликовано",
        "customer_name": "Заказчик",
        "price_text": "30000.00",
    }
    run_id1, _ = record_rss_poll_run(
        db,
        [item],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 12, 45, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 12, 45, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    run_id2, _ = record_rss_poll_run(
        db,
        [{**item, "stage_text": "Определение поставщика завершено"}, {**item, "reg_number": "0330300204926000025"}],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 12, 50, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 12, 50, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )

    first = rss_poll_telegram_candidates(db, run_id1)
    second = rss_poll_telegram_candidates(db, run_id2)
    assert len(first) == 1
    assert first[0]["purchase_number"] == "0330300204926000024"
    assert len(second) == 1
    assert second[0]["purchase_number"] == "0330300204926000025"

    record_offer_watch_notification(
        db,
        purchase_number=second[0]["purchase_number"],
        line_key=RSS_TELEGRAM_LINE_KEY,
        ktru_code="",
        offer_id="",
        match_ratio=0.0,
        matched=0,
        required=0,
        channel=RSS_TELEGRAM_CHANNEL,
        status="sent",
        sent_at_utc=datetime(2026, 5, 19, 12, 51, tzinfo=timezone.utc),
    )
    assert rss_poll_telegram_candidates(db, run_id2) == []


def test_record_rss_poll_upsert_preserves_first_seen(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    item = {
        "reg_number": "0330300204926000024",
        "title": "Закупка №0330300204926000024",
        "link": "https://example.test/?regNumber=0330300204926000024",
        "pub_date_utc": "2026-05-19T12:44:52Z",
        "stage_text": "Опубликовано",
        "customer_name": "Заказчик",
        "price_text": "30000.00",
    }

    record_rss_poll_run(
        db,
        [item],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 12, 45, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 12, 45, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    _, new_count = record_rss_poll_run(
        db,
        [{**item, "stage_text": "Определение поставщика завершено"}],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 12, 50, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 12, 50, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )

    assert new_count == 0
    rows = rss_vs_getdocs_recent(db)
    assert rows[0]["rss_first_seen_at_utc"] == "2026-05-19T12:45:00Z"
    assert rows[0]["first_seen_poll_finished_at_utc"] == "2026-05-19T12:45:00Z"
    assert rows[0]["detection_window_start_utc"] == ""
    assert rows[0]["detection_window_end_utc"] == "2026-05-19T12:45:00Z"
    assert rows[0]["stage_text"] == "Определение поставщика завершено"


def test_rss_vs_getdocs_classes(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    rss_items = [
        {"reg_number": "PN_RSS_FAST", "title": "rss fast"},
        {"reg_number": "PN_SAME", "title": "same"},
        {"reg_number": "PN_ONLY", "title": "only rss"},
    ]
    record_rss_poll_run(
        db,
        rss_items,
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_eis_poll_run(
        db,
        [
            NoticeMatch(
                purchase_number="PN_RSS_FAST",
                ktru_codes=["31.01.12.160-00000005"],
                source_file="fast.xml",
            )
        ],
        finished_at_utc=datetime(2026, 5, 19, 10, 2, tzinfo=timezone.utc),
        org_region="35",
        document_type_44="epNotificationEZT2020",
        ktru_filter=None,
        exact_dates=[],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )
    record_eis_poll_run(
        db,
        [
            NoticeMatch(
                purchase_number="PN_SAME",
                ktru_codes=["31.01.12.160-00000005"],
                source_file="same.xml",
            )
        ],
        finished_at_utc=datetime(2026, 5, 19, 10, 0, 15, tzinfo=timezone.utc),
        org_region="35",
        document_type_44="epNotificationEZT2020",
        ktru_filter=None,
        exact_dates=[],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )

    by_number = {row["reg_number"]: row for row in rss_vs_getdocs_recent(db)}
    assert by_number["PN_RSS_FAST"]["comparison_class"] == "rss_faster"
    assert by_number["PN_SAME"]["comparison_class"] == "same_window"
    assert by_number["PN_ONLY"]["comparison_class"] == "rss_only"


def test_rss_vs_getdocs_orders_by_rss_pub_date(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "PN_OLD_SEEN_LATE",
                "title": "old pub date",
                "pub_date_utc": "2026-05-19T08:00:00Z",
            }
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "PN_NEW_SEEN_EARLY",
                "title": "new pub date",
                "pub_date_utc": "2026-05-19T11:00:00Z",
                "ikz": "263690610007635060100100090110000244",
            }
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 11, 5, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 11, 5, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )

    rows = rss_vs_getdocs_recent(db)

    assert [row["reg_number"] for row in rows[:2]] == ["PN_NEW_SEEN_EARLY", "PN_OLD_SEEN_LATE"]
    assert rows[0]["subject_code"] == "69"
    assert rows[0]["rss_detect_lag_seconds"] == 300
    assert rows[0]["detection_window_start_utc"] == "2026-05-19T09:00:00Z"
    assert rows[0]["detection_window_end_utc"] == "2026-05-19T11:05:00Z"
    assert rows[0]["is_latest_rss_new"] is True
    assert rows[1]["is_latest_rss_new"] is False


def test_rss_vs_getdocs_includes_position_and_trade_mark(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    record_rss_poll_run(
        db,
        [{"reg_number": "PN1", "title": "rss", "pub_date_utc": "2026-05-19T10:00:00Z"}],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    line = _line()
    line.characteristics.append(
        CustomerCharacteristic(
            match_key="name:товарный знак",
            characteristic_kind="manual",
            characteristic_code=None,
            display_label="Товарный знак",
            value_display="Raytec",
        )
    )
    record_notice_html_page(
        db,
        purchase_number="PN1",
        source_url="https://example.test/card",
        html_text="<html></html>",
        lines=[line],
        fetched_at_utc=datetime(2026, 5, 19, 10, 2, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )

    rows = rss_vs_getdocs_recent(db)

    assert rows[0]["position_display_name"] == "Кресло офисное"
    assert rows[0]["trade_mark"] == "Raytec"


def test_notice_html_fetch_candidates_excludes_loaded_and_optionally_retries_errors(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    record_rss_poll_run(
        db,
        [
            {"reg_number": "PN_NEEDS_HTML", "link": "https://example.test/needs"},
            {"reg_number": "PN_LOADED", "link": "https://example.test/loaded"},
            {"reg_number": "PN_ERROR", "link": "https://example.test/error"},
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number="PN_LOADED",
        source_url="https://example.test/loaded",
        html_text="<html></html>",
        lines=[],
        fetched_at_utc=datetime(2026, 5, 19, 12, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    record_notice_html_page(
        db,
        purchase_number="PN_ERROR",
        source_url="https://example.test/error",
        html_text="",
        lines=[],
        fetched_at_utc=datetime(2026, 5, 19, 12, 1, tzinfo=timezone.utc),
        ok=False,
        error_text="boom",
    )

    rows = notice_html_fetch_candidates(db, limit=10)
    rows_with_retry = notice_html_fetch_candidates(db, limit=10, retry_errors=True)

    assert [r["purchase_number"] for r in rows] == ["PN_NEEDS_HTML"]
    assert {r["purchase_number"] for r in rows_with_retry} == {"PN_NEEDS_HTML", "PN_ERROR"}


def test_enrich_notice_characteristics_overlays_xml_codes(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    match = NoticeMatch(
        purchase_number="PN_ENRICH",
        ktru_codes=["32.50.13.110-00005820"],
        purchase_method_codes=["METHOD1"],
        source_file="notice.xml",
        publish_dt_in_eis="2026-05-19T08:00:00+03:00",
        customer_ktru_lines=[
            KtruPurchaseLine(
                line_key="PN_ENRICH|32.50.13.110-00005820|sid:1",
                purchase_number="PN_ENRICH",
                object_sid="1",
                object_external_sid="",
                position_index=0,
                ktru_code="32.50.13.110-00005820",
                characteristics=[
                    CustomerCharacteristic(
                        match_key="f0d0406b-f",
                        characteristic_kind="reference",
                        characteristic_code="f0d0406b-f",
                        display_label="Длина трубки",
                        value_display="не менее 105 и не более 150 см",
                    )
                ],
            )
        ],
    )
    record_eis_poll_run(
        db,
        [match],
        finished_at_utc=datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc),
        org_region="35",
        document_type_44="epNotificationEZT2020",
        ktru_filter=None,
        exact_dates=["2026-05-19+03:00"],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )

    html_chars = [
        {
            "match_key": "name:длина трубки",
            "display_label": "Длина трубки",
            "value_display": "105-150",
            "characteristic_kind": "reference",
        }
    ]
    enriched = enrich_notice_characteristics(
        db,
        purchase_number="PN_ENRICH",
        ktru_code="32.50.13.110-00005820",
        html_chars=html_chars,
    )
    assert enriched[0]["characteristic_code"] == "f0d0406b-f"
    assert "105" in enriched[0]["value_display"]


def test_enrich_notice_characteristics_keeps_html_multi_values(tmp_path):
    """XML often has one value per characteristic; HTML rowspan rows must not be collapsed."""
    db = tmp_path / "eis_analytics.sqlite3"
    modules_code = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    match = NoticeMatch(
        purchase_number="PN_MULTI",
        ktru_codes=["26.20.11.110-00000140"],
        purchase_method_codes=["METHOD1"],
        source_file="notice.xml",
        publish_dt_in_eis="2026-05-19T08:00:00+03:00",
        customer_ktru_lines=[
            KtruPurchaseLine(
                line_key="PN_MULTI|26.20.11.110-00000140|sid:1",
                purchase_number="PN_MULTI",
                object_sid="1",
                object_external_sid="",
                position_index=0,
                ktru_code="26.20.11.110-00000140",
                characteristics=[
                    CustomerCharacteristic(
                        match_key=f"code:{modules_code}",
                        characteristic_kind="reference",
                        characteristic_code=modules_code,
                        display_label="Наличие модулей и интерфейсов",
                        value_display="M.2",
                    )
                ],
            )
        ],
    )
    record_eis_poll_run(
        db,
        [match],
        finished_at_utc=datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc),
        org_region="35",
        document_type_44="epNotificationEZT2020",
        ktru_filter=None,
        exact_dates=["2026-05-19+03:00"],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )

    html_chars = [
        {
            "match_key": "name:наличие модулей и интерфейсов",
            "display_label": "Наличие модулей и интерфейсов",
            "value_display": value,
            "characteristic_kind": "html_common_info",
        }
        for value in ("M.2", "Type-C", "Gigabit Ethernet RJ45 8P8C", "HDMI")
    ]
    enriched = enrich_notice_characteristics(
        db,
        purchase_number="PN_MULTI",
        ktru_code="26.20.11.110-00000140",
        html_chars=html_chars,
    )
    values = [row["value_display"] for row in enriched]
    assert values == ["M.2", "Type-C", "Gigabit Ethernet RJ45 8P8C", "HDMI"]
    assert all(row.get("characteristic_code") == modules_code for row in enriched)


_ROWSPAN_HTML = """
<div id="positionKTRU">
  <table class="tableBlock">
    <tbody class="tableBlock__body">
      <tr class="tableBlock__row">
        <td></td><td><a href="/epz/ktru/ktruCard/commonInfo.html">26.20.11.110-00000140</a></td>
        <td>Ноутбук</td><td>Штука</td><td>30,00</td><td>100</td><td>3000</td>
        <td><span onclick="showInfo('pos-detail-1')"></span></td>
      </tr>
      <tr class="pos-detail-1">
        <td colspan="7">
          <table><tbody>
            <tr>
              <td rowspan="4">Наличие модулей и интерфейсов</td>
              <td>M.2</td><td></td>
            </tr>
            <tr><td>Type-C</td></tr>
            <tr><td>Gigabit Ethernet RJ45 8P8C</td></tr>
            <tr><td>HDMI</td></tr>
          </tbody></table>
        </td>
      </tr>
    </tbody>
  </table>
</div>
"""


def test_offer_matching_reparses_stale_notice_html_ktru_lines(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    pn = "PN_STALE_HTML"
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": pn,
                "link": f"https://example.test/{pn}",
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
    lines = parse_notice_common_info_html(_ROWSPAN_HTML, purchase_number=pn)
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url=f"https://example.test/{pn}",
        html_text=_ROWSPAN_HTML,
        lines=lines,
        fetched_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    import json
    import sqlite3

    stale = [
        {
            "match_key": "name:наличие модулей и интерфейсов",
            "display_label": "Наличие модулей и интерфейсов",
            "value_display": "M.2",
            "characteristic_kind": "html_common_info",
        }
    ]
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            UPDATE notice_html_ktru_lines
            SET characteristics_json = ?, characteristics_count = 1
            WHERE purchase_number = ?
            """,
            (json.dumps(stale, ensure_ascii=False), pn),
        )
        conn.commit()

    rows = offer_matching_ktru_rows(db, limit=10, purchase_number=pn)
    assert len(rows) == 1
    mods = [
        c["value_display"]
        for c in rows[0]["characteristics"]
        if "модул" in str(c.get("display_label") or "").lower()
    ]
    assert mods == ["M.2", "Type-C", "Gigabit Ethernet RJ45 8P8C", "HDMI"]

    assert reparse_notice_html_ktru_lines(db, pn) == 1
    rows2 = offer_matching_ktru_rows(db, limit=10, purchase_number=pn)
    mods2 = [
        c["value_display"]
        for c in rows2[0]["characteristics"]
        if "модул" in str(c.get("display_label") or "").lower()
    ]
    assert mods2 == mods


def test_connect_sets_sqlite_busy_timeout(tmp_path):
    import sqlite3

    from eis_analytics_db import analytics_db_path, connect

    db = analytics_db_path(tmp_path)
    with connect(db, busy_timeout_ms=12_000) as conn:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
    assert row is not None
    assert int(row[0]) == 12_000


def test_connect_waits_on_concurrent_write(tmp_path):
    import threading
    import time

    from eis_analytics_db import analytics_db_path, connect

    db = analytics_db_path(tmp_path)
    barrier = threading.Barrier(2, timeout=5)
    errors: list[tuple[str, BaseException]] = []

    def writer() -> None:
        try:
            with connect(db, busy_timeout_ms=5_000) as conn:
                conn.execute("BEGIN IMMEDIATE")
                barrier.wait()
                time.sleep(0.3)
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('busy-test', '1')"
                )
                conn.commit()
        except BaseException as exc:
            errors.append(("writer", exc))

    def reader() -> None:
        try:
            barrier.wait()
            with connect(db, busy_timeout_ms=5_000) as conn:
                conn.execute("SELECT value FROM meta WHERE key = 'busy-test'").fetchone()
        except BaseException as exc:
            errors.append(("reader", exc))

    t_writer = threading.Thread(target=writer)
    t_reader = threading.Thread(target=reader)
    t_writer.start()
    t_reader.start()
    t_writer.join(timeout=10)
    t_reader.join(timeout=10)
    assert not errors, errors
    assert not t_writer.is_alive()
    assert not t_reader.is_alive()

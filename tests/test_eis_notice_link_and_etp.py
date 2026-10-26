"""Tests for EIS notice URL normalization and ETP name parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from eis_analytics_db import (
    RSS_ETP_BACKFILL_META,
    analytics_db_path,
    connect,
    list_offer_matching_etp_names,
    offer_matching_ktru_count,
    offer_matching_ktru_rows,
    record_notice_html_page,
    record_rss_poll_run,
    rss_vs_getdocs_recent,
)
from eis_ktru_characteristics import KtruPurchaseLine
from eis_notice_html_fetch import normalize_eis_notice_link
from eis_notice_html_parse import parse_notice_common_info_context


def test_normalize_eis_notice_link_canonical_from_reg():
    broken = (
        "https://zakupki.gov.ruhttps//zakupki.gov.ru/epz/order/notice/"
        "ezt20/view/common-info.html?regNumber=0342300212226000052"
    )
    assert normalize_eis_notice_link(broken, reg_number="0342300212226000052") == (
        "https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html"
        "?regNumber=0342300212226000052"
    )


def test_normalize_eis_notice_link_extracts_reg_from_mangled():
    broken = "https://zakupki.gov.ruhttps//zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber=0117300004426000001"
    assert normalize_eis_notice_link(broken).endswith("regNumber=0117300004426000001")
    assert "https://zakupki.gov.ruhttps" not in normalize_eis_notice_link(broken)


def test_normalize_eis_notice_link_relative_path():
    assert normalize_eis_notice_link("/epz/order/notice/ezt20/view/common-info.html?regNumber=1") == (
        "https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber=1"
    )


def test_parse_etp_name_from_section():
    html = """
    <html><body>
      <section class="blockInfo__section section">
        <span class="section__title">Наименование электронной площадки в информационно-телекоммуникационной сети «Интернет»</span>
        <span class="section__info">АО «Сбербанк-АСТ»</span>
      </section>
      <section class="blockInfo__section section">
        <span class="section__title">Адрес электронной площадки в информационно-телекоммуникационной сети «Интернет»</span>
        <span class="section__info"><a href="http://www.sberbank-ast.ru">http://www.sberbank-ast.ru</a></span>
      </section>
    </body></html>
    """
    ctx = parse_notice_common_info_context(html)
    assert ctx["etp_name"] == "АО «Сбербанк-АСТ»"


def test_rss_vs_getdocs_normalizes_link_and_exposes_etp(tmp_path: Path):
    db = analytics_db_path(tmp_path / "data")
    pn = "0342300212226000052"
    broken = (
        f"https://zakupki.gov.ruhttps//zakupki.gov.ru/epz/order/notice/"
        f"ezt20/view/common-info.html?regNumber={pn}"
    )
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": broken, "pub_date_utc": "2026-09-21T10:00:00Z"}],
        query_label="t",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    html = """
    <section class="blockInfo__section section">
      <span class="section__title">Наименование электронной площадки в информационно-телекоммуникационной сети «Интернет»</span>
      <span class="section__info">АО «Сбербанк-АСТ»</span>
    </section>
    """
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url=broken,
        html_text=html,
        lines=[
            KtruPurchaseLine(
                line_key=f"{pn}|x|0",
                purchase_number=pn,
                object_sid="",
                object_external_sid="",
                position_index=0,
                ktru_code="26.20.15.000-00000028",
                position_display_name="Товар",
            )
        ],
        fetched_at_utc=datetime(2026, 9, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    rows = rss_vs_getdocs_recent(db)
    assert len(rows) == 1
    assert rows[0]["link"] == (
        f"https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber={pn}"
    )
    assert rows[0]["etp_name"] == "АО «Сбербанк-АСТ»"


def test_offer_matching_filters_by_etp_name(tmp_path):
    db = analytics_db_path(tmp_path)
    pn = "0342300212226000052"
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": pn,
                "link": f"https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber={pn}",
                "pub_date_utc": "2026-09-21T10:00:00Z",
                "customer_name": "Заказчик",
                "price_text": "1000.00",
            }
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 9, 21, 10, 1, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 9, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    html = """
    <section class="blockInfo__section section">
      <span class="section__title">Наименование электронной площадки в информационно-телекоммуникационной сети «Интернет»</span>
      <span class="section__info">АО «Сбербанк-АСТ»</span>
    </section>
    """
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url=f"https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber={pn}",
        html_text=html,
        lines=[
            KtruPurchaseLine(
                line_key=f"{pn}|x|0",
                purchase_number=pn,
                object_sid="",
                object_external_sid="",
                position_index=0,
                ktru_code="26.20.15.000-00000028",
                position_display_name="Товар",
            )
        ],
        fetched_at_utc=datetime(2026, 9, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    assert list_offer_matching_etp_names(db) == ["АО «Сбербанк-АСТ»"]
    found = offer_matching_ktru_rows(db, limit=10, etp_name="АО «Сбербанк-АСТ»")
    assert len(found) == 1
    assert found[0]["etp_name"] == "АО «Сбербанк-АСТ»"
    assert offer_matching_ktru_count(db, etp_name="АО «Сбербанк-АСТ»") == 1
    assert offer_matching_ktru_rows(db, limit=10, etp_name="РТС-тендер") == []
    assert offer_matching_ktru_count(db, etp_name="РТС-тендер") == 0


def test_offer_matching_uses_rss_etp_without_html(tmp_path):
    db = analytics_db_path(tmp_path)
    pn = "0342300012026000185"
    rss_url = (
        "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html?"
        "etp_7360768=on&etp=7360768"
    )
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": pn,
                "link": f"https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber={pn}",
                "pub_date_utc": "2026-10-08T08:03:00Z",
                "customer_name": "Заказчик",
                "price_text": "12000,00",
            }
        ],
        query_label="test",
        rss_url=rss_url,
        started_at_utc=datetime(2026, 10, 8, 8, 5, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 10, 8, 8, 5, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    assert list_offer_matching_etp_names(db) == ["РТС-тендер"]
    rows = offer_matching_ktru_rows(db, limit=10, etp_name="РТС-тендер")
    assert len(rows) == 1
    assert rows[0]["purchase_number"] == pn
    assert rows[0]["etp_name"] == "РТС-тендер"
    assert rows[0]["html_status"] == "not_loaded"
    assert offer_matching_ktru_count(db, etp_name="АО «Сбербанк-АСТ»") == 0


def test_rss_etp_backfill_fills_from_poll_url_once(tmp_path):
    db = analytics_db_path(tmp_path)
    with connect(db) as conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (RSS_ETP_BACKFILL_META,))
        conn.execute(
            """
            INSERT INTO eis_rss_observations(
              reg_number, query_label, first_seen_at_utc, last_seen_at_utc, etp_name
            ) VALUES ('PN1', 'live', '2026-10-08T00:00:00Z', '2026-10-08T00:00:00Z', '')
            """
        )
        conn.execute(
            """
            INSERT INTO rss_poll_runs(
              started_at_utc, finished_at_utc, query_label, rss_url, ok
            ) VALUES (
              '2026-10-08T00:00:00Z', '2026-10-08T00:00:00Z', 'live',
              'https://zakupki.gov.ru/epz/order/extendedsearch/rss.html?etp=7360768', 1
            )
            """
        )
        conn.commit()
    with connect(db) as conn:
        etp = conn.execute(
            "SELECT etp_name FROM eis_rss_observations WHERE reg_number = 'PN1'"
        ).fetchone()[0]
        flag = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (RSS_ETP_BACKFILL_META,)
        ).fetchone()[0]
    assert etp == "РТС-тендер"
    assert flag == "1"
    with connect(db) as conn:
        conn.execute("UPDATE eis_rss_observations SET etp_name = '' WHERE reg_number = 'PN1'")
        conn.commit()
    with connect(db) as conn:
        etp = conn.execute(
            "SELECT etp_name FROM eis_rss_observations WHERE reg_number = 'PN1'"
        ).fetchone()[0]
    assert etp == ""

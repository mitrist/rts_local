"""Фильтры offer_matching для итогов дня."""

from __future__ import annotations

from datetime import date, datetime, timezone

from eis_analytics_db import (
    msk_day_bounds_utc,
    offer_matching_ktru_count,
    offer_matching_ktru_rows,
    record_notice_html_page,
    record_rss_poll_run,
)
from eis_ktru_characteristics import KtruPurchaseLine


def test_msk_day_bounds_utc():
    start, end = msk_day_bounds_utc(date(2026, 5, 26))
    assert start == "2026-05-25T21:00:00Z"
    assert end == "2026-05-26T21:00:00Z"


def test_offer_matching_catalog_and_pub_date_filter(tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    pub_from, pub_to = msk_day_bounds_utc(date(2026, 5, 26))
    record_rss_poll_run(
        db,
        [{"reg_number": "A", "link": "https://x/a", "pub_date_utc": pub_from}],
        query_label="t",
        rss_url="https://x/rss",
        started_at_utc=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number="A",
        source_url="https://x/a",
        html_text="<html></html>",
        lines=[
            KtruPurchaseLine(
                line_key="A|K1|1",
                purchase_number="A",
                object_sid="",
                object_external_sid="",
                position_index=0,
                ktru_code="K1",
                position_display_name="P",
                line_sum_display="1",
                characteristics=[],
            )
        ],
        fetched_at_utc=datetime(2026, 5, 26, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    assert offer_matching_ktru_count(
        db,
        catalog_ktru_codes=["K1"],
        pub_from_utc=pub_from,
        pub_to_utc=pub_to,
    ) == 1
    assert offer_matching_ktru_count(
        db,
        catalog_ktru_codes=["K2"],
        pub_from_utc=pub_from,
        pub_to_utc=pub_to,
    ) == 0
    rows = offer_matching_ktru_rows(
        db,
        catalog_ktru_codes=["K1"],
        pub_from_utc=pub_from,
        pub_to_utc=pub_to,
    )
    assert rows[0]["purchase_number"] == "A"

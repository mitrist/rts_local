from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from eis_analytics_db import connect, init_db
from eis_pp_manual_register import (
    ensure_notice_html_loaded,
    parse_link_date_msk,
    register_manual_eis_pp_pair,
)


def test_parse_link_date_msk():
    iso = parse_link_date_msk("16.07.2026 11:16:59")
    assert iso.endswith("Z")
    assert "08:16:59" in iso  # MSK+3 → UTC


def test_register_manual_eis_pp_pair(tmp_path: Path, monkeypatch):
    db = tmp_path / "eis_analytics.sqlite3"
    meta = tmp_path / "meta"
    meta.mkdir()
    with connect(db) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO eis_rss_observations(
              reg_number, query_label, pub_date_utc, first_seen_at_utc, last_seen_at_utc
            ) VALUES ('PN-1', 'test', '2026-07-16T08:00:00Z', '2026-07-16T08:00:00Z', '2026-07-16T08:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_pages(purchase_number, source_url, fetched_at_utc, ok, region, delivery_place)
            VALUES ('PN-1', 'http://x', '2026-07-16T08:00:00Z', 1, 'Архангельская область', '04701000001')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_ktru_lines(
              purchase_number, line_key, ktru_code, characteristics_json, parsed_at_utc
            ) VALUES ('PN-1', 'lk1', '26.20.13.000-00000002', '[]', '2026-07-16T08:00:00Z')
            """
        )
        conn.commit()

    find_item = {
        "offerPriceListItemId": "l9qMBSfomO6G",
        "tradeMark": "Test",
        "createDateTime": "2026-07-16T08:20:00Z",
        "ktru": {"code": "26.20.13.000-00000002", "name": "Test"},
        "deliveryPlaces": [{"unitPrice": "1000,0", "oktmo": {"code": "04701000001", "name": "Архангельск"}}],
    }
    monkeypatch.setenv("PP_EIS_LINK_KTRU", "26.20.13.000-00000002")
    (meta / "ktru_rts_cache").mkdir(parents=True)
    (meta / "ktru_rts_cache" / "26.20.13.000-00000002.json").write_text(
        json.dumps({"code": "26.20.13.000-00000002", "characteristics": []}),
        encoding="utf-8",
    )

    with patch("eis_pp_link.compare_notice_to_find") as cmp_mock:
        from eis_pp_link import MatchResult

        cmp_mock.return_value = MatchResult(matched_count=1, total_compared=1)
        result = register_manual_eis_pp_pair(
            db,
            meta,
            purchase_number="PN-1",
            offer_price_list_item_id="l9qMBSfomO6G",
            find_item=find_item,
            link_date_msk="16.07.2026 11:16:59",
            expected_oktmo="04701000001",
            analyst_note="найдено вручную",
        )
    assert result.offer_price_list_item_id == "l9qMBSfomO6G"
    assert result.oktmo_match is True
    assert result.pp_link_date.endswith("Z")

    with connect(db) as conn:
        row = conn.execute(
            "SELECT match_origin, analyst_status FROM pp_eis_pp_match WHERE purchase_number='PN-1'"
        ).fetchone()
    assert row[0] == "manual"
    assert row[1] == "pending"


def test_ensure_notice_html_loaded_fetches_when_missing(tmp_path: Path):
    from eis_ktru_characteristics import KtruPurchaseLine

    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO eis_rss_observations(
              reg_number, query_label, link, pub_date_utc, first_seen_at_utc, last_seen_at_utc
            ) VALUES (
              'PN-2', 'test', 'https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber=PN-2',
              '2026-07-16T08:00:00Z', '2026-07-16T08:00:00Z', '2026-07-16T08:00:00Z'
            )
            """
        )
        conn.commit()

    fake_line = KtruPurchaseLine(
        line_key="PN-2|26.20.13.000-00000002|html:0",
        purchase_number="PN-2",
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code="26.20.13.000-00000002",
    )

    with patch("eis_notice_html_fetch.fetch_notice_common_info_html", return_value=("<html/>", 200)), patch(
        "eis_notice_html_parse.parse_notice_common_info_html", return_value=[fake_line]
    ):
        loaded = ensure_notice_html_loaded(db, "PN-2")
    assert loaded == 1
    with connect(db) as conn:
        row = conn.execute(
            "SELECT line_key FROM notice_html_ktru_lines WHERE purchase_number='PN-2'"
        ).fetchone()
    assert row is not None


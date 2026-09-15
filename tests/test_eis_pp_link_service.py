from __future__ import annotations

import json
from pathlib import Path

import pytest

from eis_analytics_db import (
    ANALYTICS_SCHEMA,
    connect,
    count_pp_eis_matches,
    enrich_pp_eis_matches_with_unit_prices,
    init_db,
    list_pp_eis_matches,
    record_notice_event_journal_page,
    record_notice_supplier_results_page,
    set_pp_eis_match_status,
    upsert_pp_eis_match,
    upsert_pp_monitor_finding,
    upsert_pp_monitor_registry_row,
)
from eis_pp_link_service import match_notice_by_winner_price, recompute_matches_for_notice, recompute_matches_for_pp

JOURNAL_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "event_journal_sample.html"


def test_schema_v16_pp_eis_tables(tmp_path: Path):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
        schema = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
        cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(pp_eis_pp_match)").fetchall()
        }
    assert int(schema) == ANALYTICS_SCHEMA == 19
    assert "is_exact_match" in cols
    assert "lag_minutes" in cols
    assert "match_origin" in cols
    assert "scenario_tag" in cols
    assert "participation_intent" in cols
    with connect(db) as conn:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "notice_supplier_results_pages" in tables
    assert "notice_eis_outcome" in tables


def test_pp_eis_match_crud(tmp_path: Path):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    upsert_pp_eis_match(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        offer_price_list_item_id="oid1",
        ktru_code="26.20.13.000-00000002",
        matched_count=2,
        total_compared=3,
        diff_json="[]",
        notice_pub_date_utc="2026-07-01T12:00:00Z",
        pp_link_date="2026-07-01T12:12:30Z",
        is_exact_match=True,
        lag_minutes=12.5,
    )
    rows = list_pp_eis_matches(db, exact_only=True)
    assert len(rows) == 1
    assert rows[0]["lag_label"] == "+12.5 мин"
    assert rows[0]["is_exact_match"] is True
    assert rows[0]["notice_pub_display_msk"] == "01.07.2026 15:00:00"
    assert rows[0]["pp_link_display_msk"] == "01.07.2026 15:12:30"
    assert "zakupki.gov.ru" in rows[0]["notice_url"]
    assert rows[0]["pp_offer_url"].endswith("/customer/offers/offer-item/oid1")
    assert count_pp_eis_matches(db, exact_only=True) == 1
    assert set_pp_eis_match_status(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        offer_price_list_item_id="oid1",
        status="confirmed",
        analyst_user="tester",
        analyst_note="ok",
    )
    confirmed = list_pp_eis_matches(db, analyst_status="confirmed", exact_only=True)
    assert confirmed[0]["analyst_status"] == "confirmed"


def test_list_pp_eis_matches_sort(tmp_path: Path):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)

    def _save(pn: str, oid: str, notice: str, pp: str, lag: float) -> None:
        upsert_pp_eis_match(
            db,
            purchase_number=pn,
            line_key=f"lk-{pn}",
            offer_price_list_item_id=oid,
            ktru_code="26.20.13.000-00000002",
            matched_count=1,
            total_compared=1,
            diff_json="[]",
            notice_pub_date_utc=notice,
            pp_link_date=pp,
            is_exact_match=True,
            lag_minutes=lag,
        )

    _save("PN-A", "oid-a", "2026-07-01T10:00:00Z", "2026-07-01T11:00:00Z", 60.0)
    _save("PN-B", "oid-b", "2026-07-02T10:00:00Z", "2026-07-02T09:00:00Z", -60.0)
    _save("PN-C", "oid-c", "2026-07-03T10:00:00Z", "2026-07-03T10:30:00Z", 30.0)

    by_lag_desc = list_pp_eis_matches(db, exact_only=True, sort="lag", sort_dir="desc")
    assert [r["purchase_number"] for r in by_lag_desc] == ["PN-A", "PN-C", "PN-B"]

    by_notice_asc = list_pp_eis_matches(db, exact_only=True, sort="notice_pub", sort_dir="asc")
    assert [r["purchase_number"] for r in by_notice_asc] == ["PN-A", "PN-B", "PN-C"]

    by_pp_desc = list_pp_eis_matches(db, exact_only=True, sort="pp_link", sort_dir="desc")
    assert [r["purchase_number"] for r in by_pp_desc] == ["PN-C", "PN-B", "PN-A"]


def test_enrich_pp_eis_matches_with_unit_prices(tmp_path: Path):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO notice_html_pages(purchase_number, source_url, fetched_at_utc, ok, html_text)
            VALUES ('PN-1', 'http://x', '2026-07-01T00:00:00Z', 1, '<html></html>')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_ktru_lines(
              line_key, purchase_number, ktru_code, position_display_name, unit_text,
              quantity_text, price_per_unit_text, line_sum_text, characteristics_count,
              characteristics_json, parsed_at_utc
            ) VALUES (
              'lk1', 'PN-1', '26.20.13.000-00000002', 'x', 'шт', '1',
              '73 300,00', '73 300,00', 1, '[]', '2026-07-01T00:00:00Z'
            )
            """
        )
        conn.commit()
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="oid1",
        ktru_code="26.20.13.000-00000002",
        link_date="2026-07-01T12:00:00Z",
        unit_price=73300.0,
    )
    upsert_pp_eis_match(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        offer_price_list_item_id="oid1",
        ktru_code="26.20.13.000-00000002",
        matched_count=1,
        total_compared=1,
        diff_json="[]",
        is_exact_match=True,
    )
    rows = enrich_pp_eis_matches_with_unit_prices(db, list_pp_eis_matches(db, exact_only=True))
    assert rows[0]["notice_unit_price_text"] == "73 300,00"
    assert rows[0]["pp_unit_price_text"] == "73300,00"
    assert rows[0]["notice_line_sum_text"] == "73 300,00"


def test_recompute_matches_for_pp_exact_in_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "eis_analytics.sqlite3"
    meta = tmp_path / "meta"
    (meta / "ktru_rts_cache").mkdir(parents=True)
    ktru_code = "26.20.13.000-00000002"
    template = {
        "code": ktru_code,
        "characteristics": [
            {
                "id": 1001,
                "code": "d82d240a-8",
                "name": "Условия эксплуатации",
                "characteristicValues": [{"id": 9001, "qualityDescription": "В помещении"}],
            }
        ],
    }
    (meta / "ktru_rts_cache" / f"{ktru_code}.json").write_text(
        json.dumps(template, ensure_ascii=False),
        encoding="utf-8",
    )
    with connect(db) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO eis_rss_observations(
              reg_number, query_label, pub_date_utc, first_seen_at_utc, last_seen_at_utc
            ) VALUES ('PN-1', 'test', '2026-07-01T12:00:00Z', '2026-07-01T12:00:00Z', '2026-07-01T12:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_pages(purchase_number, source_url, fetched_at_utc, ok, html_text)
            VALUES ('PN-1', 'http://x', '2026-07-01T00:00:00Z', 1, '<html></html>')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_ktru_lines(
              line_key, purchase_number, ktru_code, position_display_name, unit_text,
              quantity_text, price_per_unit_text, line_sum_text, characteristics_count,
              characteristics_json, parsed_at_utc
            ) VALUES (?, 'PN-1', ?, 'x', 'шт', '1', '1', '1', 1, ?, '2026-07-01T00:00:00Z')
            """,
            (
                "lk1",
                ktru_code,
                json.dumps(
                    [
                        {
                            "characteristic_code": "d82d240a-8",
                            "display_label": "Условия эксплуатации",
                            "value_display": "В помещении",
                        }
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
    find_item = {"ktruCharacteristics": template["characteristics"]}
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="oid1",
        find_json=json.dumps(find_item, ensure_ascii=False),
        ktru_code=ktru_code,
        link_date="2026-07-01T12:30:00Z",
    )
    monkeypatch.setenv("RTS_META_DIR", str(meta))
    monkeypatch.setenv("PP_EIS_LINK_WINDOW_MIN", "60")
    saved = recompute_matches_for_pp(
        db,
        meta_dir=meta,
        offer_price_list_item_id="oid1",
        ktru_code=ktru_code,
        find_item=find_item,
        link_date="2026-07-01T12:30:00Z",
    )
    assert saved == 1
    rows = list_pp_eis_matches(db, purchase_number="PN-1", line_key="lk1", exact_only=True)
    assert rows
    assert rows[0]["is_exact_match"] is True
    assert rows[0]["lag_minutes"] == pytest.approx(30.0)


def test_recompute_skips_out_of_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "eis_analytics.sqlite3"
    meta = tmp_path / "meta"
    (meta / "ktru_rts_cache").mkdir(parents=True)
    ktru_code = "26.20.13.000-00000002"
    template = {
        "code": ktru_code,
        "characteristics": [
            {
                "id": 1001,
                "code": "d82d240a-8",
                "name": "Условия эксплуатации",
                "characteristicValues": [{"id": 9001, "qualityDescription": "В помещении"}],
            }
        ],
    }
    (meta / "ktru_rts_cache" / f"{ktru_code}.json").write_text(
        json.dumps(template, ensure_ascii=False),
        encoding="utf-8",
    )
    with connect(db) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO eis_rss_observations(
              reg_number, query_label, pub_date_utc, first_seen_at_utc, last_seen_at_utc
            ) VALUES ('PN-1', 'test', '2026-07-01T12:00:00Z', '2026-07-01T12:00:00Z', '2026-07-01T12:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_pages(purchase_number, source_url, fetched_at_utc, ok, html_text)
            VALUES ('PN-1', 'http://x', '2026-07-01T00:00:00Z', 1, '<html></html>')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_ktru_lines(
              line_key, purchase_number, ktru_code, position_display_name, unit_text,
              quantity_text, price_per_unit_text, line_sum_text, characteristics_count,
              characteristics_json, parsed_at_utc
            ) VALUES (?, 'PN-1', ?, 'x', 'шт', '1', '1', '1', 1, ?, '2026-07-01T00:00:00Z')
            """,
            (
                "lk1",
                ktru_code,
                json.dumps(
                    [
                        {
                            "characteristic_code": "d82d240a-8",
                            "display_label": "Условия эксплуатации",
                            "value_display": "В помещении",
                        }
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
    find_item = {"ktruCharacteristics": template["characteristics"]}
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="oid1",
        find_json=json.dumps(find_item, ensure_ascii=False),
        ktru_code=ktru_code,
        link_date="2026-07-01T14:00:00Z",
    )
    monkeypatch.setenv("RTS_META_DIR", str(meta))
    monkeypatch.setenv("PP_EIS_LINK_WINDOW_MIN", "60")
    saved = recompute_matches_for_notice(
        db,
        meta_dir=meta,
        purchase_number="PN-1",
        line_key="lk1",
        ktru_code=ktru_code,
        notice_pub_date_utc="2026-07-01T12:00:00Z",
    )
    assert saved == 0
    assert count_pp_eis_matches(db) == 0


def test_match_notice_by_winner_price(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "eis_analytics.sqlite3"
    meta = tmp_path / "meta"
    (meta / "ktru_rts_cache").mkdir(parents=True)
    ktru_code = "26.20.18.000-00000069"
    template = {
        "code": ktru_code,
        "characteristics": [
            {
                "id": 1001,
                "code": "d82d240a-8",
                "name": "Условия эксплуатации",
                "characteristicValues": [{"id": 9001, "qualityDescription": "В помещении"}],
            }
        ],
    }
    (meta / "ktru_rts_cache" / f"{ktru_code}.json").write_text(
        json.dumps(template, ensure_ascii=False),
        encoding="utf-8",
    )
    with connect(db) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO eis_rss_observations(
              reg_number, query_label, pub_date_utc, first_seen_at_utc, last_seen_at_utc
            ) VALUES ('0372200168926000028', 'test', '2026-07-15T07:45:00Z', '2026-07-15T07:45:00Z', '2026-07-15T07:45:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_pages(purchase_number, source_url, fetched_at_utc, ok, html_text)
            VALUES ('0372200168926000028', 'http://x', '2026-07-15T00:00:00Z', 1, '<html></html>')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_ktru_lines(
              line_key, purchase_number, ktru_code, position_display_name, unit_text,
              quantity_text, price_per_unit_text, line_sum_text, characteristics_count,
              characteristics_json, parsed_at_utc
            ) VALUES (?, '0372200168926000028', ?, 'x', 'шт', '6', '73300', '439800', 1, ?, '2026-07-15T00:00:00Z')
            """,
            (
                "lk-winner",
                ktru_code,
                json.dumps(
                    [
                        {
                            "characteristic_code": "d82d240a-8",
                            "display_label": "Условия эксплуатации",
                            "value_display": "В помещении",
                        }
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
    record_notice_event_journal_page(
        db,
        purchase_number="0372200168926000028",
        source_url="https://example/event-journal.html",
        html_text=JOURNAL_FIXTURE.read_text(encoding="utf-8"),
        ok=True,
    )
    record_notice_supplier_results_page(
        db,
        purchase_number="0372200168926000028",
        source_url="https://example/supplier-results.html",
        html_text="""
        <div id="supplier-def-result-participant-table-1">
        <table class="blockInfo__table tableBlock"><tbody class="tableBlock__body">
        <tr class="tableBlock__row">
        <td class="tableBlock__col">121373449</td>
        <td class="tableBlock__col">1 - Победитель</td>
        <td class="tableBlock__col">439 800,00</td>
        </tr>
        </tbody></table></div>
        """,
        ok=True,
    )
    find_item = {"ktruCharacteristics": template["characteristics"], "ktru": {"code": ktru_code}}

    class FakeSummary:
        offer_price_list_item_id = "oid-winner"
        unit_price = 73300.0
        link_date = "2026-07-15T08:00:00Z"
        trade_mark = ""
        card_url = "https://offers.rts-tender.ru/customer/offers/offer-item/oid-winner"

    def fake_search(cookie, payload, timeout=30.0):
        return [FakeSummary()], 1

    def fake_find(cookie, oid, **kwargs):
        return find_item

    monkeypatch.setenv("RTS_META_DIR", str(meta))
    monkeypatch.setenv("PP_EIS_LINK_KTRU", ktru_code)
    monkeypatch.setattr("rts_customer_pp_api.search_registry_page", fake_search)
    monkeypatch.setattr("rts_customer_pp_api.find_offer_item", fake_find)

    saved = match_notice_by_winner_price(
        db,
        meta_dir=meta,
        purchase_number="0372200168926000028",
        line_key="lk-winner",
        ktru_code=ktru_code,
        quantity_text="6",
        notice_pub_date_utc="2026-07-15T07:45:00Z",
        customer_cookie="fake-cookie",
    )
    assert saved == 1
    rows = list_pp_eis_matches(
        db,
        purchase_number="0372200168926000028",
        line_key="lk-winner",
        exact_only=True,
    )
    assert rows
    assert rows[0]["match_origin"] == "eis_winner_price"
    assert rows[0]["match_origin_label"] == "цена победителя ЕИС"
    assert rows[0]["is_exact_match"] is True

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from eis_analytics_db import connect, init_db, record_notice_event_journal_page, upsert_notice_eis_outcome
from eis_notice_event_journal_parse import EIS_OUTCOME_COMPLETED_WINNER
from eis_offer_matching_outcome import sync_offer_matching_line

JOURNAL_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "event_journal_sample.html"


def test_sync_offer_matching_line_completed_winner_triggers_search(tmp_path: Path, monkeypatch):
    db = tmp_path / "eis_analytics.sqlite3"
    meta = tmp_path / "meta"
    meta.mkdir()
    ktru = "26.20.18.000-00000069"
    with connect(db) as conn:
        init_db(conn)
    record_notice_event_journal_page(
        db,
        purchase_number="PN-1",
        source_url="https://example/journal",
        html_text=JOURNAL_FIXTURE.read_text(encoding="utf-8"),
        ok=True,
    )
    upsert_notice_eis_outcome(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        eis_outcome=EIS_OUTCOME_COMPLETED_WINNER,
        winner_offer_total=439800.0,
        notice_quantity=6.0,
        expected_pp_unit_price=73300.0,
        expected_pp_unit_price_text="73300,00",
    )
    line = {
        "purchase_number": "PN-1",
        "line_key": "lk1",
        "ktru_code": ktru,
        "html_status": "loaded",
        "quantity_text": "6",
        "characteristics": [],
        "pub_date_utc": "2026-07-15T07:45:00Z",
    }
    monkeypatch.setenv("PP_EIS_LINK_KTRU", ktru)
    with patch("eis_offer_matching_outcome.refresh_notice_eis_outcome") as refresh_mock, patch(
        "eis_pp_link_service.match_notice_by_winner_price",
        return_value=1,
    ) as match_mock:
        refresh_mock.return_value = {"eis_outcome": EIS_OUTCOME_COMPLETED_WINNER, "ok": True}
        row = sync_offer_matching_line(db, meta_dir=meta, line=line, run_pp_match=True)
    assert row["procedure_status_label"] == "Завершено, есть победитель"
    match_mock.assert_called_once()


def test_sync_offer_matching_line_skips_search_when_match_exists(tmp_path: Path, monkeypatch):
    db = tmp_path / "eis_analytics.sqlite3"
    meta = tmp_path / "meta"
    meta.mkdir()
    ktru = "26.20.18.000-00000069"
    with connect(db) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO pp_eis_pp_match(
              purchase_number, line_key, offer_price_list_item_id, ktru_code, stack_key,
              matched_count, total_compared, notice_only_count, pp_only_count, diff_json,
              notice_pub_date_utc, pp_link_date, computed_at_utc, is_exact_match, lag_minutes, match_origin
            ) VALUES ('PN-1', 'lk1', 'oid1', ?, '', 1, 1, 0, 0, '[]', '', '', '2026-01-01T00:00:00Z', 1, 0, 'eis_winner_price')
            """,
            (ktru,),
        )
        conn.commit()
    upsert_notice_eis_outcome(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        eis_outcome=EIS_OUTCOME_COMPLETED_WINNER,
    )
    line = {
        "purchase_number": "PN-1",
        "line_key": "lk1",
        "ktru_code": ktru,
        "html_status": "loaded",
        "quantity_text": "6",
        "characteristics": [],
    }
    monkeypatch.setenv("PP_EIS_LINK_KTRU", ktru)
    with patch("eis_offer_matching_outcome.refresh_notice_eis_outcome") as refresh_mock, patch(
        "eis_pp_link_service.match_notice_by_winner_price",
    ) as match_mock:
        refresh_mock.return_value = {"eis_outcome": EIS_OUTCOME_COMPLETED_WINNER, "ok": True}
        sync_offer_matching_line(db, meta_dir=meta, line=line, run_pp_match=True)
    match_mock.assert_not_called()


def test_sync_offer_matching_line_list_mode_uses_cache_only(tmp_path: Path, monkeypatch):
    db = tmp_path / "eis_analytics.sqlite3"
    meta = tmp_path / "meta"
    meta.mkdir()
    ktru = "26.20.18.000-00000069"
    with connect(db) as conn:
        init_db(conn)
    upsert_notice_eis_outcome(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        eis_outcome=EIS_OUTCOME_COMPLETED_WINNER,
        expected_pp_unit_price_text="73300,00",
    )
    line = {
        "purchase_number": "PN-1",
        "line_key": "lk1",
        "ktru_code": ktru,
        "html_status": "loaded",
        "quantity_text": "6",
        "characteristics": [],
    }
    monkeypatch.setenv("PP_EIS_LINK_KTRU", ktru)
    with patch("eis_offer_matching_outcome.refresh_notice_eis_outcome") as refresh_mock, patch(
        "eis_pp_link_service.match_notice_by_winner_price",
    ) as match_mock:
        row = sync_offer_matching_line(
            db,
            meta_dir=meta,
            line=line,
            run_pp_match=False,
            refresh_outcome=False,
        )
    refresh_mock.assert_not_called()
    match_mock.assert_not_called()
    assert row["procedure_status_label"] == "Завершено, есть победитель"


def test_pp_search_hint_when_winner_without_match(tmp_path: Path, monkeypatch):
    db = tmp_path / "eis_analytics.sqlite3"
    meta = tmp_path / "meta"
    meta.mkdir()
    ktru = "26.20.18.000-00000069"
    with connect(db) as conn:
        init_db(conn)
    upsert_notice_eis_outcome(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        eis_outcome=EIS_OUTCOME_COMPLETED_WINNER,
        expected_pp_unit_price_text="73300,00",
    )
    line = {
        "purchase_number": "PN-1",
        "line_key": "lk1",
        "ktru_code": ktru,
        "html_status": "loaded",
    }
    row = sync_offer_matching_line(
        db,
        meta_dir=meta,
        line=line,
        run_pp_match=False,
        refresh_outcome=False,
    )
    assert row["pp_match_count"] == 0
    assert row["pp_search_hint"] == ""


def test_pp_search_hint_empty_when_pp_exists(tmp_path: Path, monkeypatch):
    db = tmp_path / "eis_analytics.sqlite3"
    meta = tmp_path / "meta"
    meta.mkdir()
    ktru = "26.20.18.000-00000069"
    with connect(db) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO pp_eis_pp_match(
              purchase_number, line_key, offer_price_list_item_id, ktru_code, stack_key,
              matched_count, total_compared, notice_only_count, pp_only_count, diff_json,
              notice_pub_date_utc, pp_link_date, computed_at_utc, is_exact_match, lag_minutes, match_origin
            ) VALUES ('PN-1', 'lk1', 'oid1', ?, '', 1, 1, 0, 0, '[]', '', '', '2026-01-01T00:00:00Z', 1, 0, 'manual')
            """,
            (ktru,),
        )
        conn.commit()
    upsert_notice_eis_outcome(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        eis_outcome=EIS_OUTCOME_COMPLETED_WINNER,
    )
    line = {
        "purchase_number": "PN-1",
        "line_key": "lk1",
        "ktru_code": ktru,
        "html_status": "loaded",
    }
    row = sync_offer_matching_line(
        db,
        meta_dir=meta,
        line=line,
        run_pp_match=False,
        refresh_outcome=False,
    )
    assert row["pp_match_count"] == 1
    assert row["pp_search_hint"] == ""

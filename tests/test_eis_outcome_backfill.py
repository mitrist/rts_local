from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from eis_analytics_db import connect, init_db, record_notice_event_journal_page, upsert_notice_eis_outcome
from eis_notice_event_journal_parse import EIS_OUTCOME_COMPLETED_NO_BIDS, EIS_OUTCOME_IN_PROGRESS
from eis_pp_link_service import backfill_eis_outcomes, should_force_refresh_eis_journal


def test_should_force_refresh_eis_journal_for_stale_in_progress(tmp_path: Path):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    record_notice_event_journal_page(
        db,
        purchase_number="PN-1",
        source_url="https://example/journal",
        html_text="<html></html>",
        ok=True,
    )
    with connect(db) as conn:
        conn.execute(
            "UPDATE notice_event_journal_pages SET fetched_at_utc=? WHERE purchase_number=?",
            (old, "PN-1"),
        )
        conn.commit()
    cached = {"eis_outcome": EIS_OUTCOME_IN_PROGRESS}
    assert should_force_refresh_eis_journal(db, purchase_number="PN-1", cached_outcome=cached) is True


def test_should_not_force_refresh_terminal_outcome(tmp_path: Path):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    record_notice_event_journal_page(
        db,
        purchase_number="PN-1",
        source_url="https://example/journal",
        html_text="<html></html>",
        ok=True,
    )
    with connect(db) as conn:
        conn.execute(
            "UPDATE notice_event_journal_pages SET fetched_at_utc=? WHERE purchase_number=?",
            (old, "PN-1"),
        )
        conn.commit()
    cached = {"eis_outcome": EIS_OUTCOME_COMPLETED_NO_BIDS}
    assert should_force_refresh_eis_journal(db, purchase_number="PN-1", cached_outcome=cached) is False


def test_backfill_eis_outcomes_single_purchase(tmp_path: Path, monkeypatch):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    upsert_notice_eis_outcome(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        eis_outcome=EIS_OUTCOME_IN_PROGRESS,
    )
    notice = {
        "purchase_number": "PN-1",
        "line_key": "lk1",
        "ktru_code": "26.20.13.000-00000002",
        "quantity_text": "1",
    }
    monkeypatch.setenv("PP_EIS_LINK_KTRU", "26.20.13.000-00000002")
    with patch("eis_pp_link_service.offer_matching_ktru_rows", return_value=[notice]), patch(
        "eis_pp_link_service.refresh_notice_eis_outcome",
        return_value={"eis_outcome": EIS_OUTCOME_COMPLETED_NO_BIDS, "ok": True},
    ) as refresh_mock:
        result = backfill_eis_outcomes(db, days=7, purchase_number="PN-1", force_refresh=True)
    assert result["processed"] == 1
    assert result["updated"] == 1
    refresh_mock.assert_called_once()


def test_backfill_eis_outcomes_msk_day_window(tmp_path: Path, monkeypatch):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    monkeypatch.setenv("PP_EIS_LINK_KTRU", "26.20.13.000-00000002")
    captured: dict[str, str | None] = {}

    def _fake_rows(*_args, **kwargs):
        captured["pub_from_utc"] = kwargs.get("pub_from_utc")
        captured["pub_to_utc"] = kwargs.get("pub_to_utc")
        return []

    with patch("eis_pp_link_service.offer_matching_ktru_rows", side_effect=_fake_rows):
        from eis_analytics_db import msk_day_bounds_utc

        day = date(2026, 7, 15)
        pub_from, pub_to = msk_day_bounds_utc(day)
        result = backfill_eis_outcomes(
            db,
            pub_from_utc=pub_from,
            pub_to_utc=pub_to,
            force_refresh=True,
        )
    assert result["ok"] is True
    assert captured["pub_from_utc"] == pub_from
    assert captured["pub_to_utc"] == pub_to

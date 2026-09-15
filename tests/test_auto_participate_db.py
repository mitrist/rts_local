from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from eis_analytics_db import (
    ANALYTICS_SCHEMA,
    analytics_db_path,
    auto_participate_candidates,
    count_auto_participate_created_since,
    get_auto_participate_attempt,
    get_auto_participate_edit_url,
    list_auto_participate_attempts,
    record_auto_participate_attempt,
    record_notice_html_page,
    record_rss_poll_run,
    save_rts_pp_draft,
)
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine


def _line(*, purchase_number: str, ktru: str = "08.12.12.140-00000005") -> KtruPurchaseLine:
    return KtruPurchaseLine(
        line_key=f"{purchase_number}|{ktru}|line1",
        purchase_number=purchase_number,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=ktru,
        position_display_name="Тест",
        line_sum_display="500000,00",
        characteristics=[
            CustomerCharacteristic(
                match_key="name:параметр",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="Параметр",
                value_display="да",
            )
        ],
    )


def test_auto_participate_attempts_crud(tmp_path):
    db = analytics_db_path(tmp_path)
    assert ANALYTICS_SCHEMA >= 9
    record_auto_participate_attempt(
        db,
        purchase_number="PN1",
        line_key="PN1|K1|line1",
        rts_profile="IPSokolova",
        status="created",
        ktru_code="K1",
        rts_draft_id=42,
        edit_url="https://example.test/edit/42",
        created_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
    )
    row = get_auto_participate_attempt(db, "PN1", "PN1|K1|line1", rts_profile="IPSokolova")
    assert row is not None
    assert row["status"] == "created"
    assert row["rts_draft_id"] == 42
    assert get_auto_participate_edit_url(db, "PN1", "PN1|K1|line1", rts_profile="IPSokolova") == (
        "https://example.test/edit/42"
    )

    record_auto_participate_attempt(
        db,
        purchase_number="PN1",
        line_key="PN1|K1|line1",
        rts_profile="IPSokolova",
        status="error",
        error_text="fail",
        created_at_utc=datetime(2026, 6, 25, 11, 0, tzinfo=timezone.utc),
    )
    updated = get_auto_participate_attempt(db, "PN1", "PN1|K1|line1", rts_profile="IPSokolova")
    assert updated is not None
    assert updated["status"] == "error"
    assert updated["error_text"] == "fail"

    rows = list_auto_participate_attempts(db, limit=10, rts_profile="IPSokolova")
    assert len(rows) == 1
    assert rows[0]["purchase_number"] == "PN1"


def test_count_auto_participate_created_since(tmp_path):
    db = analytics_db_path(tmp_path)
    since = datetime(2026, 6, 25, 9, 0, tzinfo=timezone.utc)
    record_auto_participate_attempt(
        db,
        purchase_number="PN1",
        line_key="k1",
        rts_profile="Nacpro",
        status="created",
        created_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
    )
    record_auto_participate_attempt(
        db,
        purchase_number="PN2",
        line_key="k2",
        rts_profile="Nacpro",
        status="error",
        created_at_utc=datetime(2026, 6, 25, 10, 5, tzinfo=timezone.utc),
    )
    assert count_auto_participate_created_since(db, rts_profile="Nacpro", since_utc=since) == 1


def test_auto_participate_candidates_excludes_attempts_and_drafts(tmp_path):
    db = analytics_db_path(tmp_path)
    pn = "0117300004426000001"
    ktru = "08.12.12.140-00000005"
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": f"https://example.test/{pn}", "pub_date_utc": "2026-06-25T10:00:00Z"}],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url="https://example.test/card",
        html_text="<html></html>",
        lines=[_line(purchase_number=pn, ktru=ktru)],
        fetched_at_utc=datetime(2026, 6, 25, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    candidates = auto_participate_candidates(db, limit=10, rts_profile="IPSokolova")
    assert len(candidates) == 1
    assert candidates[0]["purchase_number"] == pn

    record_auto_participate_attempt(
        db,
        purchase_number=pn,
        line_key=candidates[0]["line_key"],
        rts_profile="IPSokolova",
        status="created",
    )
    assert auto_participate_candidates(db, limit=10, rts_profile="IPSokolova") == []

    save_rts_pp_draft(
        db,
        purchase_number=pn,
        line_key=candidates[0]["line_key"],
        ktru_code=ktru,
        rts_draft_id=99,
        status="created",
        rts_profile="Nacpro",
    )
    candidates2 = auto_participate_candidates(db, limit=10, rts_profile="Nacpro")
    assert candidates2 == []

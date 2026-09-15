from __future__ import annotations

from pathlib import Path

import pytest

from eis_analytics_db import connect, init_db, record_notice_event_journal_page
from eis_pp_event_chain import build_pp_eis_event_chain

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "event_journal_sample.html"


def test_build_pp_eis_event_chain_merged_timeline(tmp_path: Path):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    record_notice_event_journal_page(
        db,
        purchase_number="0372200168926000028",
        source_url="https://example/event-journal.html",
        html_text=FIXTURE.read_text(encoding="utf-8"),
        ok=True,
    )
    match = {
        "purchase_number": "0372200168926000028",
        "notice_pub_date_utc": "2026-07-15T07:44:58Z",
        "pp_link_date": "2026-07-15T08:20:29Z",
    }
    chain = build_pp_eis_event_chain(db, match)
    rows = chain["rows"]
    assert chain["journal_count"] == 5
    labels = [r["label"] for r in rows]
    assert labels[0] == "Публикация извещения"
    assert any("Извещение о закупке" in x for x in labels)
    assert labels[-1] == "Публикация ПП"
    assert rows[0]["delta_label"] == "—"
    assert rows[1]["delta_minutes"] == pytest.approx(0.03, abs=0.2)

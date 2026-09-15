"""Test event chain timezone normalization for notice 0369300380626000001."""
from __future__ import annotations

from eis_notice_event_journal_parse import parse_notice_event_journal_html
from eis_pp_event_chain import build_pp_eis_event_chain
from eis_pp_link import parse_utc_datetime


def test_journal_msk_plus2_aligns_with_notice_pub():
    html = """
    <div id="event-journal-wrapper"><table id="event"><tbody>
      <tr><td>08.07.2026 14:54 <span class='timeZoneName'>(МСК+2)</span></td><td>Размещено извещение</td></tr>
      <tr><td>08.07.2026 14:55 <span class='timeZoneName'>(МСК+2)</span></td><td>Другой этап</td></tr>
    </tbody></table></div>
    """
    rows = parse_notice_event_journal_html(html)
    notice_pub = "2026-07-08T09:54:19Z"
    notice_dt = parse_utc_datetime(notice_pub)
    journal_dt = parse_utc_datetime(rows[0]["occurred_at_utc"])
    assert notice_dt is not None and journal_dt is not None
    lag_min = (journal_dt - notice_dt).total_seconds() / 60.0
    assert abs(lag_min) < 1.0


def test_build_chain_unified_msk_display(tmp_path):
    from eis_analytics_db import connect, init_db, record_notice_event_journal_page

    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    html = """
    <div id="event-journal-wrapper"><table id="event"><tbody>
      <tr><td>08.07.2026 14:54 <span class='timeZoneName'>(МСК+2)</span></td><td>Размещено извещение</td></tr>
    </tbody></table></div>
    """
    record_notice_event_journal_page(
        db,
        purchase_number="0369300380626000001",
        source_url="https://example/journal",
        html_text=html,
        ok=True,
    )
    match = {
        "purchase_number": "0369300380626000001",
        "notice_pub_date_utc": "2026-07-08T09:54:19Z",
        "pp_link_date": "2026-07-08T10:53:56.245",
    }
    chain = build_pp_eis_event_chain(db, match)
    displays = [r["occurred_display_msk"] for r in chain["rows"]]
    assert len(displays) == 3
    assert all(d.count(":") == 2 for d in displays)
    assert displays[0] == "08.07.2026 12:54:00"
    assert displays[1] == "08.07.2026 12:54:19"
    assert displays[-1] == "08.07.2026 13:53:56"

from __future__ import annotations

from pathlib import Path

from eis_notice_event_journal_parse import parse_notice_event_journal_html

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "event_journal_sample.html"


def test_parse_notice_event_journal_html_sample():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_notice_event_journal_html(html)
    assert len(rows) == 5
    assert rows[0]["occurred_display_msk"].startswith("15.07.2026 10:45")
    assert any("Извещение о закупке" in r["description"] for r in rows)
    assert rows[-1]["occurred_display_msk"].startswith("15.07.2026 11:06")


def test_parse_notice_event_journal_html_empty():
    assert parse_notice_event_journal_html("") == []


def test_parse_notice_event_journal_msk_plus2():
    html = """
    <div id="event-journal-wrapper">
      <table id="event"><tbody>
        <tr>
          <td>08.07.2026 14:54 <span class='timeZoneName' title='Екатеринбург, стандартное время'>(МСК+2)</span></td>
          <td>Размещено извещение</td>
        </tr>
      </tbody></table>
    </div>
    """
    rows = parse_notice_event_journal_html(html)
    assert len(rows) == 1
    assert rows[0]["occurred_at_utc"] == "2026-07-08T09:54:00Z"
    assert rows[0]["occurred_display_msk"] == "08.07.2026 12:54:00"


def test_procedure_status_no_applications():
    from eis_notice_event_journal_parse import (
        EIS_OUTCOME_COMPLETED_NO_BIDS,
        eis_outcome_from_journal_rows,
        procedure_status_from_journal_rows,
    )

    rows = [
        {"description": "Размещено извещение", "occurred_at_utc": "2026-07-08T09:00:00Z"},
        {
            "description": (
                "Размещен «Уведомление об отсутствии заявок участников от 10.07.2026 №ОЗТ1» "
                "для закупки №0334300178926000001"
            ),
            "occurred_at_utc": "2026-07-10T12:00:00Z",
        },
    ]
    assert procedure_status_from_journal_rows(rows) == "Завершено, нет предложений"
    assert eis_outcome_from_journal_rows(rows) == EIS_OUTCOME_COMPLETED_NO_BIDS
    assert procedure_status_from_journal_rows(rows[:1]) == "В работе"


def test_eis_outcome_completed_winner_from_sample():
    from eis_notice_event_journal_parse import (
        EIS_OUTCOME_COMPLETED_WINNER,
        eis_outcome_from_journal_rows,
        has_results_protocol,
        procedure_status_from_journal_rows,
    )

    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_notice_event_journal_html(html)
    assert has_results_protocol(rows)
    assert eis_outcome_from_journal_rows(rows) == EIS_OUTCOME_COMPLETED_WINNER
    assert procedure_status_from_journal_rows(rows) == "Завершено, есть победитель"

"""Parse EIS notice event-journal HTML."""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any

from bs4 import BeautifulSoup, Tag

from eis_changelog import MSK, dt_to_msk_clean

_WS_RE = re.compile(r"\s+")
_EVENT_DT_RE = re.compile(
    r"(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}(?::\d{2})?)",
)
_MSK_OFFSET_RE = re.compile(r"МСК\s*([+-]?\d+)?", re.IGNORECASE)


def _clean_text(value: object) -> str:
    return _WS_RE.sub(" ", str(value or "").replace("\xa0", " ").strip())


def _timezone_from_eis_label(label: str, title: str = "") -> timezone:
    """Map EIS journal labels like (МСК+2) to fixed UTC offset."""
    text = _clean_text(label).upper().replace(" ", "")
    m = _MSK_OFFSET_RE.search(text)
    if m:
        raw = m.group(1)
        delta = int(raw) if raw not in (None, "", "+", "-") else 0
        hours = 3 + delta
        return timezone(timedelta(hours=hours), f"UTC{hours:+d}")

    title_l = _clean_text(title).lower()
    title_map = (
        ("калининград", 2),
        ("самар", 4),
        ("екатеринбург", 5),
        ("омск", 6),
        ("красноярск", 7),
        ("иркутск", 8),
        ("якутск", 9),
        ("владивосток", 10),
        ("магадан", 11),
        ("камчат", 12),
    )
    for needle, hours in title_map:
        if needle in title_l:
            return timezone(timedelta(hours=hours), f"UTC{hours:+d}")
    return MSK


def _parse_event_datetime(raw: str, tz: timezone) -> datetime | None:
    text = _clean_text(raw)
    if not text:
        return None
    m = _EVENT_DT_RE.search(text)
    if not m:
        return None
    token = m.group(1)
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(token, fmt)
            return dt.replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _display_msk(dt: datetime) -> str:
    return dt_to_msk_clean(dt)


def _timezone_from_cell(dt_cell: Tag) -> timezone:
    span = dt_cell.find("span", class_="timeZoneName")
    if isinstance(span, Tag):
        label = span.get_text(" ", strip=True)
        title = str(span.get("title") or "")
        return _timezone_from_eis_label(label, title)
    return MSK


def parse_notice_event_journal_html(html: str) -> list[dict[str, Any]]:
    """Return journal rows oldest-first: occurred_at_utc, occurred_display_msk, description."""
    root = BeautifulSoup(html or "", "html.parser")
    wrapper = root.find(id="event-journal-wrapper")
    if not isinstance(wrapper, Tag):
        return []
    table = wrapper.find("table", id="event")
    if not isinstance(table, Tag):
        table = wrapper.find("table")
    if not isinstance(table, Tag):
        return []
    tbody = table.find("tbody")
    if not isinstance(tbody, Tag):
        return []
    rows: list[dict[str, Any]] = []
    for tr in tbody.find_all("tr", recursive=False):
        if not isinstance(tr, Tag):
            continue
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        dt_cell = cells[0]
        desc_cell = cells[1]
        tz = _timezone_from_cell(dt_cell)
        for span in dt_cell.find_all("span", class_="timeZoneName"):
            span.decompose()
        dt = _parse_event_datetime(dt_cell.get_text(" ", strip=True), tz)
        description = _clean_text(desc_cell.get_text(" ", strip=True))
        if dt is None or not description:
            continue
        rows.append(
            {
                "occurred_at_utc": _utc_iso(dt),
                "occurred_display_msk": _display_msk(dt),
                "description": description,
            }
        )
    rows.sort(key=lambda x: (x["occurred_at_utc"], x["description"]))
    return rows


NO_APPLICATIONS_MARKER = "уведомление об отсутствии заявок участников"
RESULTS_PROTOCOL_MARKER = "протокол подведения итогов определения поставщика"

NO_APPLICATIONS_STATUS_LABEL = "Завершено, нет предложений"
COMPLETED_WINNER_STATUS_LABEL = "Завершено, есть победитель"
IN_PROGRESS_STATUS_LABEL = "В работе"

EIS_OUTCOME_COMPLETED_NO_BIDS = "completed_no_bids"
EIS_OUTCOME_COMPLETED_WINNER = "completed_winner"
EIS_OUTCOME_IN_PROGRESS = "in_progress"
EIS_OUTCOME_UNKNOWN = "unknown"

_EIS_OUTCOME_LABELS = {
    EIS_OUTCOME_COMPLETED_NO_BIDS: NO_APPLICATIONS_STATUS_LABEL,
    EIS_OUTCOME_COMPLETED_WINNER: COMPLETED_WINNER_STATUS_LABEL,
    EIS_OUTCOME_IN_PROGRESS: IN_PROGRESS_STATUS_LABEL,
}


def journal_final_description(rows: list[dict[str, Any]]) -> str:
    """Latest journal event description (rows are oldest-first)."""
    if not rows:
        return ""
    return str(rows[-1].get("description") or "").strip()


def _description_has_no_applications(description: str) -> bool:
    return NO_APPLICATIONS_MARKER in _clean_text(description).lower()


def _description_has_results_protocol(description: str) -> bool:
    text = _clean_text(description).lower()
    return "размещен" in text and RESULTS_PROTOCOL_MARKER in text


def has_results_protocol(rows: list[dict[str, Any]]) -> bool:
    """True if any journal row mentions the final supplier-selection protocol."""
    return any(_description_has_results_protocol(str(row.get("description") or "")) for row in rows)


def eis_outcome_from_journal_rows(rows: list[dict[str, Any]]) -> str:
    """Derive procedure outcome from journal rows (newest event wins for terminal states)."""
    if not rows:
        return EIS_OUTCOME_UNKNOWN
    final = journal_final_description(rows)
    if _description_has_no_applications(final):
        return EIS_OUTCOME_COMPLETED_NO_BIDS
    if _description_has_results_protocol(final):
        return EIS_OUTCOME_COMPLETED_WINNER
    if has_results_protocol(rows):
        return EIS_OUTCOME_COMPLETED_WINNER
    return EIS_OUTCOME_IN_PROGRESS


def eis_outcome_label(outcome: str) -> str:
    return _EIS_OUTCOME_LABELS.get(str(outcome or "").strip(), "")


def procedure_status_from_journal_rows(rows: list[dict[str, Any]]) -> str:
    """Derive list-page procedure status label from journal rows."""
    return eis_outcome_label(eis_outcome_from_journal_rows(rows))


def procedure_status_from_journal_html(html: str) -> str:
    return procedure_status_from_journal_rows(parse_notice_event_journal_html(html))

"""Build merged EIS↔PP event chronology for analyst review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eis_analytics_db import get_notice_eis_outcome, get_or_fetch_notice_event_journal
from eis_changelog import MSK, dt_to_msk_clean
from eis_notice_event_journal_fetch import notice_event_journal_url
from eis_notice_event_journal_parse import (
    eis_outcome_label,
    parse_notice_event_journal_html,
    procedure_status_from_journal_rows,
)
from eis_pp_link import format_lag_minutes, parse_utc_datetime
from eis_supplier_results_fetch import notice_supplier_results_url
from participation_price import format_money_ru


@dataclass
class EventChainRow:
    kind: str
    label: str
    occurred_at_utc: str
    occurred_display_msk: str
    delta_minutes: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "occurred_at_utc": self.occurred_at_utc,
            "occurred_display_msk": self.occurred_display_msk,
            "delta_minutes": self.delta_minutes,
            "delta_label": format_lag_minutes(self.delta_minutes) if self.delta_minutes is not None else "—",
        }


def _display_msk_from_raw(raw: str) -> tuple[str, str]:
    dt = parse_utc_datetime(raw)
    if dt is None:
        from eis_analytics_db import _parse_compare_datetime_utc

        cmp_dt = _parse_compare_datetime_utc(raw)
        if cmp_dt is not None:
            dt = cmp_dt.astimezone(timezone.utc)
    if dt is None:
        return "", ""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"), dt_to_msk_clean(
        dt
    )


def _chain_row(
    *,
    kind: str,
    label: str,
    occurred_at_utc: str,
    occurred_display_msk: str,
) -> EventChainRow:
    return EventChainRow(
        kind=kind,
        label=label,
        occurred_at_utc=occurred_at_utc,
        occurred_display_msk=occurred_display_msk,
    )


def build_pp_eis_event_chain(
    db_path: Path,
    match: dict[str, Any],
    *,
    fetch_timeout: float = 30.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    purchase_number = str(match.get("purchase_number") or "").strip()
    entries: list[EventChainRow] = []
    journal_error = ""

    notice_utc, notice_msk = _display_msk_from_raw(str(match.get("notice_pub_date_utc") or ""))
    if notice_utc and notice_msk:
        entries.append(
            _chain_row(
                kind="notice_pub",
                label="Публикация извещения",
                occurred_at_utc=notice_utc,
                occurred_display_msk=notice_msk,
            )
        )

    journal_rows: list[dict[str, Any]] = []
    if purchase_number:
        try:
            payload = get_or_fetch_notice_event_journal(
                db_path,
                purchase_number=purchase_number,
                source_url=notice_event_journal_url(purchase_number),
                timeout=fetch_timeout,
                force_refresh=force_refresh,
            )
            if payload.get("ok"):
                journal_rows = parse_notice_event_journal_html(str(payload.get("html_text") or ""))
            else:
                journal_error = str(payload.get("error_text") or "journal_fetch_failed")
        except Exception as exc:
            journal_error = str(exc)

    for row in journal_rows:
        entries.append(
            _chain_row(
                kind="eis_journal",
                label=str(row.get("description") or ""),
                occurred_at_utc=str(row.get("occurred_at_utc") or ""),
                occurred_display_msk=str(row.get("occurred_display_msk") or ""),
            )
        )

    pp_utc, pp_msk = _display_msk_from_raw(str(match.get("pp_link_date") or ""))
    if pp_utc and pp_msk:
        entries.append(
            _chain_row(
                kind="pp_pub",
                label="Публикация ПП",
                occurred_at_utc=pp_utc,
                occurred_display_msk=pp_msk,
            )
        )

    entries = [e for e in entries if e.occurred_at_utc]
    entries.sort(key=lambda e: (e.occurred_at_utc, e.kind, e.label))

    prev_dt: datetime | None = None
    out_rows: list[dict[str, Any]] = []
    for entry in entries:
        cur_dt = parse_utc_datetime(entry.occurred_at_utc)
        delta: float | None = None
        if prev_dt is not None and cur_dt is not None:
            delta = (cur_dt - prev_dt).total_seconds() / 60.0
        if cur_dt is not None:
            prev_dt = cur_dt
        item = entry.as_dict()
        if cur_dt is not None:
            item["occurred_display_msk"] = dt_to_msk_clean(cur_dt)
        item["delta_minutes"] = delta
        item["delta_label"] = format_lag_minutes(delta) if delta is not None else "—"
        out_rows.append(item)

    return {
        "rows": out_rows,
        "journal_url": notice_event_journal_url(purchase_number) if purchase_number else "",
        "supplier_results_url": notice_supplier_results_url(purchase_number) if purchase_number else "",
        "journal_count": len(journal_rows),
        "journal_error": journal_error,
        "procedure_status_label": procedure_status_from_journal_rows(journal_rows),
    }


def _enrich_outcome_fields(row: dict[str, Any], outcome_row: dict[str, Any] | None) -> None:
    if not outcome_row:
        row.setdefault("eis_outcome", "")
        row.setdefault("eis_outcome_label", row.get("procedure_status_label") or "")
        return
    outcome = str(outcome_row.get("eis_outcome") or "")
    row["eis_outcome"] = outcome
    row["eis_outcome_label"] = eis_outcome_label(outcome) or str(row.get("procedure_status_label") or "")
    w = outcome_row.get("winner_offer_total")
    q = outcome_row.get("notice_quantity")
    unit_text = str(outcome_row.get("expected_pp_unit_price_text") or "")
    row["winner_participant_id"] = str(outcome_row.get("winner_participant_id") or "")
    row["expected_pp_unit_price_text"] = unit_text
    if w is not None:
        row["winner_offer_total_display"] = format_money_ru(float(w))
    else:
        row["winner_offer_total_display"] = ""
    if q is not None:
        qty = float(q)
        row["notice_quantity_display"] = str(int(qty)) if qty == int(qty) else str(qty)
    else:
        row["notice_quantity_display"] = ""
    if w is not None and q is not None and unit_text:
        row["price_key_breakdown"] = f"{row['winner_offer_total_display']} / {row['notice_quantity_display']} = {unit_text} ₽"
    else:
        row["price_key_breakdown"] = ""


def enrich_matches_with_procedure_status(
    db_path: Path,
    matches: list[dict[str, Any]],
    *,
    fetch_timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Attach procedure status and EIS outcome fields (journal cache + stored outcome)."""
    journal_cache: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for match in matches:
        row = dict(match)
        pn = str(row.get("purchase_number") or "").strip()
        lk = str(row.get("line_key") or "").strip()
        if not pn:
            row["procedure_status_label"] = ""
            _enrich_outcome_fields(row, None)
            out.append(row)
            continue
        if pn not in journal_cache:
            label = ""
            try:
                payload = get_or_fetch_notice_event_journal(
                    db_path,
                    purchase_number=pn,
                    source_url=notice_event_journal_url(pn),
                    timeout=fetch_timeout,
                    force_refresh=False,
                )
                if payload.get("ok"):
                    journal_rows = parse_notice_event_journal_html(str(payload.get("html_text") or ""))
                    label = procedure_status_from_journal_rows(journal_rows)
            except Exception:
                label = ""
            journal_cache[pn] = label
        row["procedure_status_label"] = journal_cache[pn]
        outcome_row = get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk) if lk else None
        _enrich_outcome_fields(row, outcome_row)
        if outcome_row and row.get("eis_outcome_label"):
            row["procedure_status_label"] = str(row.get("eis_outcome_label") or row["procedure_status_label"])
        out.append(row)
    return out

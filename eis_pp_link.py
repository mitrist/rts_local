"""Compare EIS notice characteristics with RTS competitor find (PP) cards."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from monitor_find_to_draft import build_find_ktru_characteristics
from notice_to_rts_pp import compare_notice_to_draft


@dataclass
class MatchResult:
    matched_count: int = 0
    total_compared: int = 0
    notice_only_count: int = 0
    pp_only_count: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched_count": self.matched_count,
            "total_compared": self.total_compared,
            "notice_only_count": self.notice_only_count,
            "pp_only_count": self.pp_only_count,
            "rows": self.rows,
        }


def _ui_status(raw_status: str) -> str:
    if raw_status == "ok":
        return "ok"
    if raw_status == "unmatched_notice":
        return "notice_only"
    if raw_status == "auto_filled":
        return "pp_only"
    return "mismatch"


def compare_notice_to_find(
    notice_chars: list[dict[str, Any]],
    find_item: dict[str, Any],
    *,
    ktru_item: dict[str, Any] | None = None,
) -> MatchResult:
    """Compare enriched notice characteristics with competitor find ktruCharacteristics.

    Uses union semantics via compare_notice_to_draft: each row is one characteristic
    key from EIS ∪ PP. Metric is matched_count / total_compared (N/M), no ratio.
    """
    pp_chars = find_item.get("ktruCharacteristics")
    if not isinstance(pp_chars, list):
        pp_chars = []
    if ktru_item and pp_chars:
        mapped_chars, _ = build_find_ktru_characteristics(ktru_item, find_item)
        draft_chars = mapped_chars
    else:
        draft_chars = [ch for ch in pp_chars if isinstance(ch, dict)]
    pseudo_draft: dict[str, Any] = {"ktruCharacteristics": draft_chars}
    raw = compare_notice_to_draft(notice_chars, pseudo_draft, ktru_item=ktru_item)

    result = MatchResult()
    for row in raw.get("rows") or []:
        if not isinstance(row, dict):
            continue
        raw_status = str(row.get("status") or "")
        ui_status = _ui_status(raw_status)
        if ui_status == "ok":
            result.matched_count += 1
        elif ui_status == "notice_only":
            result.notice_only_count += 1
        elif ui_status == "pp_only":
            result.pp_only_count += 1

        key = str(row.get("rts_code") or row.get("notice_code") or "").strip()
        label = str(row.get("notice_label") or row.get("rts_name") or key or "—").strip()
        result.rows.append(
            {
                "key": key or None,
                "label": label,
                "notice_values": list(row.get("notice_values") or []),
                "pp_values": list(row.get("draft_values") or []),
                "status": ui_status,
                "detail": str(row.get("detail") or ""),
            }
        )

    result.total_compared = len(result.rows)
    return result


def parse_utc_datetime(raw: str) -> datetime | None:
    """Parse ISO timestamps from EIS RSS or RTS link_date (assumed UTC if naive)."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        elif "+" not in text and not text.endswith("Z"):
            text = text + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def compute_lag_minutes(notice_pub_utc: str, pp_link_date: str) -> float | None:
    """Minutes from notice publication to PP registry link_date (negative = PP before notice)."""
    t_notice = parse_utc_datetime(notice_pub_utc)
    t_pp = parse_utc_datetime(pp_link_date)
    if t_notice is None or t_pp is None:
        return None
    return (t_pp - t_notice).total_seconds() / 60.0


def lag_within_window(lag_minutes: float | None, window_min: float) -> bool:
    if lag_minutes is None:
        return False
    return abs(float(lag_minutes)) <= float(window_min)


def format_lag_minutes(lag_minutes: float | None) -> str:
    if lag_minutes is None:
        return "—"
    rounded = round(float(lag_minutes), 1)
    if rounded > 0:
        return f"+{rounded:g} мин"
    if rounded < 0:
        return f"{rounded:g} мин"
    return "0 мин"


def is_exact_match_notice_driven(result: MatchResult) -> bool:
    """Variant A: every notice characteristic matches PP; pp_only extras are allowed."""
    if result.notice_only_count > 0:
        return False
    has_notice_match = False
    for row in result.rows:
        status = str(row.get("status") or "")
        if status == "ok":
            has_notice_match = True
        elif status == "pp_only":
            continue
        else:
            return False
    return has_notice_match

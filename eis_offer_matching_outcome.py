"""Procedure outcome sync and PP search for the offer-matching stage."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from eis_analytics_db import get_notice_eis_outcome, latest_winner_price_pp_hint, list_pp_eis_matches
from eis_notice_event_journal_parse import (
    EIS_OUTCOME_COMPLETED_WINNER,
    EIS_OUTCOME_UNKNOWN,
)
from eis_pp_event_chain import _enrich_outcome_fields
from eis_pp_link_service import pilot_ktru_codes, pp_eis_winner_price_enabled, refresh_notice_eis_outcome


def _has_winner_price_match(db_path: Path, *, purchase_number: str, line_key: str) -> bool:
    rows = list_pp_eis_matches(
        db_path,
        limit=5,
        purchase_number=purchase_number,
        line_key=line_key,
        exact_only=True,
    )
    return any(str(row.get("match_origin") or "") == "eis_winner_price" for row in rows)


def _winner_search_delay_sec() -> float:
    raw = str(os.environ.get("PP_EIS_WINNER_SEARCH_DELAY_SEC") or "1.5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.5


def _attach_pp_match_links(db_path: Path, row: dict[str, Any]) -> dict[str, Any]:
    pn = str(row.get("purchase_number") or "").strip()
    lk = str(row.get("line_key") or "").strip()
    if not pn or not lk:
        row["pp_match_count"] = 0
        row["winner_pp_match_count"] = 0
        row["pp_match_offer_id"] = ""
        row["pp_match_url"] = ""
        return row
    pp_matches = list_pp_eis_matches(
        db_path,
        limit=5,
        purchase_number=pn,
        line_key=lk,
        exact_only=True,
    )
    winner_matches = [m for m in pp_matches if str(m.get("match_origin") or "") == "eis_winner_price"]
    display_matches = winner_matches or pp_matches
    row["pp_match_count"] = len(pp_matches)
    row["winner_pp_match_count"] = len(winner_matches)
    top = display_matches[0] if display_matches else None
    if top:
        row["pp_match_offer_id"] = str(top.get("offer_price_list_item_id") or "")
        row["pp_match_url"] = str(top.get("pp_offer_url") or "")
    else:
        row["pp_match_offer_id"] = ""
        row["pp_match_url"] = ""
    if (
        str(row.get("eis_outcome") or "") == EIS_OUTCOME_COMPLETED_WINNER
        and not winner_matches
    ):
        row["pp_search_hint"] = latest_winner_price_pp_hint(db_path, purchase_number=pn)
    else:
        row["pp_search_hint"] = ""
    return row


def enrich_offer_matching_row(
    db_path: Path,
    row: dict[str, Any],
    outcome_row: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(row)
    _enrich_outcome_fields(out, outcome_row)
    out["procedure_status_label"] = str(out.get("eis_outcome_label") or "")
    return _attach_pp_match_links(db_path, out)


def sync_offer_matching_line(
    db_path: Path,
    *,
    meta_dir: Path,
    line: dict[str, Any],
    notice_pub_date_utc: str = "",
    fetch_timeout: float = 20.0,
    run_pp_match: bool = True,
    refresh_outcome: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Refresh EIS procedure outcome; search PP pair when completed with winner."""
    pn = str(line.get("purchase_number") or "").strip()
    lk = str(line.get("line_key") or "").strip()
    ktru_code = str(line.get("ktru_code") or "").strip()
    if not pn or not lk or line.get("html_status") != "loaded":
        outcome_row = get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk) if pn and lk else None
        return enrich_offer_matching_row(db_path, line, outcome_row)

    if refresh_outcome:
        cached_outcome = get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk)
        if force_refresh or not cached_outcome or str(cached_outcome.get("eis_outcome") or EIS_OUTCOME_UNKNOWN) == EIS_OUTCOME_UNKNOWN:
            refresh_notice_eis_outcome(
                db_path,
                purchase_number=pn,
                line_key=lk,
                ktru_code=ktru_code,
                quantity_text=str(line.get("quantity_text") or ""),
                fetch_timeout=fetch_timeout,
                force_refresh=force_refresh,
            )
    outcome_row = get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk)
    outcome_code = str((outcome_row or {}).get("eis_outcome") or EIS_OUTCOME_UNKNOWN)

    matches_saved = 0
    if (
        run_pp_match
        and pp_eis_winner_price_enabled()
        and outcome_code == EIS_OUTCOME_COMPLETED_WINNER
        and ktru_code in pilot_ktru_codes()
        and not _has_winner_price_match(db_path, purchase_number=pn, line_key=lk)
    ):
        _throttle_winner_search()
        from eis_pp_link_service import match_notice_by_winner_price

        matches_saved = match_notice_by_winner_price(
            db_path,
            meta_dir=meta_dir,
            purchase_number=pn,
            line_key=lk,
            ktru_code=ktru_code,
            html_chars=line.get("characteristics") if isinstance(line.get("characteristics"), list) else None,
            quantity_text=str(line.get("quantity_text") or ""),
            notice_pub_date_utc=notice_pub_date_utc or str(line.get("pub_date_utc") or line.get("first_seen_at_utc") or ""),
            fetch_timeout=fetch_timeout,
            force_refresh=False,
            skip_outcome_refresh=True,
        )

    enriched = enrich_offer_matching_row(db_path, line, get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk))
    enriched["pp_matches_saved"] = matches_saved
    return enriched


def _throttle_winner_search() -> None:
    global _last_pp_winner_search_mono
    delay = _winner_search_delay_sec()
    if delay <= 0:
        return
    now = time.monotonic()
    if _last_pp_winner_search_mono > 0:
        wait = delay - (now - _last_pp_winner_search_mono)
        if wait > 0:
            time.sleep(wait)
    _last_pp_winner_search_mono = time.monotonic()


_last_pp_winner_search_mono = 0.0


def sync_offer_matching_lines(
    db_path: Path,
    *,
    meta_dir: Path,
    lines: list[dict[str, Any]],
    fetch_timeout: float = 20.0,
    run_pp_match: bool = True,
    refresh_outcome: bool = True,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in lines:
        pub = str(line.get("pub_date_utc") or line.get("first_seen_at_utc") or "").strip()
        out.append(
            sync_offer_matching_line(
                db_path,
                meta_dir=meta_dir,
                line=line,
                notice_pub_date_utc=pub,
                fetch_timeout=fetch_timeout,
                run_pp_match=run_pp_match,
                refresh_outcome=refresh_outcome,
                force_refresh=force_refresh,
            )
        )
    return out

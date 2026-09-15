"""Incremental EIS notice ↔ RTS PP match computation (hypothesis phase: exact + ±window)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eis_analytics_db import (
    analytics_db_path,
    append_pp_eis_event,
    count_pilot_lines_for_notice,
    get_notice_eis_outcome,
    get_or_fetch_notice_event_journal,
    get_or_fetch_notice_supplier_results,
    list_pp_monitor_findings_by_ktru,
    offer_matching_ktru_rows,
    upsert_notice_eis_outcome,
    upsert_pp_eis_match,
    upsert_pp_monitor_finding,
)
from eis_pp_link import (
    compare_notice_to_find,
    compute_lag_minutes,
    is_exact_match_notice_driven,
    lag_within_window,
)
from pp_stack import stack_key


DEFAULT_PILOT_KTRU = (
    "26.20.13.000-00000002",
    "26.20.15.000-00000028",
    "26.20.18.000-00000069",
)


def pp_eis_link_enabled() -> bool:
    return os.environ.get("PP_EIS_LINK_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def pilot_ktru_codes() -> frozenset[str]:
    raw = os.environ.get("PP_EIS_LINK_KTRU", "").strip()
    if raw:
        return frozenset(x.strip() for x in raw.split(",") if x.strip())
    return frozenset(DEFAULT_PILOT_KTRU)


def pp_eis_link_window_min() -> float:
    try:
        return max(1.0, float(os.environ.get("PP_EIS_LINK_WINDOW_MIN", "60")))
    except ValueError:
        return 60.0


def pp_eis_link_notice_window_days() -> int:
    try:
        return max(1, int(os.environ.get("PP_EIS_LINK_NOTICE_WINDOW_DAYS", "90")))
    except ValueError:
        return 90


def pp_eis_winner_price_enabled() -> bool:
    return os.environ.get("PP_EIS_WINNER_PRICE_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def pp_eis_winner_search_max() -> int:
    try:
        return max(1, int(os.environ.get("PP_EIS_WINNER_SEARCH_MAX", "200")))
    except ValueError:
        return 200


def eis_journal_cache_ttl_hours() -> float:
    """Re-fetch journal for non-terminal outcomes when cache is older than this."""
    try:
        return max(0.25, float(os.environ.get("EIS_JOURNAL_CACHE_TTL_HOURS", "2")))
    except ValueError:
        return 2.0


def should_force_refresh_eis_journal(
    db_path: Path,
    *,
    purchase_number: str,
    cached_outcome: dict[str, Any] | None,
    force_refresh: bool = False,
) -> bool:
    """Refresh stale journal for in-progress notices; keep terminal outcomes cached."""
    if force_refresh:
        return True
    from eis_analytics_db import get_notice_event_journal_fetched_at
    from eis_notice_event_journal_parse import EIS_OUTCOME_IN_PROGRESS, EIS_OUTCOME_UNKNOWN

    outcome = str((cached_outcome or {}).get("eis_outcome") or EIS_OUTCOME_UNKNOWN)
    if outcome not in (EIS_OUTCOME_IN_PROGRESS, EIS_OUTCOME_UNKNOWN):
        return False
    fetched_at = get_notice_event_journal_fetched_at(db_path, purchase_number=purchase_number)
    if not fetched_at:
        return True
    from eis_pp_link import parse_utc_datetime

    fetched_dt = parse_utc_datetime(fetched_at)
    if fetched_dt is None:
        return True
    age_hours = (datetime.now(timezone.utc) - fetched_dt).total_seconds() / 3600.0
    return age_hours >= eis_journal_cache_ttl_hours()


def _is_pilot_ktru(ktru_code: str) -> bool:
    return str(ktru_code or "").strip() in pilot_ktru_codes()


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _notice_pub_from_utc() -> str:
    start = datetime.now(timezone.utc) - timedelta(days=pp_eis_link_notice_window_days())
    return _utc_iso(start)


def _notice_pub_iso(notice_row: dict[str, Any]) -> str:
    return str(notice_row.get("pub_date_utc") or notice_row.get("first_seen_at_utc") or "").strip()


def _prepare_notice_chars(
    db_path: Path,
    *,
    purchase_number: str,
    line_key: str,
    ktru_code: str,
    html_chars: list[dict[str, Any]] | None = None,
    history_path: Path | None = None,
) -> list[dict[str, Any]]:
    from eis_analytics_db import enrich_notice_characteristics

    chars = html_chars
    if chars is None:
        rows = offer_matching_ktru_rows(
            db_path,
            limit=1,
            purchase_number=purchase_number,
            line_key=line_key,
        )
        if not rows:
            return []
        chars = rows[0].get("characteristics") or []
    return enrich_notice_characteristics(
        db_path,
        purchase_number=purchase_number,
        ktru_code=ktru_code,
        html_chars=chars,
        history_path=history_path,
    )


def _load_ktru_template(meta_dir: Path, ktru_code: str) -> dict[str, Any] | None:
    try:
        from rts_ktru_cache import load_cached_template

        return load_cached_template(meta_dir / "ktru_rts_cache", ktru_code, ttl_sec=0)
    except Exception:
        return None


def _pair_passes_hypothesis(
    match_result,
    *,
    notice_pub_date_utc: str,
    pp_link_date: str,
) -> tuple[bool, float | None]:
    lag = compute_lag_minutes(notice_pub_date_utc, pp_link_date)
    if not is_exact_match_notice_driven(match_result):
        return False, lag
    if not lag_within_window(lag, pp_eis_link_window_min()):
        return False, lag
    return True, lag


def _save_winner_price_match(
    db_path: Path,
    *,
    purchase_number: str,
    line_key: str,
    offer_price_list_item_id: str,
    ktru_code: str,
    stack_key_value: str,
    match_result,
    notice_pub_date_utc: str = "",
    pp_link_date: str = "",
) -> bool:
    if not is_exact_match_notice_driven(match_result):
        return False
    lag = compute_lag_minutes(notice_pub_date_utc, pp_link_date)
    upsert_pp_eis_match(
        db_path,
        purchase_number=purchase_number,
        line_key=line_key,
        offer_price_list_item_id=offer_price_list_item_id,
        ktru_code=ktru_code,
        stack_key=stack_key_value,
        matched_count=match_result.matched_count,
        total_compared=match_result.total_compared,
        notice_only_count=match_result.notice_only_count,
        pp_only_count=match_result.pp_only_count,
        diff_json=json.dumps(match_result.rows, ensure_ascii=False),
        notice_pub_date_utc=notice_pub_date_utc,
        pp_link_date=pp_link_date,
        is_exact_match=True,
        lag_minutes=lag,
        match_origin="eis_winner_price",
    )
    append_pp_eis_event(
        db_path,
        event_type="match_computed",
        ktru_code=ktru_code,
        payload={
            "purchase_number": purchase_number,
            "line_key": line_key,
            "offer_price_list_item_id": offer_price_list_item_id,
            "lag_minutes": lag,
            "is_exact_match": True,
            "match_origin": "eis_winner_price",
        },
    )
    return True


def _load_customer_cookie_for_winner_search(meta_dir: Path) -> tuple[str, dict[str, Any]]:
    try:
        from rts_customer_pp_api import load_customer_cookie, probe_customer_session

        cookie = load_customer_cookie(meta_dir)
        if not cookie:
            return "", {"ok": False, "reason": "no_cookie"}
        probe = probe_customer_session(cookie, meta_dir=meta_dir)
        if not probe.get("ok"):
            return "", probe
        return cookie, probe
    except Exception as exc:
        return "", {"ok": False, "reason": "load_failed", "detail": str(exc)[:300]}


def refresh_notice_eis_outcome(
    db_path: Path,
    *,
    purchase_number: str,
    line_key: str,
    ktru_code: str = "",
    quantity_text: str = "",
    fetch_timeout: float = 30.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch journal (+ supplier-results if needed) and upsert notice_eis_outcome."""
    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    from eis_notice_event_journal_parse import EIS_OUTCOME_UNKNOWN

    if not pn or not lk:
        return {"eis_outcome": EIS_OUTCOME_UNKNOWN, "ok": False}

    from eis_notice_event_journal_fetch import notice_event_journal_url
    from eis_notice_event_journal_parse import (
        EIS_OUTCOME_COMPLETED_NO_BIDS,
        EIS_OUTCOME_COMPLETED_WINNER,
        EIS_OUTCOME_IN_PROGRESS,
        eis_outcome_from_journal_rows,
        parse_notice_event_journal_html,
    )
    from eis_supplier_results_fetch import notice_supplier_results_url
    from eis_supplier_results_parse import parse_supplier_results_html
    from eis_winner_price_key import compute_expected_pp_unit_price, notice_quantity_from_text

    cached_outcome = get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk) if pn and lk else None
    journal_force = should_force_refresh_eis_journal(
        db_path,
        purchase_number=pn,
        cached_outcome=cached_outcome,
        force_refresh=force_refresh,
    )
    journal_payload = get_or_fetch_notice_event_journal(
        db_path,
        purchase_number=pn,
        source_url=notice_event_journal_url(pn),
        timeout=fetch_timeout,
        force_refresh=journal_force,
    )
    journal_rows: list[dict[str, Any]] = []
    if journal_payload.get("ok"):
        journal_rows = parse_notice_event_journal_html(str(journal_payload.get("html_text") or ""))
    outcome = eis_outcome_from_journal_rows(journal_rows)

    if outcome == EIS_OUTCOME_COMPLETED_NO_BIDS:
        upsert_notice_eis_outcome(
            db_path,
            purchase_number=pn,
            line_key=lk,
            eis_outcome=outcome,
        )
        return {"eis_outcome": outcome, "ok": True}

    if outcome != EIS_OUTCOME_COMPLETED_WINNER:
        upsert_notice_eis_outcome(
            db_path,
            purchase_number=pn,
            line_key=lk,
            eis_outcome=EIS_OUTCOME_IN_PROGRESS,
        )
        return {"eis_outcome": EIS_OUTCOME_IN_PROGRESS, "ok": True}

    supplier_payload = get_or_fetch_notice_supplier_results(
        db_path,
        purchase_number=pn,
        source_url=notice_supplier_results_url(pn),
        timeout=fetch_timeout,
        force_refresh=force_refresh,
    )
    if not supplier_payload.get("ok"):
        return {
            "eis_outcome": outcome,
            "ok": False,
            "error": str(supplier_payload.get("error_text") or "")[:300],
        }

    winner = parse_supplier_results_html(str(supplier_payload.get("html_text") or ""))
    if winner is None:
        return {"eis_outcome": outcome, "ok": False, "error": "no_winner"}

    qty_text = str(quantity_text or "").strip()
    if not qty_text:
        rows = offer_matching_ktru_rows(
            db_path,
            limit=1,
            purchase_number=pn,
            line_key=lk,
        )
        if rows:
            qty_text = str(rows[0].get("quantity_text") or "").strip()
    quantity = notice_quantity_from_text(qty_text)
    if quantity is None or quantity <= 0:
        upsert_notice_eis_outcome(
            db_path,
            purchase_number=pn,
            line_key=lk,
            eis_outcome=outcome,
            winner_participant_id=winner.participant_id,
            winner_offer_total=winner.offer_total,
            protocol_at_utc=str(winner.formed_at_utc or ""),
        )
        return {"eis_outcome": outcome, "ok": True, "error": "invalid_quantity"}

    target_unit, target_text = compute_expected_pp_unit_price(winner.offer_total, quantity)
    upsert_notice_eis_outcome(
        db_path,
        purchase_number=pn,
        line_key=lk,
        eis_outcome=outcome,
        winner_participant_id=winner.participant_id,
        winner_offer_total=winner.offer_total,
        notice_quantity=quantity,
        expected_pp_unit_price=target_unit,
        expected_pp_unit_price_text=target_text,
        protocol_at_utc=str(winner.formed_at_utc or ""),
    )
    return {
        "eis_outcome": outcome,
        "ok": True,
        "target_unit": target_unit,
        "target_text": target_text,
        "winner_participant_id": winner.participant_id,
    }


def match_notice_by_winner_price(
    db_path: Path,
    *,
    meta_dir: Path,
    purchase_number: str,
    line_key: str,
    ktru_code: str,
    html_chars: list[dict[str, Any]] | None = None,
    quantity_text: str = "",
    history_path: Path | None = None,
    notice_pub_date_utc: str = "",
    fetch_timeout: float = 30.0,
    force_refresh: bool = False,
    customer_cookie: str | None = None,
    skip_outcome_refresh: bool = False,
) -> int:
    """EIS-driven match: protocol + winner offer / notice quantity → exact PP unit price."""
    if not pp_eis_link_enabled() or not pp_eis_winner_price_enabled() or not _is_pilot_ktru(ktru_code):
        return 0
    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    if not pn or not lk:
        return 0

    from eis_notice_event_journal_parse import EIS_OUTCOME_COMPLETED_WINNER
    from eis_winner_price_key import pp_unit_price_matches_target

    if skip_outcome_refresh:
        from eis_analytics_db import get_notice_eis_outcome

        cached = get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk)
        if not cached or str(cached.get("eis_outcome") or "") != EIS_OUTCOME_COMPLETED_WINNER:
            return 0
        target_unit = cached.get("expected_pp_unit_price")
        if target_unit is None:
            return 0
        target_unit = float(target_unit)
    else:
        refresh = refresh_notice_eis_outcome(
            db_path,
            purchase_number=pn,
            line_key=lk,
            ktru_code=ktru_code,
            quantity_text=quantity_text,
            fetch_timeout=fetch_timeout,
            force_refresh=force_refresh,
        )
        if str(refresh.get("eis_outcome") or "") != EIS_OUTCOME_COMPLETED_WINNER:
            return 0
        target_unit_raw = refresh.get("target_unit")
        if target_unit_raw is None:
            return 0
        target_unit = float(target_unit_raw)

    pilot_line_count = count_pilot_lines_for_notice(
        db_path,
        purchase_number=pn,
        pilot_ktru_codes=pilot_ktru_codes(),
    )
    if pilot_line_count > 1:
        append_pp_eis_event(
            db_path,
            event_type="multi_line_skip",
            ktru_code=ktru_code,
            payload={"purchase_number": pn, "line_key": lk, "pilot_line_count": pilot_line_count},
        )
        return 0

    cookie = str(customer_cookie or "").strip()
    if not cookie:
        cookie, probe = _load_customer_cookie_for_winner_search(meta_dir)
        if not cookie:
            append_pp_eis_event(
                db_path,
                event_type="winner_price_no_cookie",
                ktru_code=ktru_code,
                payload={"purchase_number": pn, "probe": probe},
            )
            return 0

    from rts_customer_pp_api import RtsCustomerPpApiError, build_search_payload, find_offer_item, search_registry_page

    enriched = _prepare_notice_chars(
        db_path,
        purchase_number=pn,
        line_key=lk,
        ktru_code=ktru_code,
        html_chars=html_chars,
        history_path=history_path,
    )
    if not enriched:
        return 0
    ktru_item = _load_ktru_template(meta_dir, ktru_code)

    saved = 0
    offset = 0
    take = 50
    max_candidates = pp_eis_winner_search_max()
    processed = 0
    while processed < max_candidates:
        payload = build_search_payload(
            ktru_code,
            skip=offset,
            take=take,
            price_from=target_unit,
            price_to=target_unit,
        )
        try:
            summaries, total = search_registry_page(cookie, payload)
        except RtsCustomerPpApiError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            append_pp_eis_event(
                db_path,
                event_type="winner_price_search_failed",
                ktru_code=ktru_code,
                payload={
                    "purchase_number": pn,
                    "error": str(exc)[:300],
                    "reason": str(getattr(exc, "reason", "") or "api_error"),
                    "http_status": detail.get("http_status"),
                },
            )
            break
        except Exception as exc:
            append_pp_eis_event(
                db_path,
                event_type="winner_price_search_failed",
                ktru_code=ktru_code,
                payload={"purchase_number": pn, "error": str(exc)[:300]},
            )
            break
        if not summaries:
            break
        for summary in summaries:
            if processed >= max_candidates:
                break
            processed += 1
            oid = str(summary.offer_price_list_item_id or "").strip()
            if not oid:
                continue
            if not pp_unit_price_matches_target(summary.unit_price, target_unit):
                continue
            try:
                find_item = find_offer_item(cookie, oid)
            except Exception:
                continue
            if not isinstance(find_item, dict) or not find_item:
                continue
            result = compare_notice_to_find(enriched, find_item, ktru_item=ktru_item)
            ktru_obj = find_item.get("ktru") if isinstance(find_item.get("ktru"), dict) else {}
            from pp_stack import find_characteristics_fingerprint, stack_key as pp_stack_key_fn

            chash, _ = find_characteristics_fingerprint(find_item)
            stack_key_value = pp_stack_key_fn(ktru_code, chash) if chash else ""
            pp_link = str(summary.link_date or find_item.get("createDateTime") or "").strip()
            upsert_pp_monitor_finding(
                db_path,
                offer_price_list_item_id=oid,
                find_json=json.dumps(find_item, ensure_ascii=False),
                ktru_code=str(ktru_obj.get("code") or ktru_code),
                trade_mark=str(find_item.get("tradeMark") or summary.trade_mark or ""),
                link_date=pp_link,
                unit_price=summary.unit_price,
                card_url=str(summary.card_url or ""),
                source="winner_price_search",
                trigger_recompute=False,
            )
            if _save_winner_price_match(
                db_path,
                purchase_number=pn,
                line_key=lk,
                offer_price_list_item_id=oid,
                ktru_code=ktru_code,
                stack_key_value=stack_key_value,
                match_result=result,
                notice_pub_date_utc=notice_pub_date_utc,
                pp_link_date=pp_link,
            ):
                saved += 1
        offset += len(summaries)
        if offset >= total or processed >= max_candidates:
            break
    if saved:
        from eis_analytics_db import get_notice_eis_outcome

        outcome_row = get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk)
        target_text = str((outcome_row or {}).get("expected_pp_unit_price_text") or "")
        append_pp_eis_event(
            db_path,
            event_type="notice_winner_price_matched",
            ktru_code=ktru_code,
            payload={
                "purchase_number": pn,
                "line_key": lk,
                "matches_saved": saved,
                "target_unit_price_text": target_text,
            },
        )
    return saved


def _save_match_pair(
    db_path: Path,
    *,
    purchase_number: str,
    line_key: str,
    offer_price_list_item_id: str,
    ktru_code: str,
    stack_key_value: str,
    match_result,
    notice_pub_date_utc: str = "",
    pp_link_date: str = "",
    lag_minutes: float | None = None,
    match_origin: str = "live",
) -> bool:
    ok, lag = _pair_passes_hypothesis(
        match_result,
        notice_pub_date_utc=notice_pub_date_utc,
        pp_link_date=pp_link_date,
    )
    if not ok:
        return False
    upsert_pp_eis_match(
        db_path,
        purchase_number=purchase_number,
        line_key=line_key,
        offer_price_list_item_id=offer_price_list_item_id,
        ktru_code=ktru_code,
        stack_key=stack_key_value,
        matched_count=match_result.matched_count,
        total_compared=match_result.total_compared,
        notice_only_count=match_result.notice_only_count,
        pp_only_count=match_result.pp_only_count,
        diff_json=json.dumps(match_result.rows, ensure_ascii=False),
        notice_pub_date_utc=notice_pub_date_utc,
        pp_link_date=pp_link_date,
        is_exact_match=True,
        lag_minutes=lag if lag is not None else lag_minutes,
        match_origin=match_origin,
    )
    append_pp_eis_event(
        db_path,
        event_type="match_computed",
        ktru_code=ktru_code,
        payload={
            "purchase_number": purchase_number,
            "line_key": line_key,
            "offer_price_list_item_id": offer_price_list_item_id,
            "lag_minutes": lag,
            "is_exact_match": True,
            "match_origin": match_origin,
        },
    )
    return True


def compute_match_for_pair(
    db_path: Path,
    *,
    meta_dir: Path,
    purchase_number: str,
    line_key: str,
    offer_price_list_item_id: str,
    ktru_code: str,
    find_item: dict[str, Any],
    notice_chars: list[dict[str, Any]] | None = None,
    history_path: Path | None = None,
    notice_pub_date_utc: str = "",
    pp_link_date: str = "",
    characteristics_hash: str = "",
    match_origin: str = "live",
) -> dict[str, Any] | None:
    if not pp_eis_link_enabled() or not _is_pilot_ktru(ktru_code):
        return None
    enriched = _prepare_notice_chars(
        db_path,
        purchase_number=purchase_number,
        line_key=line_key,
        ktru_code=ktru_code,
        html_chars=notice_chars,
        history_path=history_path,
    )
    if not enriched or not find_item:
        return None
    ktru_item = _load_ktru_template(meta_dir, ktru_code)
    result = compare_notice_to_find(enriched, find_item, ktru_item=ktru_item)
    chash = str(characteristics_hash or "").strip()
    stack_key_value = stack_key(ktru_code, chash) if chash else ""
    if _save_match_pair(
        db_path,
        purchase_number=purchase_number,
        line_key=line_key,
        offer_price_list_item_id=offer_price_list_item_id,
        ktru_code=ktru_code,
        stack_key_value=stack_key_value,
        match_result=result,
        notice_pub_date_utc=notice_pub_date_utc,
        pp_link_date=pp_link_date,
        match_origin=match_origin,
    ):
        payload = result.as_dict()
        payload["lag_minutes"] = compute_lag_minutes(notice_pub_date_utc, pp_link_date)
        payload["is_exact_match"] = True
        return payload
    return None


def recompute_matches_for_notice(
    db_path: Path,
    *,
    meta_dir: Path,
    purchase_number: str,
    line_key: str,
    ktru_code: str,
    html_chars: list[dict[str, Any]] | None = None,
    history_path: Path | None = None,
    notice_pub_date_utc: str = "",
    match_origin: str = "live",
) -> int:
    if not pp_eis_link_enabled() or not _is_pilot_ktru(ktru_code):
        return 0
    findings = list_pp_monitor_findings_by_ktru(db_path, ktru_code)
    if not findings:
        return 0
    window = pp_eis_link_window_min()
    saved = 0
    for finding in findings:
        oid = str(finding.get("offer_price_list_item_id") or "").strip()
        find_item = finding.get("find") if isinstance(finding.get("find"), dict) else {}
        pp_link = str(finding.get("link_date") or "")
        if not oid or not find_item:
            continue
        lag = compute_lag_minutes(notice_pub_date_utc, pp_link)
        if not lag_within_window(lag, window):
            continue
        chash = str(finding.get("characteristics_hash") or "").strip()
        if compute_match_for_pair(
            db_path,
            meta_dir=meta_dir,
            purchase_number=purchase_number,
            line_key=line_key,
            offer_price_list_item_id=oid,
            ktru_code=ktru_code,
            find_item=find_item,
            notice_chars=html_chars,
            history_path=history_path,
            notice_pub_date_utc=notice_pub_date_utc,
            pp_link_date=pp_link,
            characteristics_hash=chash,
            match_origin=match_origin,
        ):
            saved += 1
    if saved:
        append_pp_eis_event(
            db_path,
            event_type="notice_seen",
            ktru_code=ktru_code,
            payload={"purchase_number": purchase_number, "line_key": line_key, "matches_saved": saved},
        )
    return saved


def recompute_matches_for_pp(
    db_path: Path,
    *,
    meta_dir: Path,
    offer_price_list_item_id: str,
    ktru_code: str,
    find_item: dict[str, Any] | None = None,
    link_date: str = "",
    characteristics_hash: str = "",
    history_path: Path | None = None,
) -> int:
    if not pp_eis_link_enabled() or not _is_pilot_ktru(ktru_code):
        return 0
    oid = str(offer_price_list_item_id or "").strip()
    if not oid:
        return 0
    if find_item is None:
        from eis_analytics_db import get_pp_monitor_finding

        row = get_pp_monitor_finding(db_path, oid)
        if not row:
            return 0
        find_item = row.get("find") if isinstance(row.get("find"), dict) else {}
        link_date = str(row.get("link_date") or link_date)
        characteristics_hash = str(row.get("characteristics_hash") or characteristics_hash)
    if not find_item:
        return 0
    window = pp_eis_link_window_min()
    notices = offer_matching_ktru_rows(
        db_path,
        limit=500,
        ktru_queries=[ktru_code],
        pub_from_utc=_notice_pub_from_utc(),
    )
    saved = 0
    chash = str(characteristics_hash or "").strip()
    for notice in notices:
        pn = str(notice.get("purchase_number") or "").strip()
        lk = str(notice.get("line_key") or "").strip()
        notice_pub = _notice_pub_iso(notice)
        if not pn or not lk or not notice_pub:
            continue
        lag = compute_lag_minutes(notice_pub, link_date)
        if not lag_within_window(lag, window):
            continue
        if compute_match_for_pair(
            db_path,
            meta_dir=meta_dir,
            purchase_number=pn,
            line_key=lk,
            offer_price_list_item_id=oid,
            ktru_code=ktru_code,
            find_item=find_item,
            notice_chars=notice.get("characteristics") or [],
            history_path=history_path,
            notice_pub_date_utc=notice_pub,
            pp_link_date=link_date,
            characteristics_hash=chash,
        ):
            saved += 1
    if saved:
        append_pp_eis_event(
            db_path,
            event_type="pp_seen",
            ktru_code=ktru_code,
            payload={"offer_price_list_item_id": oid, "matches_saved": saved},
        )
    return saved


def backfill_pp_eis_links(
    db_path: Path,
    *,
    meta_dir: Path,
    days: int = 7,
    history_path: Path | None = None,
    batch_size: int = 200,
) -> dict[str, Any]:
    """Recompute hypothesis candidates for pilot notices over the last N days."""
    if not pp_eis_link_enabled():
        return {"ok": False, "reason": "disabled", "notices_processed": 0, "matches_saved": 0}
    pub_from = _utc_iso(datetime.now(timezone.utc) - timedelta(days=max(1, int(days))))
    notices_processed = 0
    matches_saved = 0
    for ktru_code in sorted(pilot_ktru_codes()):
        offset = 0
        while True:
            notices = offer_matching_ktru_rows(
                db_path,
                limit=batch_size,
                offset=offset,
                catalog_ktru_codes=[ktru_code],
                pub_from_utc=pub_from,
            )
            if not notices:
                break
            for notice in notices:
                pn = str(notice.get("purchase_number") or "").strip()
                lk = str(notice.get("line_key") or "").strip()
                if not pn or not lk:
                    continue
                notices_processed += 1
                notice_pub = _notice_pub_iso(notice)
                from eis_analytics_db import get_notice_eis_outcome
                from eis_notice_event_journal_parse import EIS_OUTCOME_IN_PROGRESS, EIS_OUTCOME_UNKNOWN

                refresh_notice_eis_outcome(
                    db_path,
                    purchase_number=pn,
                    line_key=lk,
                    ktru_code=ktru_code,
                    quantity_text=str(notice.get("quantity_text") or ""),
                )
                outcome_row = get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk)
                outcome = str(outcome_row.get("eis_outcome") or "") if outcome_row else EIS_OUTCOME_UNKNOWN
                if outcome in (EIS_OUTCOME_IN_PROGRESS, EIS_OUTCOME_UNKNOWN):
                    matches_saved += recompute_matches_for_notice(
                        db_path,
                        meta_dir=meta_dir,
                        purchase_number=pn,
                        line_key=lk,
                        ktru_code=ktru_code,
                        html_chars=notice.get("characteristics") or [],
                        history_path=history_path,
                        notice_pub_date_utc=notice_pub,
                    )
            offset += batch_size
            if len(notices) < batch_size:
                break
    return {
        "ok": True,
        "days": days,
        "pub_from_utc": pub_from,
        "window_min": pp_eis_link_window_min(),
        "notices_processed": notices_processed,
        "matches_saved": matches_saved,
    }


def _pp_near_any_notice(
    pp_link_date: str,
    notice_pubs: list[str],
    *,
    window_min: float,
) -> bool:
    for notice_pub in notice_pubs:
        lag = compute_lag_minutes(notice_pub, pp_link_date)
        if lag_within_window(lag, window_min):
            return True
    return False


def _fetch_missing_pp_findings(
    db_path: Path,
    *,
    meta_dir: Path,
    ktru_code: str,
    link_from_utc: str,
    notice_pubs: list[str],
    cookie: str,
    fetch_delay_sec: float = 0.25,
    max_fetch: int | None = None,
) -> dict[str, Any]:
    from eis_analytics_db import list_pp_monitor_registry_link_window, upsert_pp_monitor_finding

    window = pp_eis_link_window_min()
    candidates = list_pp_monitor_registry_link_window(
        db_path,
        ktru_codes=[ktru_code],
        link_from_utc=link_from_utc,
        missing_find_only=True,
    )
    to_fetch: list[dict[str, Any]] = []
    for row in candidates:
        link_date = str(row.get("link_date") or "").strip()
        if not link_date:
            continue
        if notice_pubs and not _pp_near_any_notice(link_date, notice_pubs, window_min=window):
            continue
        to_fetch.append(row)
    if max_fetch is not None:
        to_fetch = to_fetch[: max(0, int(max_fetch))]

    from rts_customer_pp_api import RtsCustomerPpApiError, find_offer_item

    fetched = 0
    errors = 0
    import time

    for row in to_fetch:
        oid = str(row.get("offer_price_list_item_id") or "").strip()
        if not oid:
            continue
        try:
            find_item = find_offer_item(cookie, oid)
        except RtsCustomerPpApiError:
            errors += 1
            continue
        except Exception:
            errors += 1
            continue
        if not isinstance(find_item, dict) or not find_item:
            errors += 1
            continue
        ktru_obj = find_item.get("ktru") if isinstance(find_item.get("ktru"), dict) else {}
        link_date = str(row.get("link_date") or find_item.get("createDateTime") or "").strip()
        upsert_pp_monitor_finding(
            db_path,
            offer_price_list_item_id=oid,
            find_json=json.dumps(find_item, ensure_ascii=False),
            ktru_code=str(ktru_obj.get("code") or row.get("ktru_code") or ktru_code),
            trade_mark=str(find_item.get("tradeMark") or row.get("trade_mark") or ""),
            link_date=link_date,
            unit_price=row.get("unit_price"),
            card_url=str(row.get("card_url") or ""),
            source="historical_scan",
            trigger_recompute=False,
        )
        fetched += 1
        if fetch_delay_sec > 0:
            time.sleep(fetch_delay_sec)
    return {
        "ktru_code": ktru_code,
        "registry_candidates": len(candidates),
        "date_prefiltered": len(to_fetch),
        "fetched": fetched,
        "errors": errors,
    }


def scan_historical_pp_eis_links(
    db_path: Path,
    *,
    meta_dir: Path,
    days: int = 30,
    history_path: Path | None = None,
    batch_size: int = 200,
    cookie: str | None = None,
    fetch_delay_sec: float = 0.25,
    max_fetch_per_ktru: int | None = None,
) -> dict[str, Any]:
    """Historical scan: registry without price filter, exact match + ±window, match_origin=historical."""
    if not pp_eis_link_enabled():
        return {"ok": False, "reason": "disabled"}
    window = pp_eis_link_window_min()
    pub_from_dt = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    pub_from = _utc_iso(pub_from_dt)
    link_from = _utc_iso(pub_from_dt - timedelta(hours=2))
    notices_processed = 0
    matches_saved = 0
    fetch_stats: list[dict[str, Any]] = []
    customer_cookie = str(cookie or "").strip()
    cookie_probe: dict[str, Any] = {}
    if not customer_cookie:
        try:
            from rts_customer_pp_api import load_customer_cookie

            customer_cookie = load_customer_cookie(meta_dir)
        except Exception as exc:
            cookie_probe = {"ok": False, "reason": "load_failed", "detail": str(exc)[:300]}
    if customer_cookie:
        try:
            from rts_customer_pp_api import probe_customer_session

            cookie_probe = probe_customer_session(customer_cookie, meta_dir=meta_dir)
        except Exception as exc:
            cookie_probe = {"ok": False, "reason": "probe_failed", "detail": str(exc)[:300]}
    fetch_enabled = bool(customer_cookie) and bool(cookie_probe.get("ok"))

    for ktru_code in sorted(pilot_ktru_codes()):
        notice_pubs: list[str] = []
        offset = 0
        while True:
            notices = offer_matching_ktru_rows(
                db_path,
                limit=batch_size,
                offset=offset,
                catalog_ktru_codes=[ktru_code],
                pub_from_utc=pub_from,
            )
            if not notices:
                break
            for notice in notices:
                pub = _notice_pub_iso(notice)
                if pub:
                    notice_pubs.append(pub)
            offset += batch_size
            if len(notices) < batch_size:
                break

        if customer_cookie and fetch_enabled:
            fetch_stats.append(
                _fetch_missing_pp_findings(
                    db_path,
                    meta_dir=meta_dir,
                    ktru_code=ktru_code,
                    link_from_utc=link_from,
                    notice_pubs=notice_pubs,
                    cookie=customer_cookie,
                    fetch_delay_sec=fetch_delay_sec,
                    max_fetch=max_fetch_per_ktru,
                )
            )

        offset = 0
        while True:
            notices = offer_matching_ktru_rows(
                db_path,
                limit=batch_size,
                offset=offset,
                catalog_ktru_codes=[ktru_code],
                pub_from_utc=pub_from,
            )
            if not notices:
                break
            for notice in notices:
                pn = str(notice.get("purchase_number") or "").strip()
                lk = str(notice.get("line_key") or "").strip()
                if not pn or not lk:
                    continue
                notices_processed += 1
                matches_saved += recompute_matches_for_notice(
                    db_path,
                    meta_dir=meta_dir,
                    purchase_number=pn,
                    line_key=lk,
                    ktru_code=ktru_code,
                    html_chars=notice.get("characteristics") or [],
                    history_path=history_path,
                    notice_pub_date_utc=_notice_pub_iso(notice),
                    match_origin="historical",
                )
            offset += batch_size
            if len(notices) < batch_size:
                break

    append_pp_eis_event(
        db_path,
        event_type="historical_scan_completed",
        payload={
            "days": days,
            "pub_from_utc": pub_from,
            "window_min": window,
            "notices_processed": notices_processed,
            "matches_saved": matches_saved,
            "fetch_stats": fetch_stats,
        },
    )
    return {
        "ok": True,
        "days": days,
        "pub_from_utc": pub_from,
        "link_from_utc": link_from,
        "window_min": window,
        "notices_processed": notices_processed,
        "matches_saved": matches_saved,
        "fetch_stats": fetch_stats,
        "cookie_probe": cookie_probe,
        "fetch_enabled": fetch_enabled,
    }


def maybe_recompute_after_pp_finding(
    db_path: Path,
    *,
    meta_dir: Path,
    offer_price_list_item_id: str,
    ktru_code: str,
    find_json: str,
    link_date: str = "",
    characteristics_hash: str = "",
    history_path: Path | None = None,
) -> None:
    try:
        parsed = json.loads(find_json) if find_json else {}
        find_item = parsed if isinstance(parsed, dict) else {}
        recompute_matches_for_pp(
            db_path,
            meta_dir=meta_dir,
            offer_price_list_item_id=offer_price_list_item_id,
            ktru_code=ktru_code,
            find_item=find_item,
            link_date=link_date,
            characteristics_hash=characteristics_hash,
            history_path=history_path,
        )
    except Exception:
        return


def maybe_recompute_after_notice_html(
    db_path: Path,
    *,
    meta_dir: Path,
    purchase_number: str,
    lines: list[Any],
    history_path: Path | None = None,
    pub_date_utc: str = "",
) -> None:
    """HTML ingest hook — PP matching runs from offer-matching UI, not here."""
    del meta_dir, history_path, pub_date_utc, lines, purchase_number, db_path
    return


def backfill_eis_outcomes(
    db_path: Path,
    *,
    days: int = 30,
    pub_from_utc: str | None = None,
    pub_to_utc: str | None = None,
    batch_size: int = 200,
    force_refresh: bool = False,
    purchase_number: str | None = None,
    fetch_timeout: float = 30.0,
) -> dict[str, Any]:
    """Refresh notice_eis_outcome from EIS journal (+ supplier-results for winners)."""
    pn_filter = str(purchase_number or "").strip()
    if pub_from_utc:
        pub_from = str(pub_from_utc).strip()
        pub_to = str(pub_to_utc or "").strip() or None
    else:
        pub_from = _utc_iso(datetime.now(timezone.utc) - timedelta(days=max(1, int(days))))
        pub_to = None
    processed = 0
    updated = 0
    by_outcome: dict[str, int] = {}
    errors = 0
    for ktru_code in sorted(pilot_ktru_codes()):
        offset = 0
        while True:
            notices = offer_matching_ktru_rows(
                db_path,
                limit=batch_size,
                offset=offset,
                catalog_ktru_codes=[ktru_code],
                pub_from_utc=pub_from if not pn_filter else None,
                pub_to_utc=pub_to if not pn_filter else None,
                purchase_number=pn_filter or None,
            )
            if not notices:
                break
            for notice in notices:
                pn = str(notice.get("purchase_number") or "").strip()
                lk = str(notice.get("line_key") or "").strip()
                if not pn or not lk:
                    continue
                processed += 1
                before = get_notice_eis_outcome(db_path, purchase_number=pn, line_key=lk)
                before_code = str((before or {}).get("eis_outcome") or "")
                result = refresh_notice_eis_outcome(
                    db_path,
                    purchase_number=pn,
                    line_key=lk,
                    ktru_code=str(notice.get("ktru_code") or ktru_code),
                    quantity_text=str(notice.get("quantity_text") or ""),
                    fetch_timeout=fetch_timeout,
                    force_refresh=force_refresh,
                )
                after_code = str(result.get("eis_outcome") or "")
                by_outcome[after_code] = by_outcome.get(after_code, 0) + 1
                if not result.get("ok"):
                    errors += 1
                elif after_code != before_code:
                    updated += 1
            offset += batch_size
            if len(notices) < batch_size:
                break
        if pn_filter:
            break
    return {
        "ok": True,
        "days": days,
        "pub_from_utc": pub_from,
        "pub_to_utc": pub_to,
        "purchase_number": pn_filter,
        "force_refresh": force_refresh,
        "processed": processed,
        "updated": updated,
        "errors": errors,
        "by_outcome": by_outcome,
    }


def backfill_eis_winner_price_links(
    db_path: Path,
    *,
    meta_dir: Path,
    days: int = 30,
    history_path: Path | None = None,
    batch_size: int = 200,
    force_refresh: bool = False,
    customer_cookie: str | None = None,
) -> dict[str, Any]:
    """Backfill supplier-results fetch and winner-price matches for pilot notices."""
    if not pp_eis_link_enabled() or not pp_eis_winner_price_enabled():
        return {"ok": False, "reason": "disabled", "notices_processed": 0, "matches_saved": 0}
    pub_from = _utc_iso(datetime.now(timezone.utc) - timedelta(days=max(1, int(days))))
    notices_processed = 0
    matches_saved = 0
    cookie = str(customer_cookie or "").strip()
    for ktru_code in sorted(pilot_ktru_codes()):
        offset = 0
        while True:
            notices = offer_matching_ktru_rows(
                db_path,
                limit=batch_size,
                offset=offset,
                catalog_ktru_codes=[ktru_code],
                pub_from_utc=pub_from,
            )
            if not notices:
                break
            for notice in notices:
                pn = str(notice.get("purchase_number") or "").strip()
                lk = str(notice.get("line_key") or "").strip()
                if not pn or not lk:
                    continue
                notices_processed += 1
                matches_saved += match_notice_by_winner_price(
                    db_path,
                    meta_dir=meta_dir,
                    purchase_number=pn,
                    line_key=lk,
                    ktru_code=ktru_code,
                    html_chars=notice.get("characteristics") or [],
                    quantity_text=str(notice.get("quantity_text") or ""),
                    history_path=history_path,
                    notice_pub_date_utc=_notice_pub_iso(notice),
                    force_refresh=force_refresh,
                    customer_cookie=cookie or None,
                )
            offset += batch_size
            if len(notices) < batch_size:
                break
    return {
        "ok": True,
        "days": days,
        "pub_from_utc": pub_from,
        "notices_processed": notices_processed,
        "matches_saved": matches_saved,
    }


def _cli_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="EIS-PP link hypothesis backfill")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--meta-dir", type=str, default=os.environ.get("RTS_META_DIR", ""))
    parser.add_argument("--db", type=str, default="")
    parser.add_argument("--history-db", type=str, default=os.environ.get("EIS_HISTORY_DB", ""))
    args = parser.parse_args()
    meta = Path(args.meta_dir) if args.meta_dir else analytics_db_path().parent
    db = Path(args.db) if args.db else analytics_db_path()
    history = Path(args.history_db) if args.history_db and Path(args.history_db).is_file() else None
    result = backfill_pp_eis_links(db, meta_dir=meta, days=args.days, history_path=history)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"window_min=±{pp_eis_link_window_min()}")


if __name__ == "__main__":
    _cli_main()

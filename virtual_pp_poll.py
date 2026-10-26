"""Match the virtual PP set to EIS HTML-monitor notices (per KTRU)."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pp_core_catalog import resolve_ktru_template
from region_aliases import resolve_region
from rts_cookie_ingest import cookie_has_auth_markers
from rts_invites_poll import load_cookie_header
from rts_lot_items import parse_lot_items_search_paged_payload
from rts_pp_application_api import (
    ProcedureBundle,
    RtsPpApplicationApiError,
    open_lk_session_for_ezt,
    parse_auction_search_response,
    parse_procedure_summary,
    resolve_procedure,
    search_ezt_by_ktru,
)
from pp_virtual_queue import list_drafted_virtual_variants_for_match
from virtual_pp_match import lot_item_chars_preview, match_eis_html_line, match_lot_item
from virtual_pp_store import (
    list_eis_monitor_ktru_lines,
    list_virtual_pp_ktru_codes,
    list_virtual_pp_variants,
    record_unknown_region,
    replace_rts_ezt_lot_items,
    upsert_rts_ezt_notice,
    upsert_virtual_match_event,
)

_LOG = logging.getLogger("virtual_pp_poll")
_PUB_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
DEFAULT_WINDOW_DAYS = 7
DEFAULT_PAUSE_SEC = 0.25


def parse_publication_date_string(raw: str) -> str:
    match = _PUB_DATE_RE.search(str(raw or ""))
    if not match:
        return ""
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"


def publication_from_iso(*, days: int = DEFAULT_WINDOW_DAYS) -> str:
    window = max(1, min(int(days or DEFAULT_WINDOW_DAYS), 90))
    start = datetime.now(timezone.utc) - timedelta(days=window)
    return start.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def load_supplier_cookie_file(path: Path) -> str:
    if not path.is_file():
        raise RtsPpApplicationApiError(f"Файл cookie не найден: {path}.", reason="no_cookie")
    cookie = load_cookie_header(path).strip()
    if not cookie or not cookie_has_auth_markers(cookie):
        raise RtsPpApplicationApiError("В cookie нет сессии РТС. Обновите cookie.", reason="no_cookie")
    return cookie


def match_ezt_search_rows(
    analytics_db: Path,
    code: str,
    rows: list[Any],
    *,
    cache_dir: Path | None = None,
    pause_sec: float = 0,
    qty_risk_threshold: float = 0,
    fetch_bundle: Any,
) -> dict[str, Any]:
    variants = list_virtual_pp_variants(analytics_db, code)
    if not variants:
        return {"notices": 0, "matched": 0, "errors": [f"{code}: нет виртуального набора"]}
    set_id = str(variants[0].get("set_id") or "")
    template = resolve_ktru_template(code, cache_dir=cache_dir)
    errors: list[str] = []
    notices_n = 0
    matched_n = 0
    for row in rows:
        notices_n += 1
        region = resolve_region(row.region)
        if not region.mapped:
            record_unknown_region(analytics_db, row.region)
        pub_raw = row.publication_date
        upsert_rts_ezt_notice(
            analytics_db,
            {
                "notification_number": row.notification_number,
                "ktru_code": code,
                "region_raw": row.region,
                "subject_oktmo": region.subject_oktmo,
                "nmck": row.start_price,
                "url_trade_id": row.url_trade_id,
                "lot_state": row.lot_state,
                "lot_state_label": row.lot_state_label,
                "participant_has_active_application": row.participant_has_active_application,
                "publication_date": parse_publication_date_string(pub_raw),
                "publication_date_raw": pub_raw,
                "trade_name": row.trade_name,
            },
        )
        try:
            bundle = fetch_bundle(row)
        except (RtsPpApplicationApiError, RuntimeError) as exc:
            errors.append(f"{row.notification_number}: procedure {exc}")
            continue
        procedure = bundle.procedure if bundle is not None else None
        if procedure is None:
            reason = getattr(bundle, "skip_reason", "") if bundle is not None else ""
            errors.append(f"{row.notification_number}: {reason or 'procedure_parse_failed'}")
            continue
        upsert_rts_ezt_notice(
            analytics_db,
            {
                "notification_number": row.notification_number,
                "ktru_code": code,
                "region_raw": row.region,
                "subject_oktmo": region.subject_oktmo,
                "nmck": procedure.nmck_total if procedure.nmck_total is not None else row.start_price,
                "url_trade_id": row.url_trade_id,
                "numeric_trade_id": procedure.numeric_trade_id,
                "lot_state": row.lot_state,
                "lot_state_label": row.lot_state_label,
                "participant_has_active_application": row.participant_has_active_application,
                "publication_date": parse_publication_date_string(pub_raw),
                "publication_date_raw": pub_raw,
                "trade_name": row.trade_name or procedure.purchase_name,
            },
        )
        items = parse_lot_items_search_paged_payload(bundle.lot_items or {})
        stored_items: list[dict[str, Any]] = []
        for idx, item in enumerate(items):
            stored_items.append(
                {
                    "line_index": idx,
                    "ktru_code": item.classification_code or code,
                    "quantity": _ru_float(item.quantity_text),
                    "unit_price": _ru_float(item.price_amount),
                    "total": _ru_float(item.total_amount),
                    "characteristics": lot_item_chars_preview(item),
                    "ktru_version": item.ktru_version_number,
                }
            )
            event = match_lot_item(
                item,
                variants,
                template,
                region_raw=row.region,
                nmck=row.start_price,
                own_application=row.participant_has_active_application,
                qty_risk_threshold=qty_risk_threshold,
                delivery_days=None,
            )
            upsert_virtual_match_event(
                analytics_db,
                {
                    **event,
                    "notification_number": row.notification_number,
                    "line_index": idx,
                    "ktru_code": item.classification_code or code,
                    "set_id": set_id,
                },
            )
            matched_n += 1
        replace_rts_ezt_lot_items(analytics_db, row.notification_number, stored_items)
        if pause_sec:
            time.sleep(max(0.0, float(pause_sec)))
    return {"notices": notices_n, "matched": matched_n, "errors": errors}


def ingest_ezt_lk_dump(
    analytics_db: Path,
    *,
    ktru_code: str,
    search_payload: dict[str, Any],
    procedures: dict[str, Any] | None = None,
    lot_items: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    qty_risk_threshold: float = 0,
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    if not code:
        raise ValueError("ktru_code не задан.")
    rows, total = parse_auction_search_response(search_payload)
    proc_map = procedures if isinstance(procedures, dict) else {}
    lot_map = lot_items if isinstance(lot_items, dict) else {}

    def fetch_bundle(row: Any) -> ProcedureBundle:
        raw = proc_map.get(row.url_trade_id) or proc_map.get(row.notification_number) or {}
        procedure = parse_procedure_summary(raw) if isinstance(raw, dict) and raw else None
        items_raw = None
        if procedure is not None:
            items_raw = lot_map.get(str(procedure.numeric_trade_id)) or lot_map.get(row.url_trade_id)
        if items_raw is None:
            items_raw = lot_map.get(row.notification_number)
        return ProcedureBundle(
            procedure=procedure,
            lot_items=items_raw if isinstance(items_raw, dict) else None,
            skip_reason="" if procedure is not None else "no_procedure",
        )

    stats = match_ezt_search_rows(
        analytics_db,
        code,
        rows,
        cache_dir=cache_dir,
        qty_risk_threshold=qty_risk_threshold,
        fetch_bundle=fetch_bundle,
    )
    return {
        "ok": True,
        "ktru_code": code,
        "source": "browser",
        "search_total": total,
        **stats,
        "errors": stats.get("errors", [])[:20],
    }


def _iso_date(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    parsed = parse_publication_date_string(text)
    return parsed


def poll_virtual_pp(
    analytics_db: Path,
    cookie: str = "",
    *,
    ktru_code: str = "",
    days: int = DEFAULT_WINDOW_DAYS,
    cache_dir: Path | None = None,
    pause_sec: float = DEFAULT_PAUSE_SEC,
    qty_risk_threshold: float = 0,
    template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Match drafted virtual PP variants to EIS HTML-monitor lines. cookie unused."""
    del cookie, pause_sec
    codes = [str(ktru_code).strip()] if str(ktru_code or "").strip() else list_virtual_pp_ktru_codes(analytics_db)
    codes = [c for c in codes if c]
    if not codes:
        return {"ok": True, "ktru_codes": [], "notices": 0, "matched": 0, "errors": [], "source": "eis"}
    errors: list[str] = []
    notices_n = 0
    matched_n = 0
    for code in codes:
        variants = list_drafted_virtual_variants_for_match(analytics_db, code)
        if not variants:
            variants = list_virtual_pp_variants(analytics_db, code)
        if not variants:
            errors.append(f"{code}: нет виртуального набора — сначала соберите набор из ядра")
            continue
        set_id = str(variants[0].get("set_id") or "")
        resolved = template if isinstance(template, dict) and template else resolve_ktru_template(
            code, cache_dir=cache_dir
        )
        lines = list_eis_monitor_ktru_lines(analytics_db, code, days=days)
        by_notice: dict[str, list[dict[str, Any]]] = {}
        for line in lines:
            pn = str(line.get("purchase_number") or "").strip()
            if pn:
                by_notice.setdefault(pn, []).append(line)
        if not by_notice:
            errors.append(f"{code}: в мониторе ЕИС нет HTML-позиций за {days} дн.")
            continue
        for pn, group in by_notice.items():
            notices_n += 1
            first = group[0]
            region_raw = str(first.get("region") or "")
            region = resolve_region(region_raw)
            if not region.mapped:
                record_unknown_region(analytics_db, region_raw)
            pub = _iso_date(str(first.get("observed_at_utc") or ""))
            nmck = _ru_float(first.get("price_text")) or _ru_float(first.get("line_sum_text"))
            upsert_rts_ezt_notice(
                analytics_db,
                {
                    "notification_number": pn,
                    "ktru_code": code,
                    "region_raw": region_raw,
                    "subject_oktmo": region.subject_oktmo,
                    "nmck": nmck,
                    "publication_date": pub,
                    "publication_date_raw": str(first.get("observed_at_utc") or ""),
                    "trade_name": str(first.get("position_display_name") or ""),
                    "lot_state_label": str(first.get("etp_name") or "ЕИС"),
                },
            )
            stored_items: list[dict[str, Any]] = []
            for idx, line in enumerate(group):
                try:
                    line_index = int(line.get("position_index") or idx)
                except (TypeError, ValueError):
                    line_index = idx
                qty = _ru_float(line.get("quantity_text"))
                unit_price = _ru_float(line.get("price_per_unit_text"))
                total = _ru_float(line.get("line_sum_text"))
                chars = line.get("characteristics") if isinstance(line.get("characteristics"), list) else []
                event = match_eis_html_line(
                    chars,
                    variants,
                    resolved,
                    region_raw=str(line.get("region") or region_raw),
                    nmck=nmck,
                    quantity=qty,
                    unit_price=unit_price,
                    total=total,
                    qty_risk_threshold=qty_risk_threshold,
                )
                stored_items.append(
                    {
                        "line_index": line_index,
                        "ktru_code": str(line.get("ktru_code") or code),
                        "quantity": qty,
                        "unit_price": unit_price,
                        "total": total,
                        "characteristics": event.get("characteristics") or [],
                    }
                )
                upsert_virtual_match_event(
                    analytics_db,
                    {
                        **event,
                        "notification_number": pn,
                        "line_index": line_index,
                        "ktru_code": str(line.get("ktru_code") or code),
                        "set_id": set_id,
                    },
                )
                matched_n += 1
            replace_rts_ezt_lot_items(analytics_db, pn, stored_items)
    return {
        "ok": True,
        "ktru_codes": codes,
        "notices": notices_n,
        "matched": matched_n,
        "errors": errors[:20],
        "source": "eis",
        "days": days,
    }


def _ru_float(value: object) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().replace("\xa0", " ").replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None

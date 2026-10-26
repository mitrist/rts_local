"""Poll RTS customer registry for new competitor preliminary offers."""

from __future__ import annotations

import json
import logging
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from eis_analytics_db import (
    display_compare_datetime_msk,
    list_known_pp_monitor_ids,
    record_pp_monitor_notification,
    upsert_pp_monitor_finding,
    upsert_pp_monitor_registry_row,
)
from notify import html_bold, html_link, markdown_bold, markdown_link
from rts_customer_pp_api import (
    RegistryOfferSummary,
    RtsCustomerPpApiError,
    build_search_payload,
    find_offer_item,
    load_customer_cookie,
    parse_search_summary,
    probe_customer_session,
    search_registry_page,
)
from rts_pp_monitor_settings import (
    backfill_ktru_delay_sec,
    backfill_page_delay_sec,
    backfill_pages_per_run,
    effective_ktru_codes,
    load_settings,
    notify_find_enabled,
    passes_monitor_filters,
    publish_date_from_watermark,
    registry_backfill_state,
    save_settings,
    search_filter_overrides,
    search_take_from_env,
    settings_path,
    update_link_date_watermark,
)

CompareOfferSummary = RegistryOfferSummary

_LOG = logging.getLogger("rts_pp_monitor")

TELEGRAM_PP_MONITOR_CHANNEL = "telegram_rts_pp_monitor"
MAX_PP_MONITOR_CHANNEL = "max_rts_pp_monitor"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _max_per_run() -> int:
    try:
        return max(1, min(int(os.environ.get("RTS_PP_MONITOR_MAX_PER_RUN", "20")), 200))
    except ValueError:
        return 20


def _full_pages_per_poll() -> bool:
    """When true, paginate through all registry pages even after notify limit is reached."""
    raw = os.environ.get("RTS_PP_MONITOR_FULL_PAGES", "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _search_take(settings: dict[str, Any]) -> int:
    try:
        return max(1, min(int(settings.get("search_take") or search_take_from_env()), 100))
    except (TypeError, ValueError):
        return search_take_from_env()


def _seed_channels() -> list[tuple[str, str]]:
    return [
        (TELEGRAM_PP_MONITOR_CHANNEL, "Telegram"),
        (MAX_PP_MONITOR_CHANNEL, "MAX"),
    ]


def pp_monitor_notify_message(
    summary: RegistryOfferSummary | dict[str, Any],
    find_item: dict[str, Any] | None,
    *,
    links: str,
) -> str:
    del find_item
    if isinstance(summary, RegistryOfferSummary):
        row = summary.as_dict()
    else:
        row = summary
    link_fn = html_link if links == "html" else markdown_link
    bold = html_bold if links == "html" else markdown_bold
    ktru = str(row.get("ktru_code") or "").strip()
    name = str(row.get("name") or "").strip()
    link_raw = str(row.get("link_date") or "").strip()
    link_display = display_compare_datetime_msk(link_raw) if link_raw else ""
    regions = row.get("delivery_regions_count")
    unit_price = row.get("unit_price")
    card_url = str(row.get("card_url") or "").strip()
    lines = [
        bold("Новая запись в реестре ПП"),
        "",
        f"КТРУ: {ktru}" if ktru else "",
        f"Наименование: {name}" if name else "",
        f"Дата добавления: {link_display}" if link_display else "",
    ]
    if unit_price is not None:
        try:
            lines.append(f"Цена за ед.: {float(unit_price):g}")
        except (TypeError, ValueError):
            pass
    if regions is not None:
        lines.append(f"Кол-во регионов поставки: {regions}")
    lines = [x for x in lines if x]
    if card_url:
        lines.append(link_fn(card_url, "Карточка ПП на РТС"))
    return "\n".join(lines)


def _save_registry_row(db_path: Path, summary: RegistryOfferSummary) -> None:
    upsert_pp_monitor_registry_row(
        db_path,
        offer_price_list_item_id=summary.offer_price_list_item_id,
        ktru_code=summary.ktru_code,
        name=summary.name,
        trade_mark=summary.trade_mark,
        link_date=summary.link_date,
        validity_date=summary.validity_date,
        unit_price=summary.unit_price,
        delivery_regions_count=summary.delivery_regions_count,
        card_url=summary.card_url,
        price_list_item_id=summary.price_list_item_id,
    )


def _save_finding(
    db_path: Path,
    summary: RegistryOfferSummary,
    find_item: dict[str, Any],
    *,
    source: str = "notify",
    trigger_recompute: bool = True,
) -> None:
    upsert_pp_monitor_finding(
        db_path,
        offer_price_list_item_id=summary.offer_price_list_item_id,
        find_json=json.dumps(find_item, ensure_ascii=False),
        ktru_code=summary.ktru_code,
        trade_mark=summary.trade_mark,
        link_date=summary.link_date,
        unit_price=summary.unit_price,
        card_url=summary.card_url,
        source=source,
        trigger_recompute=trigger_recompute,
    )


def _record_channels(
    db_path: Path,
    summary: RegistryOfferSummary,
    channels: list[tuple[str, str]],
    *,
    status: str,
    error_text: str = "",
) -> None:
    for channel, _label in channels:
        record_pp_monitor_notification(
            db_path,
            offer_price_list_item_id=summary.offer_price_list_item_id,
            ktru_code=summary.ktru_code,
            channel=channel,
            link_date=summary.link_date,
            unit_price=summary.unit_price,
            trade_mark=summary.trade_mark,
            card_url=summary.card_url,
            status=status,
            error_text=error_text,
        )


def _notify_summary(
    summary: RegistryOfferSummary,
    find_item: dict[str, Any] | None,
    *,
    db_path: Path,
    settings: dict[str, Any],
    senders: list[tuple[str, str, Callable[..., None], str, str, float, str]],
) -> tuple[int, int]:
    sent = 0
    failed = 0
    for channel, label, send_long, token, chat_id, timeout, message_links in senders:
        if channel == TELEGRAM_PP_MONITOR_CHANNEL and not settings.get("notify_telegram", True):
            continue
        if channel == MAX_PP_MONITOR_CHANNEL and not settings.get("notify_max", True):
            continue
        if not token or not chat_id:
            continue
        message = pp_monitor_notify_message(summary, find_item, links=message_links)
        try:
            send_long(message, token, chat_id, timeout)
            record_pp_monitor_notification(
                db_path,
                offer_price_list_item_id=summary.offer_price_list_item_id,
                ktru_code=summary.ktru_code,
                channel=channel,
                link_date=summary.link_date,
                unit_price=summary.unit_price,
                trade_mark=summary.trade_mark,
                card_url=summary.card_url,
                status="sent",
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            record_pp_monitor_notification(
                db_path,
                offer_price_list_item_id=summary.offer_price_list_item_id,
                ktru_code=summary.ktru_code,
                channel=channel,
                link_date=summary.link_date,
                unit_price=summary.unit_price,
                trade_mark=summary.trade_mark,
                card_url=summary.card_url,
                status="error",
                error_text=str(exc),
            )
            _LOG.warning("%s: notify failed for %s: %s", label, summary.offer_price_list_item_id, exc)
    return sent, failed


def _note_search_error(result: dict[str, Any], *, ktru_code: str, exc: RtsCustomerPpApiError) -> bool:
    """Record API error; return True when the whole poll should stop (expired session)."""
    _LOG.warning("PP monitor search failed for %s: %s", ktru_code, exc)
    result["ok"] = False
    result["reason"] = str(exc.reason or "api_error")
    result.setdefault("errors", []).append({"ktru_code": ktru_code, "error": str(exc)})
    return exc.reason == "session_expired"


def _probe_customer_cookie(cookie: str, ktru_code: str) -> dict[str, Any] | None:
    probe = probe_customer_session(cookie, ktru_code=ktru_code)
    if probe.get("ok"):
        return None
    reason = str(probe.get("reason") or "probe_failed")
    detail = str(probe.get("detail") or "Customer cookie probe failed")
    return {"ok": False, "reason": reason, "error": detail, "skipped": False}


def _build_search_payload_for_settings(
    settings: dict[str, Any],
    ktru_code: str,
    *,
    skip: int,
    take: int,
    publish_date_from: str = "",
) -> dict[str, Any]:
    overrides = search_filter_overrides(settings)
    return build_search_payload(
        ktru_code,
        skip=skip,
        take=take,
        price_from=overrides.get("price_from"),
        price_to=overrides.get("price_to"),
        validity_date_from=str(overrides.get("validity_date_from") or ""),
        publish_date_from=publish_date_from,
    )


def _update_watermark_from_summaries(
    settings: dict[str, Any],
    ktru_code: str,
    summaries: list[RegistryOfferSummary],
) -> dict[str, Any]:
    updated = settings
    for summary in summaries:
        if summary.link_date:
            updated = update_link_date_watermark(updated, ktru_code, summary.link_date)
    return updated


def _page_fully_known(summaries: list[RegistryOfferSummary], known_global: set[str]) -> bool:
    page_ids = {s.offer_price_list_item_id for s in summaries}
    return bool(page_ids) and page_ids.issubset(known_global)


def _persist_registry_page(
    db_path: Path,
    summaries: list[RegistryOfferSummary],
) -> None:
    for summary in summaries:
        _save_registry_row(db_path, summary)


def run_rts_pp_monitor_backfill(
    meta_dir: Path,
    db_path: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    settings_path_obj = settings_path(meta_dir)
    settings = load_settings(settings_path_obj)
    result: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "reason": "",
        "ktru_polled": 0,
        "pages_fetched": 0,
        "items_seen": 0,
        "seeded": 0,
        "registry_backfill_done": bool(settings.get("registry_backfill_done")),
    }
    ktru_codes = effective_ktru_codes(settings)
    if not ktru_codes:
        result["skipped"] = True
        result["reason"] = "no_ktru"
        return result
    if settings.get("registry_backfill_done") and not force:
        result["skipped"] = True
        result["reason"] = "backfill_done"
        return result
    try:
        cookie = load_customer_cookie(meta_dir)
    except RtsCustomerPpApiError as exc:
        result["ok"] = False
        result["reason"] = exc.reason
        result["error"] = str(exc)
        return result

    probe_fail = _probe_customer_cookie(cookie, ktru_codes[0])
    if probe_fail is not None:
        return {**result, **probe_fail}

    take = _search_take(settings)
    page_delay = backfill_page_delay_sec()
    ktru_delay = backfill_ktru_delay_sec()
    pages_limit = backfill_pages_per_run()
    known_global = list_known_pp_monitor_ids(db_path)
    updated_settings = deepcopy(settings)
    backfill_map = dict(updated_settings.get("registry_backfill") or {})

    session_aborted = False
    for ktru_code in ktru_codes:
        if session_aborted:
            break
        state = registry_backfill_state(updated_settings, ktru_code)
        if state.get("completed") and not force:
            continue
        skip = int(state.get("skip") or 0)
        total = state.get("total")
        pages_this_ktru = 0
        max_link = str((updated_settings.get("link_date_watermarks") or {}).get(ktru_code) or "")

        while True:
            if pages_limit > 0 and pages_this_ktru >= pages_limit:
                break
            payload = _build_search_payload_for_settings(updated_settings, ktru_code, skip=skip, take=take)
            try:
                summaries, page_total = search_registry_page(cookie, payload)
            except RtsCustomerPpApiError as exc:
                if _note_search_error(result, ktru_code=ktru_code, exc=exc):
                    session_aborted = True
                break
            result["ktru_polled"] += 1 if pages_this_ktru == 0 else 0
            result["pages_fetched"] += 1
            pages_this_ktru += 1
            result["items_seen"] += len(summaries)
            if total is None:
                total = page_total
            if not summaries:
                state["completed"] = True
                break
            for summary in summaries:
                _save_registry_row(db_path, summary)
                if summary.link_date and summary.link_date > max_link:
                    max_link = summary.link_date
                oid = summary.offer_price_list_item_id
                if oid not in known_global:
                    _record_channels(db_path, summary, _seed_channels(), status="seeded")
                    known_global.add(oid)
                    result["seeded"] += 1
            skip += take
            state["skip"] = skip
            state["total"] = total
            if skip >= int(total or 0):
                state["completed"] = True
                break
            if page_delay > 0:
                time.sleep(page_delay)

        if max_link:
            updated_settings = update_link_date_watermark(updated_settings, ktru_code, max_link)
        backfill_map[ktru_code] = state
        updated_settings["registry_backfill"] = backfill_map
        save_settings(settings_path_obj, updated_settings)
        if ktru_delay > 0:
            time.sleep(ktru_delay)

    all_done = all(
        isinstance(backfill_map.get(code), dict) and bool(backfill_map[code].get("completed"))
        for code in ktru_codes
    )
    if all_done:
        updated_settings = load_settings(settings_path_obj)
        updated_settings["registry_backfill_done"] = True
        updated_settings["initial_seed_done"] = True
        save_settings(settings_path_obj, updated_settings)
        result["registry_backfill_done"] = True
    _LOG.info(
        "PP monitor backfill: pages=%s items=%s seeded=%s done=%s",
        result["pages_fetched"],
        result["items_seen"],
        result["seeded"],
        result["registry_backfill_done"],
    )
    return result


def _process_new_summary(
    summary: RegistryOfferSummary,
    *,
    db_path: Path,
    meta_dir: Path,
    settings: dict[str, Any],
    cookie: str,
    known_global: set[str],
    senders: list[tuple[str, str, Callable[..., None], str, str, float, str]] | None,
    processed_new: int,
    max_per_run: int,
    result: dict[str, Any],
) -> int:
    oid = summary.offer_price_list_item_id
    if oid in known_global:
        return processed_new
    _save_registry_row(db_path, summary)
    result["items_new"] += 1
    if not passes_monitor_filters(summary.as_dict(), settings):
        _record_channels(db_path, summary, _seed_channels(), status="seeded")
        known_global.add(oid)
        return processed_new
    if processed_new >= max_per_run:
        return processed_new
    find_item: dict[str, Any] | None = None
    need_find = bool(notify_find_enabled() or senders)
    if not need_find:
        try:
            from auto_monitor_draft import auto_monitor_draft_env_enabled

            need_find = auto_monitor_draft_env_enabled()
        except Exception:
            need_find = False
    if need_find:
        try:
            find_item = find_offer_item(cookie, oid)
        except RtsCustomerPpApiError as exc:
            _LOG.warning("PP monitor find failed for %s: %s", oid, exc)
            result.setdefault("errors", []).append({"offer_id": oid, "error": str(exc)})
            return processed_new
    if find_item:
        _save_finding(db_path, summary, find_item, source="notify")
        try:
            from auto_monitor_draft import try_enqueue_from_finding

            if try_enqueue_from_finding(
                db_path,
                meta_dir,
                offer_price_list_item_id=oid,
                find_item=find_item,
                ktru_code=str(summary.ktru_code or ""),
                unit_price=summary.unit_price if isinstance(summary.unit_price, (int, float)) else None,
            ):
                result["auto_draft_enqueued"] = int(result.get("auto_draft_enqueued") or 0) + 1
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("auto_monitor_draft enqueue failed for %s: %s", oid, exc)
    if senders:
        sent, failed = _notify_summary(
            summary,
            find_item,
            db_path=db_path,
            settings=settings,
            senders=senders,
        )
        result["notified"] += sent
        result["notify_failed"] += failed
    else:
        _record_channels(db_path, summary, _seed_channels(), status="sent")
    known_global.add(oid)
    return processed_new + 1


def run_rts_pp_monitor_poll(
    meta_dir: Path,
    db_path: Path,
    *,
    force: bool = False,
    senders: list[tuple[str, str, Callable[..., None], str, str, float, str]] | None = None,
) -> dict[str, Any]:
    settings_path_obj = settings_path(meta_dir)
    settings = load_settings(settings_path_obj)
    result: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "reason": "",
        "seed_mode": False,
        "ktru_polled": 0,
        "items_seen": 0,
        "items_new": 0,
        "notified": 0,
        "notify_failed": 0,
        "seeded": 0,
        "pages_fetched": 0,
        "auto_draft_enqueued": 0,
    }
    if not settings.get("enabled") and not force:
        result["skipped"] = True
        result["reason"] = "disabled"
        return result
    if not settings.get("registry_backfill_done"):
        result["skipped"] = True
        result["reason"] = "backfill_pending"
        return result
    ktru_codes = effective_ktru_codes(settings)
    if not ktru_codes:
        result["skipped"] = True
        result["reason"] = "no_ktru"
        return result
    try:
        cookie = load_customer_cookie(meta_dir)
    except RtsCustomerPpApiError as exc:
        result["ok"] = False
        result["reason"] = exc.reason
        result["error"] = str(exc)
        return result

    probe_fail = _probe_customer_cookie(cookie, ktru_codes[0])
    if probe_fail is not None:
        return {**result, **probe_fail}

    poll_started = datetime.now(timezone.utc).replace(microsecond=0)
    known_global = list_known_pp_monitor_ids(db_path)
    max_per_run = _max_per_run()
    processed_new = 0
    take = _search_take(settings)
    updated_settings = deepcopy(settings)

    session_aborted = False
    for ktru_code in ktru_codes:
        if session_aborted:
            break
        skip = 0
        publish_from = publish_date_from_watermark(settings, ktru_code)
        all_known_on_page = False
        while not all_known_on_page:
            payload = _build_search_payload_for_settings(
                settings,
                ktru_code,
                skip=skip,
                take=take,
                publish_date_from=publish_from,
            )
            try:
                summaries, _total = search_registry_page(cookie, payload)
            except RtsCustomerPpApiError as exc:
                if _note_search_error(result, ktru_code=ktru_code, exc=exc):
                    session_aborted = True
                break
            if skip == 0:
                result["ktru_polled"] += 1
            result["pages_fetched"] += 1
            result["items_seen"] += len(summaries)
            if not summaries:
                break
            _persist_registry_page(db_path, summaries)
            updated_settings = _update_watermark_from_summaries(updated_settings, ktru_code, summaries)
            if _page_fully_known(summaries, known_global):
                if not _full_pages_per_poll() or len(summaries) < take:
                    break
            for summary in summaries:
                processed_new = _process_new_summary(
                    summary,
                    db_path=db_path,
                    meta_dir=meta_dir,
                    settings=settings,
                    cookie=cookie,
                    known_global=known_global,
                    senders=senders,
                    processed_new=processed_new,
                    max_per_run=max_per_run,
                    result=result,
                )
            skip += take
            if len(summaries) < take:
                all_known_on_page = True
            elif processed_new >= max_per_run and not _full_pages_per_poll():
                all_known_on_page = True

    if int(result.get("items_new") or 0) > 0:
        finished = datetime.now(timezone.utc).replace(microsecond=0)
        updated_settings["last_new_poll_started_at_utc"] = poll_started.isoformat().replace("+00:00", "Z")
        updated_settings["last_new_poll_finished_at_utc"] = finished.isoformat().replace("+00:00", "Z")
        updated_settings["last_new_poll_items_new"] = int(result["items_new"])

    save_settings(settings_path_obj, updated_settings)

    if result["notified"] or result["notify_failed"]:
        _LOG.info(
            "PP monitor poll: new=%s notified=%s failed=%s",
            result["items_new"],
            result["notified"],
            result["notify_failed"],
        )
    return result


def iter_json_objects(text: str) -> list[Any]:
    """Parse one JSON value or several concatenated objects (paged registry dumps)."""
    decoder = json.JSONDecoder()
    out: list[Any] = []
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, end = decoder.raw_decode(text, idx)
        out.append(obj)
        idx = end
    return out


def _page_items_from_blob(blob: Any) -> list[dict[str, Any]]:
    if isinstance(blob, list):
        return [x for x in blob if isinstance(x, dict)]
    if isinstance(blob, dict):
        page = blob.get("page")
        if isinstance(page, list):
            return [x for x in page if isinstance(x, dict)]
        if blob.get("ktru") or blob.get("offerPriceListItemProtectedId") or blob.get("protectedOfferItemId"):
            return [blob]
    return []


def import_registry_find_dump(
    db_path: Path,
    text: str,
    *,
    ktru_code: str = "",
    source: str = "dump",
) -> dict[str, Any]:
    """Load customer-registry search pages into findings (prices/regions), not into EIS core."""
    wanted = str(ktru_code or "").strip()
    blobs = iter_json_objects(text)
    seen: set[str] = set()
    imported = 0
    skipped = 0
    filtered = 0
    for blob in blobs:
        for raw in _page_items_from_blob(blob):
            summary = parse_search_summary(raw, ktru_code=wanted)
            if summary is None:
                skipped += 1
                continue
            if wanted and summary.ktru_code != wanted:
                filtered += 1
                continue
            oid = summary.offer_price_list_item_id
            if oid in seen:
                continue
            seen.add(oid)
            _save_registry_row(db_path, summary)
            _save_finding(db_path, summary, raw, source=source, trigger_recompute=False)
            imported += 1
    return {
        "ok": True,
        "blobs": len(blobs),
        "imported": imported,
        "skipped": skipped,
        "filtered": filtered,
        "ktru_code": wanted,
        "source": source,
    }

"""Auto PP draft queue from competitor monitor (separate from EIS auto-participate)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from auto_monitor_draft_settings import (
    DEFAULT_RTS_PROFILE,
    global_enabled,
    load_settings,
    settings_path,
)
from eis_analytics_db import (
    claim_next_auto_monitor_draft,
    count_auto_monitor_draft_done_since,
    enqueue_auto_monitor_draft,
    get_pp_monitor_draft,
    list_auto_monitor_draft_queue,
    update_auto_monitor_draft_queue,
)
from monitor_find_to_draft import competitor_unit_price_amount, delivery_params_from_find
from participation_price import parse_quantity
from rts_cookie_profiles import cookie_path_for_profile, normalize_rts_profile
from rts_pp_draft_service import create_pp_draft_from_monitor
from rts_pp_publish_service import publish_enabled, start_publish_job

_LOG = logging.getLogger("auto_monitor_draft")
AUTO_MONITOR_PUBLISH_SOURCE = "auto_monitor"


@dataclass
class QueueDrainSummary:
    processed: int = 0
    done: int = 0
    skipped: int = 0
    errors: int = 0
    cookie_expired: int = 0
    publish_prepared: int = 0
    publish_errors: int = 0
    stopped_reason: str = ""
    enqueued: int = 0


def auto_monitor_draft_env_enabled() -> bool:
    return os.environ.get("RTS_AUTO_MONITOR_DRAFT", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def compute_pp_sum(
    find_item: dict[str, Any] | None,
    *,
    fallback_unit_price: float | None = None,
) -> float | None:
    """Сумма ПП = цена за ед. × количество из deliveryPlaces[0]. None if quantity missing."""
    item = find_item if isinstance(find_item, dict) else {}
    params = delivery_params_from_find(item)
    qty = parse_quantity(str(params.get("quantity_text") or ""))
    if qty is None:
        return None
    price = competitor_unit_price_amount(item, fallback_unit_price=fallback_unit_price)
    if price is None:
        return None
    return float(price) * float(qty)


def matches_rules(
    *,
    ktru_code: str,
    pp_sum: float | None,
    settings: dict[str, Any],
) -> tuple[bool, str]:
    if not settings.get("enabled"):
        return False, "disabled"
    code = str(ktru_code or "").strip()
    if not code:
        return False, "no_ktru"
    allowed = {str(x).strip() for x in (settings.get("ktru_codes") or []) if str(x).strip()}
    if not allowed:
        return False, "empty_whitelist"
    if code not in allowed:
        return False, "ktru_whitelist"
    if pp_sum is None:
        return False, "no_quantity"
    try:
        min_amount = float(settings.get("min_pp_amount") or 0)
    except (TypeError, ValueError):
        min_amount = 0.0
    if float(pp_sum) < min_amount:
        return False, "min_amount"
    return True, ""


def try_enqueue_from_finding(
    analytics_db: Path,
    meta_dir: Path,
    *,
    offer_price_list_item_id: str,
    find_item: dict[str, Any] | None,
    ktru_code: str = "",
    unit_price: float | None = None,
) -> bool:
    """Enqueue if env+settings+rules match. Returns True when queued as pending."""
    if not auto_monitor_draft_env_enabled():
        return False
    settings = load_settings(settings_path(meta_dir))
    if not global_enabled(settings):
        return False
    profile = normalize_rts_profile(settings.get("rts_profile")) or DEFAULT_RTS_PROFILE
    oid = str(offer_price_list_item_id or "").strip()
    if not oid:
        return False

    existing_draft = get_pp_monitor_draft(analytics_db, oid, rts_profile=profile)
    if existing_draft and existing_draft.get("status") == "created" and existing_draft.get("rts_draft_id"):
        return False

    ktru = str(ktru_code or "").strip()
    if not ktru and isinstance(find_item, dict):
        ktru_obj = find_item.get("ktru") if isinstance(find_item.get("ktru"), dict) else {}
        ktru = str(ktru_obj.get("code") or "").strip()

    pp_sum = compute_pp_sum(find_item, fallback_unit_price=unit_price)
    ok, reason = matches_rules(ktru_code=ktru, pp_sum=pp_sum, settings=settings)
    if not ok:
        _LOG.debug("auto_monitor_draft skip enqueue %s: %s", oid, reason)
        return False

    queued = enqueue_auto_monitor_draft(
        analytics_db,
        offer_price_list_item_id=oid,
        rts_profile=profile,
        ktru_code=ktru,
        pp_sum=pp_sum,
    )
    if queued:
        _LOG.info(
            "auto_monitor_draft enqueued offer=%s ktru=%s sum=%s profile=%s",
            oid,
            ktru,
            pp_sum,
            profile,
        )
    return queued


def _maybe_start_auto_publish(
    *,
    analytics_db: Path,
    cookie_path: Path,
    settings: dict[str, Any],
    profile: str,
    offer_id: str,
    rts_draft_id: int | None,
    summary: QueueDrainSummary,
) -> str:
    """After draft: prepare publish job if auto_publish on. Returns queue status."""
    if not settings.get("auto_publish"):
        return "created"
    if not publish_enabled():
        summary.publish_errors += 1
        return "created"
    if not rts_draft_id:
        return "created"
    try:
        pub = start_publish_job(
            analytics_db=analytics_db,
            cookie_path=cookie_path,
            rts_draft_id=int(rts_draft_id),
            rts_profile=profile,
            source=AUTO_MONITOR_PUBLISH_SOURCE,
            source_key=str(offer_id or ""),
            force_new=False,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("auto_monitor_draft publish prepare crashed draft=%s", rts_draft_id)
        summary.publish_errors += 1
        return "created"
    if pub.ok and str(pub.status or "") == "awaiting_signature":
        summary.publish_prepared += 1
        return "awaiting_signature"
    if pub.ok and str(pub.status or "") == "published":
        summary.publish_prepared += 1
        return "published"
    summary.publish_errors += 1
    _LOG.warning(
        "auto_monitor_draft publish prepare failed draft=%s: %s",
        rts_draft_id,
        pub.error or pub.reason,
    )
    return "created"


def process_auto_monitor_draft_queue(
    meta_dir: Path,
    analytics_db: Path,
    *,
    project_root: Path | None = None,
    preoffers_path: Path | None = None,
    limit: int = 5,
) -> QueueDrainSummary:
    """Drain pending queue FIFO (create drafts). Call after monitor poll."""
    summary = QueueDrainSummary()
    if not auto_monitor_draft_env_enabled():
        summary.stopped_reason = "env_disabled"
        return summary
    settings = load_settings(settings_path(meta_dir))
    if not global_enabled(settings):
        summary.stopped_reason = "settings_disabled"
        return summary

    profile = normalize_rts_profile(settings.get("rts_profile")) or DEFAULT_RTS_PROFILE
    try:
        max_hour = max(1, min(int(settings.get("max_per_hour") or 10), 100))
    except (TypeError, ValueError):
        max_hour = 10
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    done_hour = count_auto_monitor_draft_done_since(
        analytics_db, rts_profile=profile, since_utc=since
    )
    remaining = max(0, max_hour - done_hour)
    if remaining <= 0:
        summary.stopped_reason = "hour_limit"
    else:
        root = project_root or Path(os.environ.get("RTS_PROJECT_ROOT", Path(__file__).resolve().parent))
        cookie_path = cookie_path_for_profile(profile, meta_dir, root)
        offers_path = preoffers_path or Path(
            os.environ.get("RTS_PREOFFERS_FILE", str(meta_dir / "rts_preoffers.json"))
        )
        batch = max(1, min(int(limit), remaining, 20))

        for _ in range(batch):
            if count_auto_monitor_draft_done_since(
                analytics_db, rts_profile=profile, since_utc=since
            ) >= max_hour:
                summary.stopped_reason = "hour_limit"
                break
            job = claim_next_auto_monitor_draft(analytics_db, rts_profile=profile)
            if not job:
                break
            summary.processed += 1
            oid = str(job.get("offer_price_list_item_id") or "").strip()
            existing = get_pp_monitor_draft(analytics_db, oid, rts_profile=profile)
            if existing and existing.get("status") == "created" and existing.get("rts_draft_id"):
                q_status = _maybe_start_auto_publish(
                    analytics_db=analytics_db,
                    cookie_path=cookie_path,
                    settings=settings,
                    profile=profile,
                    offer_id=oid,
                    rts_draft_id=int(existing["rts_draft_id"]),
                    summary=summary,
                )
                update_auto_monitor_draft_queue(
                    analytics_db,
                    oid,
                    rts_profile=profile,
                    status=q_status,
                    rts_draft_id=int(existing["rts_draft_id"]),
                    error_text="already_existed",
                )
                summary.done += 1
                summary.skipped += 1
                continue
            try:
                result = create_pp_draft_from_monitor(
                    analytics_db=analytics_db,
                    meta_dir=meta_dir,
                    preoffers_path=offers_path,
                    cookie_path=cookie_path,
                    offer_price_list_item_id=oid,
                    rts_profile=profile,
                    created_by="auto_monitor_draft",
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("auto_monitor_draft create crashed offer=%s", oid)
                update_auto_monitor_draft_queue(
                    analytics_db,
                    oid,
                    rts_profile=profile,
                    status="error",
                    error_text=str(exc)[:2000],
                )
                summary.errors += 1
                continue

            if result.ok:
                q_status = _maybe_start_auto_publish(
                    analytics_db=analytics_db,
                    cookie_path=cookie_path,
                    settings=settings,
                    profile=profile,
                    offer_id=oid,
                    rts_draft_id=result.rts_draft_id,
                    summary=summary,
                )
                update_auto_monitor_draft_queue(
                    analytics_db,
                    oid,
                    rts_profile=profile,
                    status=q_status,
                    rts_draft_id=result.rts_draft_id,
                    error_text="already_existed" if result.already_existed else "",
                )
                summary.done += 1
                if count_auto_monitor_draft_done_since(
                    analytics_db, rts_profile=profile, since_utc=since
                ) >= max_hour:
                    summary.stopped_reason = "hour_limit"
                    break
                continue

            status = "cookie_expired" if result.reason in ("no_cookie", "session_expired") else "error"
            if result.reason in ("not_eligible", "duplicate_draft", "not_found", "no_ktru", "bad_request"):
                status = "skipped"
            update_auto_monitor_draft_queue(
                analytics_db,
                oid,
                rts_profile=profile,
                status=status,
                error_text=str(result.error or result.reason or "")[:2000],
                rts_draft_id=result.rts_draft_id,
            )
            if status == "cookie_expired":
                summary.cookie_expired += 1
                summary.stopped_reason = "cookie_expired"
                _LOG.warning("auto_monitor_draft cookie expired profile=%s: %s", profile, result.error)
                break
            if status == "skipped":
                summary.skipped += 1
            else:
                summary.errors += 1

    # Catch-up: drafts left as «created»/legacy «done» when auto_publish turned on later.
    _catchup_auto_publish(
        analytics_db=analytics_db,
        meta_dir=meta_dir,
        project_root=project_root,
        settings=settings,
        profile=profile,
        summary=summary,
    )
    return summary


def _catchup_auto_publish(
    *,
    analytics_db: Path,
    meta_dir: Path,
    project_root: Path | None,
    settings: dict[str, Any],
    profile: str,
    summary: QueueDrainSummary,
) -> None:
    if summary.stopped_reason == "cookie_expired":
        return
    if not settings.get("auto_publish"):
        return
    if not publish_enabled():
        return
    root = project_root or Path(os.environ.get("RTS_PROJECT_ROOT", Path(__file__).resolve().parent))
    cookie_path = cookie_path_for_profile(profile, meta_dir, root)
    for row in list_auto_monitor_draft_queue(analytics_db, limit=50, rts_profile=profile):
        status = str(row.get("status") or "")
        # «created» = draft ready for prepare; «done» = legacy status before created-parity.
        if status not in ("created", "done"):
            continue
        draft_id = row.get("rts_draft_id")
        if not draft_id:
            continue
        oid = str(row.get("offer_price_list_item_id") or "").strip()
        if not oid:
            continue
        q_status = _maybe_start_auto_publish(
            analytics_db=analytics_db,
            cookie_path=cookie_path,
            settings=settings,
            profile=profile,
            offer_id=oid,
            rts_draft_id=int(draft_id),
            summary=summary,
        )
        if q_status == "created":
            continue
        update_auto_monitor_draft_queue(
            analytics_db,
            oid,
            rts_profile=profile,
            status=q_status,
            rts_draft_id=int(draft_id),
        )
        _LOG.info(
            "auto_monitor_draft publish catch-up draft=%s offer=%s profile=%s status=%s",
            draft_id,
            oid,
            profile,
            q_status,
        )


def drain_summary_as_dict(summary: QueueDrainSummary) -> dict[str, Any]:
    return {
        "processed": summary.processed,
        "done": summary.done,
        "skipped": summary.skipped,
        "errors": summary.errors,
        "cookie_expired": summary.cookie_expired,
        "publish_prepared": summary.publish_prepared,
        "publish_errors": summary.publish_errors,
        "stopped_reason": summary.stopped_reason,
        "enqueued": summary.enqueued,
    }

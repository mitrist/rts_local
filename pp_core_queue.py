"""Sequential PP draft+publish queue from demand-core variants."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from catalog_pp_draft import CatalogPpDraftError, normalize_region_prices
from eis_analytics_db import (
    claim_next_pp_core_queue,
    get_pp_core_variant,
    has_active_pp_core_queue_item,
    insert_pp_core_queue_row,
    list_pp_core_queue,
    save_pp_core_delivery,
    update_pp_core_queue,
)
from pp_core_catalog import (
    cache_dir_from_meta,
    resolve_ktru_template,
    sync_core_snapshot_template,
    variant_can_enqueue,
    variant_selection_key,
)
from pp_occupancy import occupied_selection_keys
from rts_cookie_profiles import cookie_path_for_profile, normalize_rts_profile
from rts_pp_draft_service import create_pp_draft_from_catalog
from rts_pp_publish_service import publish_enabled, start_publish_job

_LOG = logging.getLogger("pp_core_queue")
CORE_PUBLISH_SOURCE = "core"
_DRAIN_LOCK = threading.Lock()


class CoreQueueError(ValueError):
    """Invalid enqueue payload."""


@dataclass
class CoreEnqueueResult:
    run_id: str
    enqueued: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def as_api_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "run_id": self.run_id,
            "enqueued": self.enqueued,
            "skipped": self.skipped,
        }


@dataclass
class CoreDrainSummary:
    processed: int = 0
    drafted: int = 0
    publish_prepared: int = 0
    errors: int = 0
    stopped_reason: str = ""


def _source_key(ktru_code: str, variant_no: int, run_id: str) -> str:
    return f"core:{ktru_code}:{int(variant_no)}:{run_id}"


def enqueue_core_variants(
    analytics_db: Path,
    *,
    ktru_code: str,
    variant_nos: list[Any],
    regions: list[Any],
    rts_profile: str,
    created_by: str = "",
    run_id: str | None = None,
) -> CoreEnqueueResult:
    code = str(ktru_code or "").strip()
    profile = normalize_rts_profile(rts_profile)
    if not code:
        raise CoreQueueError("Укажите код КТРУ.")
    if not profile:
        raise CoreQueueError("Укажите профиль РТС.")
    nos: list[int] = []
    seen: set[int] = set()
    for raw in variant_nos or []:
        try:
            num = int(raw)
        except (TypeError, ValueError):
            continue
        if num < 1 or num in seen:
            continue
        seen.add(num)
        nos.append(num)
    if not nos:
        raise CoreQueueError("Отметьте хотя бы один вариант ядра.")
    try:
        region_rows = normalize_region_prices(regions)
    except CatalogPpDraftError as exc:
        raise CoreQueueError(str(exc)) from exc
    rid = str(run_id or "").strip() or str(uuid.uuid4())
    save_pp_core_delivery(analytics_db, code, region_rows)
    result = CoreEnqueueResult(run_id=rid)
    occupied = occupied_selection_keys(analytics_db, ktru_code=code, rts_profile=profile, queued=True)
    batch_keys: dict[frozenset[tuple[str, str]], int] = {}
    for num in nos:
        variant = get_pp_core_variant(analytics_db, code, num)
        if variant is None:
            result.skipped.append({"variant_no": num, "reason": "нет в снимке ядра"})
            continue
        if not variant_can_enqueue(variant):
            status = str(variant.get("map_status") or "")
            result.skipped.append(
                {
                    "variant_no": num,
                    "reason": variant.get("map_reason")
                    or f"статус {status}, в очередь только ready/валидация с кодами РТС",
                }
            )
            continue
        key = variant_selection_key(variant)
        if key:
            if key in batch_keys:
                result.skipped.append(
                    {
                        "variant_no": num,
                        "reason": f"набор характеристик РТС совпадает с вариантом {batch_keys[key]}",
                    }
                )
                continue
            if key in occupied:
                result.skipped.append(
                    {
                        "variant_no": num,
                        "reason": f"набор характеристик РТС совпадает с вариантом {occupied[key]}",
                    }
                )
                continue
        if has_active_pp_core_queue_item(
            analytics_db, ktru_code=code, variant_no=num, rts_profile=profile
        ):
            result.skipped.append({"variant_no": num, "reason": "уже в активной очереди"})
            continue
        inserted = insert_pp_core_queue_row(
            analytics_db,
            run_id=rid,
            ktru_code=code,
            variant_no=num,
            eis_hash=str(variant.get("eis_hash") or ""),
            rts_profile=profile,
            regions=region_rows,
            created_by=created_by,
        )
        if inserted:
            result.enqueued += 1
            if key:
                batch_keys[key] = num
        else:
            result.skipped.append({"variant_no": num, "reason": "уже в этом run"})
    if result.enqueued < 1:
        bits = [
            f"v{row.get('variant_no')}: {row.get('reason')}"
            for row in result.skipped
            if row.get("reason")
        ]
        msg = "Ни один вариант не поставлен в очередь."
        if bits:
            msg += " " + "; ".join(bits)
        raise CoreQueueError(msg)
    return result


def _maybe_start_publish(
    *,
    analytics_db: Path,
    cookie_path: Path,
    profile: str,
    ktru_code: str,
    variant_no: int,
    run_id: str,
    rts_draft_id: int | None,
    summary: CoreDrainSummary,
) -> str:
    if not publish_enabled():
        return "drafted"
    if not rts_draft_id:
        return "drafted"
    try:
        pub = start_publish_job(
            analytics_db=analytics_db,
            cookie_path=cookie_path,
            rts_draft_id=int(rts_draft_id),
            rts_profile=profile,
            source=CORE_PUBLISH_SOURCE,
            source_key=_source_key(ktru_code, variant_no, run_id),
            force_new=False,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("pp_core publish prepare crashed draft=%s", rts_draft_id)
        summary.errors += 1
        return "drafted"
    if pub.ok and str(pub.status or "") == "awaiting_signature":
        summary.publish_prepared += 1
        return "publish_prepared"
    if pub.ok and str(pub.status or "") == "published":
        summary.publish_prepared += 1
        return "done"
    _LOG.warning("pp_core publish prepare failed draft=%s: %s", rts_draft_id, pub.error or pub.reason)
    summary.errors += 1
    return "drafted"


def process_pp_core_queue(
    meta_dir: Path,
    analytics_db: Path,
    *,
    project_root: Path | None = None,
    rts_profile: str = "",
    run_id: str = "",
    limit: int = 1,
) -> CoreDrainSummary:
    """Drain pending core jobs FIFO. Default one draft per call."""
    summary = CoreDrainSummary()
    if not _DRAIN_LOCK.acquire(blocking=False):
        summary.stopped_reason = "busy"
        return summary
    try:
        return _process_pp_core_queue_locked(
            meta_dir,
            analytics_db,
            project_root=project_root,
            rts_profile=rts_profile,
            run_id=run_id,
            limit=limit,
        )
    finally:
        _DRAIN_LOCK.release()


def _process_pp_core_queue_locked(
    meta_dir: Path,
    analytics_db: Path,
    *,
    project_root: Path | None = None,
    rts_profile: str = "",
    run_id: str = "",
    limit: int = 1,
) -> CoreDrainSummary:
    summary = CoreDrainSummary()
    profile_filter = normalize_rts_profile(rts_profile)
    root = project_root or Path(os.environ.get("RTS_PROJECT_ROOT", Path(__file__).resolve().parent))
    batch = max(1, min(int(limit), 20))
    for _ in range(batch):
        job = claim_next_pp_core_queue(
            analytics_db, rts_profile=profile_filter, run_id=str(run_id or "")
        )
        if not job:
            if summary.processed == 0:
                summary.stopped_reason = "empty"
            break
        summary.processed += 1
        qid = int(job["id"])
        code = str(job.get("ktru_code") or "").strip()
        variant_no = int(job.get("variant_no") or 0)
        profile = normalize_rts_profile(job.get("rts_profile")) or profile_filter
        rid = str(job.get("run_id") or "")
        tmpl = resolve_ktru_template(code, cache_dir=cache_dir_from_meta(meta_dir))
        sync_core_snapshot_template(analytics_db, code, tmpl)
        variant = get_pp_core_variant(analytics_db, code, variant_no)
        if variant is None:
            update_pp_core_queue(analytics_db, qid, status="error", error_text="вариант исчез из снимка")
            summary.errors += 1
            continue
        if not variant_can_enqueue(variant):
            update_pp_core_queue(
                analytics_db,
                qid,
                status="skipped",
                error_text=str(variant.get("map_reason") or "нельзя поставить в очередь"),
            )
            summary.errors += 1
            continue
        key = variant_selection_key(variant)
        if key:
            occupied = occupied_selection_keys(
                analytics_db, ktru_code=code, rts_profile=profile, queued=False
            )
            taken = occupied.get(key)
            if taken is not None and taken != variant_no:
                update_pp_core_queue(
                    analytics_db,
                    qid,
                    status="skipped",
                    error_text=f"набор характеристик РТС совпадает с вариантом {taken}",
                )
                summary.errors += 1
                continue
        chars = variant.get("characteristics")
        if not isinstance(chars, list):
            chars = []
        cookie_path = cookie_path_for_profile(profile, meta_dir, root)
        try:
            result = create_pp_draft_from_catalog(
                analytics_db=analytics_db,
                meta_dir=meta_dir,
                cookie_path=cookie_path,
                ktru_code=code,
                characteristics=chars,
                regions=job.get("regions") if isinstance(job.get("regions"), list) else [],
                rts_profile=profile,
                created_by=str(job.get("created_by") or "pp_core_queue"),
                source=CORE_PUBLISH_SOURCE,
                fill_extra={
                    "variant_no": variant_no,
                    "run_id": rid,
                    "eis_hash": str(variant.get("eis_hash") or ""),
                },
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("pp_core create crashed ktru=%s variant=%s", code, variant_no)
            update_pp_core_queue(analytics_db, qid, status="error", error_text=str(exc)[:2000])
            summary.errors += 1
            continue
        if not result.ok:
            update_pp_core_queue(
                analytics_db,
                qid,
                status="error",
                error_text=str(result.error or result.reason or "create_failed")[:2000],
                rts_draft_id=result.rts_draft_id,
            )
            summary.errors += 1
            continue
        summary.drafted += 1
        q_status = _maybe_start_publish(
            analytics_db=analytics_db,
            cookie_path=cookie_path,
            profile=profile,
            ktru_code=code,
            variant_no=variant_no,
            run_id=rid,
            rts_draft_id=result.rts_draft_id,
            summary=summary,
        )
        update_pp_core_queue(
            analytics_db,
            qid,
            status=q_status,
            rts_draft_id=result.rts_draft_id,
        )
    return summary


def queue_payload(analytics_db: Path, *, run_id: str = "", ktru_code: str = "") -> dict[str, Any]:
    items = list_pp_core_queue(analytics_db, run_id=run_id, ktru_code=ktru_code)
    counts: dict[str, int] = {}
    for row in items:
        st = str(row.get("status") or "")
        counts[st] = counts.get(st, 0) + 1
    return {"ok": True, "run_id": str(run_id or ""), "items": items, "counts": counts}

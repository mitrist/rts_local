"""Sequential local draft queue from the virtual set (core + belt), no RTS session."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from catalog_pp_draft import CATALOG_PURCHASE_NUMBER, CatalogPpDraftError, normalize_region_prices
from eis_analytics_db import (
    claim_next_pp_virtual_queue,
    get_pp_core_delivery,
    has_active_pp_virtual_queue_item,
    has_rts_pp_draft_id,
    insert_pp_virtual_queue_row,
    list_pp_virtual_queue,
    next_local_virtual_draft_id,
    save_pp_core_delivery,
    save_rts_pp_draft,
    update_pp_virtual_queue,
    update_pp_virtual_queue_regions,
)
from virtual_pp_match import match_eis_html_line
from pp_core_catalog import (
    MAP_READY,
    MAP_VALIDATE,
    cache_dir_from_meta,
    map_combo_to_rts,
    resolve_ktru_template,
    selection_eis_hash,
    variant_can_enqueue,
    variant_selection_key,
)
from pp_occupancy import occupied_selection_keys
from rts_cookie_profiles import normalize_rts_profile
from virtual_pp_store import (
    get_rts_ezt_lot_item,
    get_virtual_match_event,
    get_virtual_pp_variant,
    latest_virtual_set,
    list_virtual_pp_variants,
    load_notice_characteristics,
    upsert_virtual_match_event,
    upsert_virtual_pp_variant,
)

_LOG = logging.getLogger("pp_virtual_queue")
VIRTUAL_SOURCE = "virtual_core"
_DRAIN_LOCK = threading.Lock()


class VirtualQueueError(ValueError):
    """Invalid virtual enqueue payload."""


@dataclass
class VirtualEnqueueResult:
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
class VirtualDrainSummary:
    processed: int = 0
    drafted: int = 0
    errors: int = 0
    stopped_reason: str = ""


def enqueue_virtual_variants(
    analytics_db: Path,
    *,
    ktru_code: str,
    variant_nos: list[Any],
    regions: list[Any],
    rts_profile: str,
    created_by: str = "",
    run_id: str | None = None,
) -> VirtualEnqueueResult:
    code = str(ktru_code or "").strip()
    profile = normalize_rts_profile(rts_profile)
    if not code:
        raise VirtualQueueError("Укажите код КТРУ.")
    if not profile:
        raise VirtualQueueError("Укажите профиль РТС.")
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
        raise VirtualQueueError("Отметьте хотя бы один вариант набора.")
    try:
        region_rows = normalize_region_prices(regions)
    except CatalogPpDraftError as exc:
        raise VirtualQueueError(str(exc)) from exc
    rid = str(run_id or "").strip() or str(uuid.uuid4())
    save_pp_core_delivery(analytics_db, code, region_rows)
    meta = latest_virtual_set(analytics_db, code) or {}
    set_id = str(meta.get("set_id") or "")
    if not set_id:
        raise VirtualQueueError("Сначала соберите виртуальный набор.")
    result = VirtualEnqueueResult(run_id=rid)
    occupied = occupied_selection_keys(
        analytics_db,
        ktru_code=code,
        rts_profile=profile,
        queued=True,
        include_core=False,
        include_virtual=True,
    )
    batch_keys: dict[frozenset[tuple[str, str]], int] = {}
    for num in nos:
        variant = get_virtual_pp_variant(analytics_db, code, num)
        if variant is None:
            result.skipped.append({"variant_no": num, "reason": "нет в виртуальном наборе"})
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
        if has_active_pp_virtual_queue_item(
            analytics_db, ktru_code=code, variant_no=num, rts_profile=profile
        ):
            result.skipped.append({"variant_no": num, "reason": "уже в активной очереди"})
            continue
        inserted = insert_pp_virtual_queue_row(
            analytics_db,
            run_id=rid,
            ktru_code=code,
            variant_no=num,
            eis_hash=str(variant.get("eis_hash") or ""),
            in_core=int(variant.get("in_core") or 0),
            set_id=set_id,
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
        raise VirtualQueueError(msg)
    return result


def _create_local_virtual_draft(
    analytics_db: Path,
    meta_dir: Path,
    *,
    ktru_code: str,
    variant: dict[str, Any],
    regions: list[dict[str, Any]],
    rts_profile: str,
    created_by: str,
    variant_no: int,
    run_id: str,
) -> dict[str, Any]:
    profile = normalize_rts_profile(rts_profile)
    line_key = str(uuid.uuid4())
    tmpl = resolve_ktru_template(ktru_code, cache_dir=cache_dir_from_meta(meta_dir))
    ktru_name = ""
    if isinstance(tmpl, dict):
        ktru_name = str(tmpl.get("name") or "").strip()
    if not ktru_name:
        ktru_name = str(variant.get("chars_preview") or "").strip()
    fill_report = {
        "source": VIRTUAL_SOURCE,
        "ktru_code": ktru_code,
        "ktru_name": ktru_name,
        "regions_count": len(regions),
        "variant_no": variant_no,
        "run_id": run_id,
        "eis_hash": str(variant.get("eis_hash") or ""),
        "in_core": int(variant.get("in_core") or 0),
        "local": True,
    }
    draft_id = next_local_virtual_draft_id(analytics_db, rts_profile=profile)
    save_rts_pp_draft(
        analytics_db,
        purchase_number=CATALOG_PURCHASE_NUMBER,
        line_key=line_key,
        ktru_code=ktru_code,
        rts_draft_id=draft_id,
        status="created",
        created_by=created_by,
        rts_profile=profile,
        fill_report_json=json.dumps(fill_report, ensure_ascii=False),
        verify_json=json.dumps({"source": VIRTUAL_SOURCE, "ok": 1, "local": True}, ensure_ascii=False),
    )
    return {"ok": True, "rts_draft_id": draft_id, "line_key": line_key, "fill_report": fill_report}


def _notice_chars_as_eis(raw: list[Any] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for ch in raw or []:
        if not isinstance(ch, dict):
            continue
        label = str(
            ch.get("display_label") or ch.get("eis_char") or ch.get("name") or ch.get("match_key") or ""
        ).strip()
        value = str(
            ch.get("value_display") or ch.get("eis_value") or ch.get("value") or ""
        ).strip()
        code = str(ch.get("characteristic_code") or ch.get("code") or "").strip()
        if not label and not value:
            continue
        out.append(
            {
                "display_label": label,
                "value_display": value,
                "characteristic_code": code,
            }
        )
    return out


def _regions_for_notice_draft(
    analytics_db: Path,
    ktru_code: str,
    event: dict[str, Any] | None,
    lot: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    oktmo = str(
        (event or {}).get("subject_oktmo") or (lot or {}).get("subject_oktmo") or ""
    ).strip()
    if not oktmo or oktmo == "unmapped":
        raise VirtualQueueError("У извещения нет субъекта ОКТМО — сначала проставьте алиас региона.")
    price = (event or {}).get("unit_price")
    if price is None and lot:
        price = lot.get("unit_price")
    qty = (event or {}).get("quantity")
    if qty is None and lot:
        qty = lot.get("quantity")
    delivery = get_pp_core_delivery(analytics_db, ktru_code)
    merged = [dict(row) for row in delivery if isinstance(row, dict)]
    codes = {str(row.get("code") or "").strip() for row in merged}
    if oktmo not in codes:
        merged.append(
            {
                "code": oktmo,
                "unit_price": "" if price is None else str(price),
                "quantity": "1" if qty is None else str(qty),
            }
        )
    try:
        return normalize_region_prices(merged)
    except CatalogPpDraftError:
        return normalize_region_prices(
            [
                {
                    "code": oktmo,
                    "unit_price": "" if price is None else str(price),
                    "quantity": "1" if qty is None else str(qty),
                }
            ]
        )


def _drafted_virtual_job(
    analytics_db: Path,
    *,
    ktru_code: str,
    variant_no: int,
    rts_profile: str,
) -> dict[str, Any] | None:
    profile = normalize_rts_profile(rts_profile)
    want = int(variant_no)
    for job in list_pp_virtual_queue(analytics_db, ktru_code=ktru_code, limit=500):
        if int(job.get("variant_no") or 0) != want:
            continue
        if normalize_rts_profile(job.get("rts_profile")) != profile:
            continue
        if str(job.get("status") or "") != "drafted":
            continue
        draft_id = job.get("rts_draft_id")
        if draft_id is None:
            continue
        if not has_rts_pp_draft_id(analytics_db, rts_draft_id=int(draft_id), rts_profile=profile):
            continue
        return job
    return None


def _attach_notice_region_to_job(
    analytics_db: Path,
    job: dict[str, Any],
    *,
    ktru_code: str,
    event: dict[str, Any] | None,
    lot: dict[str, Any] | None,
) -> str:
    """Add the notice subject region to an existing virtual draft. Returns oktmo or ''."""
    notice_rows = _regions_for_notice_draft(analytics_db, ktru_code, event, lot)
    oktmo = str((event or {}).get("subject_oktmo") or "").strip()
    add = next((row for row in notice_rows if str(row.get("code") or "").strip() == oktmo), None)
    if add is None and notice_rows:
        add = notice_rows[0]
        oktmo = str(add.get("code") or "").strip()
    if not add or not oktmo:
        return ""
    current = [dict(row) for row in (job.get("regions") or []) if isinstance(row, dict)]
    seen = {str(row.get("code") or "").strip() for row in current}
    if oktmo in seen:
        return ""
    current.append(add)
    try:
        normalized = normalize_region_prices(current)
    except CatalogPpDraftError:
        normalized = current
    update_pp_virtual_queue_regions(analytics_db, int(job["id"]), normalized)
    try:
        save_pp_core_delivery(analytics_db, ktru_code, normalize_region_prices(normalized))
    except CatalogPpDraftError:
        pass
    return oktmo


def _refresh_notice_match_event(
    analytics_db: Path,
    meta_dir: Path,
    *,
    ktru_code: str,
    notification_number: str,
    line_index: int,
    event: dict[str, Any] | None,
    notice_chars: list[dict[str, str]],
    tmpl: dict[str, Any] | None,
) -> dict[str, Any] | None:
    live = list_drafted_virtual_variants_for_match(analytics_db, ktru_code)
    if not live:
        return None
    matched = match_eis_html_line(
        notice_chars,
        live,
        tmpl,
        region_raw=str((event or {}).get("region_raw") or ""),
        subject_oktmo=str((event or {}).get("subject_oktmo") or ""),
        nmck=(event or {}).get("nmck"),
        quantity=(event or {}).get("quantity"),
        unit_price=(event or {}).get("unit_price"),
    )
    sid = str((event or {}).get("set_id") or (live[0].get("set_id") if live else "") or "")
    if not sid:
        meta = latest_virtual_set(analytics_db, ktru_code) or {}
        sid = str(meta.get("set_id") or "")
    if sid:
        upsert_virtual_match_event(
            analytics_db,
            {
                **matched,
                "notification_number": notification_number,
                "line_index": line_index,
                "ktru_code": ktru_code,
                "set_id": sid,
            },
        )
    return matched


def _reuse_existing_virtual_draft(
    analytics_db: Path,
    meta_dir: Path,
    *,
    ktru_code: str,
    variant_no: int,
    notification_number: str,
    line_index: int,
    rts_profile: str,
    event: dict[str, Any] | None,
    lot: dict[str, Any] | None,
    notice_chars: list[dict[str, str]],
    tmpl: dict[str, Any] | None,
) -> dict[str, Any]:
    existing = get_virtual_pp_variant(analytics_db, ktru_code, variant_no)
    job = _drafted_virtual_job(
        analytics_db, ktru_code=ktru_code, variant_no=variant_no, rts_profile=rts_profile
    )
    region_added = ""
    if job:
        try:
            region_added = _attach_notice_region_to_job(
                analytics_db, job, ktru_code=ktru_code, event=event, lot=lot
            )
        except VirtualQueueError:
            region_added = ""
    matched = _refresh_notice_match_event(
        analytics_db,
        meta_dir,
        ktru_code=ktru_code,
        notification_number=notification_number,
        line_index=line_index,
        event=event,
        notice_chars=notice_chars,
        tmpl=tmpl,
    )
    now_hit = str((matched or {}).get("char_status") or "") == "hit"
    if region_added:
        message = (
            f"Вариант {variant_no} уже создан. Добавлен регион {region_added} "
            "по этому извещению."
        )
    elif now_hit:
        message = f"Извещение совпадает с вариантом {variant_no} — черновик уже создан."
    else:
        message = f"Черновик с этим набором уже есть: вариант {variant_no}."
    return {
        "ok": True,
        "already": True,
        "region_added": bool(region_added),
        "matched": now_hit,
        "ktru_code": ktru_code,
        "variant_no": variant_no,
        "in_core": int((existing or {}).get("in_core") or 0),
        "rts_draft_id": (job or {}).get("rts_draft_id"),
        "message": message,
    }


def draft_virtual_from_notice(
    analytics_db: Path,
    meta_dir: Path,
    *,
    ktru_code: str,
    notification_number: str,
    line_index: int,
    rts_profile: str,
    created_by: str = "",
) -> dict[str, Any]:
    """Map unmatched notice chars into the virtual set and a local Виртуальное_ядро draft."""
    code = str(ktru_code or "").strip()
    pn = str(notification_number or "").strip()
    profile = normalize_rts_profile(rts_profile)
    if not code:
        raise VirtualQueueError("Укажите код КТРУ.")
    if not pn:
        raise VirtualQueueError("Укажите номер извещения.")
    if not profile:
        raise VirtualQueueError("Укажите профиль РТС.")
    try:
        idx = int(line_index)
    except (TypeError, ValueError):
        idx = 0
    event = get_virtual_match_event(
        analytics_db, notification_number=pn, line_index=idx, ktru_code=code
    )
    lot = get_rts_ezt_lot_item(analytics_db, pn, idx)
    notice_chars = _notice_chars_as_eis(
        (lot.get("characteristics") if lot else None)
        or []
    )
    if not notice_chars:
        notice_chars = _notice_chars_as_eis(
            load_notice_characteristics(
                analytics_db, notification_number=pn, line_index=idx, ktru_code=code
            )
        )
    if not notice_chars:
        raise VirtualQueueError("Нет характеристик извещения для виртуального ПП.")
    tmpl = resolve_ktru_template(code, cache_dir=cache_dir_from_meta(meta_dir))
    mapped = map_combo_to_rts(notice_chars, tmpl)
    variant_row = {
        "eis_hash": selection_eis_hash(mapped.get("characteristics")),
        "in_core": 0,
        "characteristics": mapped.get("characteristics") or [],
        "chars_preview": str(mapped.get("preview") or ""),
        "map_status": str(mapped.get("map_status") or ""),
        "map_reason": str(mapped.get("map_reason") or ""),
    }
    if not variant_row["eis_hash"]:
        raise VirtualQueueError("Не удалось сопоставить характеристики извещения с шаблоном РТС.")
    if str(variant_row["map_status"]) not in (MAP_READY, MAP_VALIDATE):
        variant_row["map_status"] = MAP_VALIDATE
    if not variant_can_enqueue(variant_row):
        raise VirtualQueueError(
            str(variant_row.get("map_reason") or "Характеристики извещения нельзя поставить в очередь.")
        )
    key = variant_selection_key(variant_row)
    occupied = occupied_selection_keys(
        analytics_db,
        ktru_code=code,
        rts_profile=profile,
        queued=True,
        include_core=False,
        include_virtual=True,
    )
    if key and key in occupied:
        taken = int(occupied[key])
        return _reuse_existing_virtual_draft(
            analytics_db,
            meta_dir,
            ktru_code=code,
            variant_no=taken,
            notification_number=pn,
            line_index=idx,
            rts_profile=profile,
            event=event,
            lot=lot,
            notice_chars=notice_chars,
            tmpl=tmpl,
        )
    meta = latest_virtual_set(analytics_db, code) or {}
    saved = upsert_virtual_pp_variant(
        analytics_db, code, variant_row, set_id=str(meta.get("set_id") or "")
    )
    variant_no = int(saved.get("variant_no") or 0)
    regions = _regions_for_notice_draft(analytics_db, code, event, lot)
    save_pp_core_delivery(analytics_db, code, regions)
    rid = str(uuid.uuid4())
    inserted = insert_pp_virtual_queue_row(
        analytics_db,
        run_id=rid,
        ktru_code=code,
        variant_no=variant_no,
        eis_hash=str(saved.get("eis_hash") or ""),
        in_core=int(saved.get("in_core") or 0),
        set_id=str(saved.get("set_id") or meta.get("set_id") or ""),
        rts_profile=profile,
        regions=regions,
        created_by=created_by,
    )
    if not inserted:
        raise VirtualQueueError("Не удалось поставить вариант в очередь.")
    draft = _create_local_virtual_draft(
        analytics_db,
        meta_dir,
        ktru_code=code,
        variant=saved,
        regions=regions,
        rts_profile=profile,
        created_by=created_by or "pp_virtual_journal",
        variant_no=variant_no,
        run_id=rid,
    )
    job_id = None
    for job in list_pp_virtual_queue(analytics_db, run_id=rid, ktru_code=code):
        job_id = int(job.get("id") or 0)
        break
    if job_id:
        update_pp_virtual_queue(
            analytics_db,
            job_id,
            status="drafted",
            rts_draft_id=int(draft["rts_draft_id"]),
        )
    _refresh_notice_match_event(
        analytics_db,
        meta_dir,
        ktru_code=code,
        notification_number=pn,
        line_index=idx,
        event=event,
        notice_chars=notice_chars,
        tmpl=tmpl,
    )
    return {
        "ok": True,
        "already": False,
        "ktru_code": code,
        "variant_no": variant_no,
        "in_core": int(saved.get("in_core") or 0),
        "rts_draft_id": draft.get("rts_draft_id"),
        "line_key": draft.get("line_key"),
        "run_id": rid,
        "message": f"Создан черновик Виртуальное_ядро #{variant_no}.",
    }


def process_pp_virtual_queue(
    meta_dir: Path,
    analytics_db: Path,
    *,
    project_root: Path | None = None,
    rts_profile: str = "",
    run_id: str = "",
    limit: int = 1,
) -> VirtualDrainSummary:
    """Drain pending virtual jobs FIFO. Local Черновики ПП only, never RTS/session/publish."""
    summary = VirtualDrainSummary()
    if not _DRAIN_LOCK.acquire(blocking=False):
        summary.stopped_reason = "busy"
        return summary
    try:
        return _process_pp_virtual_queue_locked(
            meta_dir,
            analytics_db,
            project_root=project_root,
            rts_profile=rts_profile,
            run_id=run_id,
            limit=limit,
        )
    finally:
        _DRAIN_LOCK.release()


def _process_pp_virtual_queue_locked(
    meta_dir: Path,
    analytics_db: Path,
    *,
    project_root: Path | None = None,
    rts_profile: str = "",
    run_id: str = "",
    limit: int = 1,
) -> VirtualDrainSummary:
    summary = VirtualDrainSummary()
    profile_filter = normalize_rts_profile(rts_profile)
    batch = max(1, min(int(limit), 20))
    for _ in range(batch):
        job = claim_next_pp_virtual_queue(
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
        variant = get_virtual_pp_variant(analytics_db, code, variant_no)
        if variant is None:
            update_pp_virtual_queue(analytics_db, qid, status="error", error_text="вариант исчез из набора")
            summary.errors += 1
            continue
        if not variant_can_enqueue(variant):
            update_pp_virtual_queue(
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
                analytics_db,
                ktru_code=code,
                rts_profile=profile,
                queued=False,
                include_core=False,
                include_virtual=True,
            )
            taken = occupied.get(key)
            if taken is not None and taken != variant_no:
                update_pp_virtual_queue(
                    analytics_db,
                    qid,
                    status="skipped",
                    error_text=f"набор характеристик РТС совпадает с вариантом {taken}",
                )
                summary.errors += 1
                continue
        try:
            result = _create_local_virtual_draft(
                analytics_db,
                meta_dir,
                ktru_code=code,
                variant=variant,
                regions=job.get("regions") if isinstance(job.get("regions"), list) else [],
                rts_profile=profile,
                created_by=str(job.get("created_by") or "pp_virtual_queue"),
                variant_no=variant_no,
                run_id=rid,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("pp_virtual create crashed ktru=%s variant=%s", code, variant_no)
            update_pp_virtual_queue(analytics_db, qid, status="error", error_text=str(exc)[:2000])
            summary.errors += 1
            continue
        summary.drafted += 1
        update_pp_virtual_queue(
            analytics_db,
            qid,
            status="drafted",
            rts_draft_id=int(result["rts_draft_id"]),
        )
    return summary


def queue_payload(analytics_db: Path, *, run_id: str = "", ktru_code: str = "") -> dict[str, Any]:
    items = list_pp_virtual_queue(analytics_db, run_id=run_id, ktru_code=ktru_code)
    counts: dict[str, int] = {}
    for row in items:
        st = str(row.get("status") or "")
        counts[st] = counts.get(st, 0) + 1
    return {"ok": True, "run_id": str(run_id or ""), "items": items, "counts": counts}


def list_drafted_virtual_variants_for_match(analytics_db: Path, ktru_code: str) -> list[dict[str, Any]]:
    """Live virtual_core drafts with delivery regions, for EIS matching."""
    from eis_analytics_db import has_rts_pp_draft_id

    code = str(ktru_code or "").strip()
    if not code:
        return []
    by_no: dict[int, dict[str, Any]] = {}
    for job in list_pp_virtual_queue(analytics_db, ktru_code=code, limit=500):
        if str(job.get("status") or "") != "drafted":
            continue
        draft_id = job.get("rts_draft_id")
        if draft_id is None:
            continue
        profile = normalize_rts_profile(job.get("rts_profile"))
        if not profile or not has_rts_pp_draft_id(
            analytics_db, rts_draft_id=int(draft_id), rts_profile=profile
        ):
            continue
        num = int(job.get("variant_no") or 0)
        variant = get_virtual_pp_variant(analytics_db, code, num)
        if variant is None:
            continue
        row = dict(variant)
        row["regions"] = job.get("regions") if isinstance(job.get("regions"), list) else []
        by_no[num] = row
    if not by_no:
        return []
    order = {int(v.get("variant_no") or 0): i for i, v in enumerate(list_virtual_pp_variants(analytics_db, code))}
    return sorted(by_no.values(), key=lambda row: order.get(int(row.get("variant_no") or 0), 10**9))

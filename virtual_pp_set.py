"""Build a virtual PP set (core + optional belt) without enqueue/publish."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from eis_analytics_db import list_pp_core_variants
from pp_core_catalog import MAP_READY, map_combo_to_rts, resolve_ktru_template
from virtual_pp_store import latest_virtual_set, list_virtual_pp_variants, replace_virtual_pp_set

BELT_MAX = 8
BELT_CORE_MAX = 0.95
VIRTUAL_DELIVERY_DAYS_MIN = 1
VIRTUAL_DELIVERY_DAYS_MAX = 100


def new_set_id() -> str:
    return uuid.uuid4().hex[:16]


def pick_belt_rows(
    extras: list[dict[str, Any]],
    *,
    limit: int = BELT_MAX,
) -> list[dict[str, Any]]:
    ready = [
        row
        for row in extras
        if str(row.get("map_status") or "") == MAP_READY
        and str(row.get("eis_hash") or row.get("hash") or "").strip()
    ]
    return ready[: max(0, int(limit))]


def pick_belt_from_leftovers(
    leftover: list[dict[str, Any]],
    total: int,
    core_share: float,
    template: dict[str, Any] | None,
    *,
    limit: int = BELT_MAX,
) -> list[dict[str, Any]]:
    """Continue greedy coverage from leftover combos up to BELT_CORE_MAX, ready only."""
    acc_share = float(core_share or 0)
    belt: list[dict[str, Any]] = []
    denom = float(total or 0)
    for combo in leftover:
        if len(belt) >= max(0, int(limit)):
            break
        if acc_share >= BELT_CORE_MAX:
            break
        chars = combo.get("chars") if isinstance(combo.get("chars"), list) else []
        mapped = map_combo_to_rts(chars, template)
        if str(mapped.get("map_status") or "") != MAP_READY:
            continue
        eis_hash = str(combo.get("hash") or combo.get("eis_hash") or "").strip()
        if not eis_hash:
            continue
        n = int(combo.get("n") or 0)
        if denom:
            acc_share += n / denom
        belt.append(
            {
                "eis_hash": eis_hash,
                "hash": eis_hash,
                "n": n,
                "characteristics": mapped.get("characteristics") or [],
                "chars_preview": str(mapped.get("preview") or combo.get("preview") or ""),
                "map_status": mapped.get("map_status") or MAP_READY,
                "map_reason": str(mapped.get("map_reason") or ""),
            }
        )
    return belt


def load_belt_rows_from_history(
    history_db: Path,
    ktru_code: str,
    template: dict[str, Any] | None,
    *,
    limit: int = BELT_MAX,
) -> list[dict[str, Any]]:
    from pp_core_catalog import _demand_map_mod

    code = str(ktru_code or "").strip()
    if not code or not Path(history_db).is_file():
        return []
    demand = _demand_map_mod()
    rows = demand.load_rows(Path(history_db), code)
    if not rows:
        return []
    combos = demand.build_combos(rows)
    core, total, share = demand.select_core(combos)
    core_hashes = {str(c.get("hash") or "") for c in core}
    leftover = [c for c in combos if str(c.get("hash") or "") not in core_hashes]
    return pick_belt_from_leftovers(leftover, total, share, template, limit=limit)


def _existing_belt_rows(path: Path, ktru_code: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in list_virtual_pp_variants(path, ktru_code):
        if int(row.get("in_core") or 0):
            continue
        eis_hash = str(row.get("eis_hash") or "").strip()
        if not eis_hash:
            continue
        if not str(row.get("map_status") or ""):
            row["map_status"] = MAP_READY
        out.append(row)
    return out


def _assign_variant_nos(
    core_rows: list[dict[str, Any]],
    belt_rows: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prev_no = {
        str(row.get("eis_hash") or "").strip(): int(row.get("variant_no") or 0)
        for row in previous
        if str(row.get("eis_hash") or "").strip() and int(row.get("variant_no") or 0) >= 1
    }
    used: set[int] = set()
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in core_rows:
        eis_hash = str(row.get("eis_hash") or "").strip()
        if not eis_hash or eis_hash in seen:
            continue
        seen.add(eis_hash)
        try:
            no = int(row.get("variant_no") or 0)
        except (TypeError, ValueError):
            no = 0
        if no < 1 or no in used:
            no = max(used, default=0) + 1
        used.add(no)
        variants.append(
            {
                "variant_no": no,
                "eis_hash": eis_hash,
                "in_core": 1,
                "n": int(row.get("n") or 0),
                "characteristics": row.get("characteristics") if isinstance(row.get("characteristics"), list) else [],
                "chars_preview": str(row.get("chars_preview") or ""),
                "map_status": str(row.get("map_status") or ""),
                "map_reason": str(row.get("map_reason") or ""),
            }
        )
    next_no = max(used, default=0)
    for extra in belt_rows:
        eis_hash = str(extra.get("eis_hash") or extra.get("hash") or "").strip()
        if not eis_hash or eis_hash in seen:
            continue
        seen.add(eis_hash)
        no = int(prev_no.get(eis_hash) or 0)
        if no < 1 or no in used:
            next_no += 1
            no = next_no
        used.add(no)
        next_no = max(next_no, no)
        chars = extra.get("characteristics")
        if not isinstance(chars, list):
            chars = []
        variants.append(
            {
                "variant_no": no,
                "eis_hash": eis_hash,
                "in_core": 0,
                "n": int(extra.get("n") or 0),
                "characteristics": chars,
                "chars_preview": str(extra.get("chars_preview") or extra.get("preview") or ""),
                "map_status": str(extra.get("map_status") or MAP_READY),
                "map_reason": str(extra.get("map_reason") or ""),
            }
        )
    return variants


def _keep_extra_previous_variants(
    variants: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep journal-created variants that sync from core/belt would otherwise drop."""
    seen = {str(row.get("eis_hash") or "").strip() for row in variants}
    used = {int(row.get("variant_no") or 0) for row in variants}
    extra: list[dict[str, Any]] = []
    for row in previous:
        if not isinstance(row, dict):
            continue
        eis_hash = str(row.get("eis_hash") or "").strip()
        if not eis_hash or eis_hash in seen:
            continue
        try:
            no = int(row.get("variant_no") or 0)
        except (TypeError, ValueError):
            no = 0
        if no < 1 or no in used:
            no = max(used, default=0) + 1
        used.add(no)
        seen.add(eis_hash)
        chars = row.get("characteristics") if isinstance(row.get("characteristics"), list) else []
        extra.append(
            {
                "variant_no": no,
                "eis_hash": eis_hash,
                "in_core": int(row.get("in_core") or 0),
                "n": int(row.get("n") or 0),
                "characteristics": chars,
                "chars_preview": str(row.get("chars_preview") or ""),
                "map_status": str(row.get("map_status") or ""),
                "map_reason": str(row.get("map_reason") or ""),
            }
        )
    return list(variants) + extra


def sync_virtual_set_from_core(
    path: Path,
    ktru_code: str,
    *,
    belt_rows: list[dict[str, Any]] | None = None,
    history_db: Path | None = None,
    cache_dir: Path | None = None,
    template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    if not code:
        raise ValueError("Укажите ktru_code.")
    core = list_pp_core_variants(path, code)
    if not core:
        raise ValueError(f"Нет снимка ядра для КТРУ {code}.")
    previous = list_virtual_pp_variants(path, code)
    extras = belt_rows
    if extras is None:
        tmpl = template if isinstance(template, dict) and template else resolve_ktru_template(
            code, cache_dir=cache_dir
        )
        hist = Path(history_db) if history_db else None
        if hist is not None and hist.is_file():
            extras = load_belt_rows_from_history(hist, code, tmpl, limit=BELT_MAX)
        else:
            extras = _existing_belt_rows(path, code)
    set_id = new_set_id()
    variants = _assign_variant_nos(core, pick_belt_rows(extras or []), previous)
    variants = _keep_extra_previous_variants(variants, previous)
    summary = replace_virtual_pp_set(path, code, set_id, variants)
    summary["delivery_days"] = [VIRTUAL_DELIVERY_DAYS_MIN, VIRTUAL_DELIVERY_DAYS_MAX]
    summary["variants"] = variants
    return summary


def virtual_set_payload(path: Path, ktru_code: str) -> dict[str, Any]:
    from pp_core_catalog import mark_duplicate_selections, variant_can_enqueue
    from eis_analytics_db import get_pp_core_delivery

    code = str(ktru_code or "").strip()
    meta = latest_virtual_set(path, code) or {}
    rows = mark_duplicate_selections([dict(row) for row in list_virtual_pp_variants(path, code)])
    for row in rows:
        row["can_enqueue"] = variant_can_enqueue(row)
    return {
        "ok": True,
        "ktru_code": code,
        "set": meta,
        "delivery_days": [VIRTUAL_DELIVERY_DAYS_MIN, VIRTUAL_DELIVERY_DAYS_MAX],
        "delivery": get_pp_core_delivery(path, code),
        "variants": rows,
    }

"""Demand-core + price ladder from RTS customer-registry PP dumps."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from catalog_pp_draft import is_locked_characteristic
from pp_core_catalog import (
    CoreSnapshotError,
    _demand_map_mod,
    _norm,
    _option_text,
    _option_value_code,
    _template_indexes,
    demand_key,
    find_selection_pairs,
    locked_code_set,
    persist_core_snapshot,
    resolve_ktru_template,
)
from rts_ktru_cache import save_cached_template
from rts_pp_api import _math_notation, _parse_rts_num
from pp_core_nmck import _combo_hash
from pp_core_registry import (
    BIG_REGION_MIN,
    FREQUENT_REGION_LIMIT,
    subject_codes_from_find,
    unit_price_from_finding,
)
from rts_oktmo_catalog import load_subject_oktmo
from rts_pp_monitor import _page_items_from_blob, import_registry_find_dump, iter_json_objects


class PpDumpImportError(ValueError):
    """Cannot parse or import an RTS PP registry dump."""


def iter_pp_dump_items(text: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for blob in iter_json_objects(text):
        for raw in _page_items_from_blob(blob):
            oid = str(
                raw.get("offerPriceListItemProtectedId")
                or raw.get("protectedOfferItemId")
                or raw.get("id")
                or ""
            ).strip()
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            out.append(raw)
    return out


def required_unlocked_codes(template: dict[str, Any] | None) -> set[str]:
    if not isinstance(template, dict):
        return set()
    out: set[str] = set()
    for ch in template.get("characteristics") or []:
        if not isinstance(ch, dict) or ch.get("isActual") is False:
            continue
        code = str(ch.get("code") or "").strip()
        if code and ch.get("isRequired") and not is_locked_characteristic(ch):
            out.add(code)
    return out


def _dump_option_key(cv: dict[str, Any]) -> tuple[Any, ...]:
    qd = _norm(str(cv.get("qualityDescription") or ""))
    if qd:
        return ("q", qd)
    conc = _parse_rts_num(cv.get("concreteValue"))
    mn = _parse_rts_num(cv.get("minValue"))
    mx = _parse_rts_num(cv.get("maxValue"))
    if conc is None and mn is None and mx is None:
        return ()
    return (
        "n",
        conc,
        mn,
        mx,
        _math_notation(cv.get("minMathNotation")),
        _math_notation(cv.get("maxMathNotation")),
    )


def _kind_dto(kind_id: Any) -> dict[str, Any]:
    try:
        kid = int(kind_id or 0)
    except (TypeError, ValueError):
        kid = 0
    if kid == 1:
        return {"id": 1, "name": "Неизменяемая заказчиком"}
    return {"id": 2, "name": "Изменяемая заказчиком с выбором одного значения"}


def _type_dto(type_id: Any) -> dict[str, Any]:
    try:
        tid = int(type_id or 1)
    except (TypeError, ValueError):
        tid = 1
    if tid == 2:
        return {"id": 2, "name": "Количественная"}
    return {"id": 1, "name": "Качественная"}


def template_from_pp_dump_items(
    items: list[dict[str, Any]],
    *,
    ktru_code: str,
) -> dict[str, Any] | None:
    """Union of dump characteristic values when Ktru/get cache is empty."""
    wanted = str(ktru_code or "").strip()
    name = ""
    version: Any = 1
    unit = {"code": "796", "fullName": "Штука"}
    chars: dict[str, dict[str, Any]] = {}
    option_ids: dict[tuple[str, tuple[Any, ...]], dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        kobj = raw.get("ktru") if isinstance(raw.get("ktru"), dict) else {}
        code = str(kobj.get("code") or "").strip()
        if wanted and code != wanted:
            continue
        name = name or str(kobj.get("name") or "").strip()
        if kobj.get("version") is not None:
            version = kobj.get("version")
        u = raw.get("unit") if isinstance(raw.get("unit"), dict) else {}
        if u.get("code"):
            unit = {
                "code": str(u.get("code") or "796"),
                "fullName": str(u.get("name") or u.get("fullName") or "Штука"),
            }
        for ch in raw.get("ktruCharacteristics") or []:
            if not isinstance(ch, dict):
                continue
            ccode = str(ch.get("code") or "").strip()
            if not ccode:
                continue
            slot = chars.setdefault(
                ccode,
                {
                    "id": ch.get("id"),
                    "code": ccode,
                    "name": str(ch.get("name") or "").strip(),
                    "isActual": True,
                    "isRequired": bool(ch.get("isRequired")),
                    "characteristicType": _type_dto(ch.get("typeId")),
                    "characteristicKind": _kind_dto(ch.get("kindId")),
                    "characteristicValues": [],
                },
            )
            if not slot.get("name"):
                slot["name"] = str(ch.get("name") or "").strip()
            for cv in ch.get("characteristicValues") or []:
                if not isinstance(cv, dict):
                    continue
                key = _dump_option_key(cv)
                if not key:
                    continue
                seen = option_ids.setdefault((ccode, key), cv)
                if seen is cv:
                    opt = dict(cv)
                    if not str(opt.get("valueCode") or "").strip() and opt.get("id") is not None:
                        opt["valueCode"] = str(opt["id"])
                    slot["characteristicValues"].append(opt)
    if not chars:
        return None
    return {
        "code": wanted,
        "name": name or wanted,
        "version": version,
        "actual": True,
        "okeis": [unit],
        "characteristics": list(chars.values()),
    }


def _chars_from_selection(
    selection: dict[str, str] | list[dict[str, str]],
    template: dict[str, Any],
    locked: set[str],
) -> list[dict[str, str]]:
    by_code, _by_name = _template_indexes(template)
    if isinstance(selection, dict):
        items = sorted(selection.items())
    else:
        items = [
            (str(row.get("code") or ""), str(row.get("value_code") or ""))
            for row in selection
            if isinstance(row, dict)
        ]
    chars: list[dict[str, str]] = []
    for code, value_code in items:
        if not code or not value_code or code in locked:
            continue
        tmpl = by_code.get(code)
        if tmpl is None:
            continue
        options = [cv for cv in (tmpl.get("characteristicValues") or []) if isinstance(cv, dict)]
        hit = next((opt for opt in options if _option_value_code(opt) == value_code), None)
        label = _option_text(hit) if hit is not None else value_code
        name = str(tmpl.get("name") or code).strip()
        chars.append(
            {
                "display_label": name,
                "characteristic_code": code,
                "value_display": label,
            }
        )
    return chars


def build_pp_dump_combos(
    items: list[dict[str, Any]],
    *,
    ktru_code: str,
    template: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    wanted = str(ktru_code or "").strip()
    locked = locked_code_set(template)
    required = required_unlocked_codes(template)
    by_hash: dict[str, dict[str, Any]] = {}
    skipped_ktru = 0
    skipped_empty = 0
    skipped_incomplete = 0
    used = 0
    dates: list[str] = []
    used_items: list[dict[str, Any]] = []
    for raw in items:
        kobj = raw.get("ktru") if isinstance(raw.get("ktru"), dict) else {}
        code = str(kobj.get("code") or "").strip()
        if wanted and code != wanted:
            skipped_ktru += 1
            continue
        offer = raw.get("offer") if isinstance(raw.get("offer"), dict) else {}
        status = str(offer.get("status") or "").strip()
        if status and status.lower() != "published":
            skipped_empty += 1
            continue
        selection = find_selection_pairs(raw, template)
        if not selection:
            skipped_empty += 1
            continue
        if required - {row["code"] for row in selection}:
            skipped_incomplete += 1
            continue
        pairs = sorted(demand_key(selection, locked=locked))
        if not pairs:
            skipped_empty += 1
            continue
        hid = _combo_hash(list(pairs))
        chars = _chars_from_selection(selection, template or {}, locked)
        item = by_hash.setdefault(
            hid,
            {
                "hash": hid,
                "n": 0,
                "chars": chars,
                "preview": "; ".join(f"{ch['display_label']}={ch['value_display']}" for ch in chars) or "—",
                "prices": [],
            },
        )
        item["n"] += 1
        used += 1
        used_items.append(raw)
        price = unit_price_from_finding({"find": raw})
        if price is not None:
            item["prices"].append(price)
        for key in ("createDateTime", "lastModifyDateTime", "offerPriceListItemLinkDate"):
            dt = str(raw.get(key) or "")[:10]
            if dt:
                dates.append(dt)
                break
    combos = sorted(by_hash.values(), key=lambda c: (-int(c["n"]), str(c["hash"])))
    period = (min(dates), max(dates)) if dates else ("", "")
    stats = {
        "items": used + skipped_empty + skipped_incomplete + skipped_ktru,
        "used": used,
        "skipped_empty": skipped_empty,
        "skipped_incomplete": skipped_incomplete,
        "skipped_ktru": skipped_ktru,
        "combos": len(combos),
        "period": period,
        "used_items": used_items,
    }
    return combos, stats


def frequent_regions_from_pp_items(
    items: list[dict[str, Any]],
    *,
    min_regions: int = BIG_REGION_MIN,
    limit: int = FREQUENT_REGION_LIMIT,
) -> list[dict[str, Any]]:
    catalog = load_subject_oktmo()
    by_code = {row.code: row for row in catalog}
    catalog_codes = set(by_code)
    counts: Counter[str] = Counter()
    big_n = 0
    for raw in items:
        places = raw.get("deliveryPlaces") if isinstance(raw.get("deliveryPlaces"), list) else []
        if len(places) < int(min_regions):
            continue
        codes = subject_codes_from_find(raw, catalog_codes)
        if not codes:
            continue
        big_n += 1
        counts.update(codes)
    total = big_n or 1
    take = max(0, int(limit))
    items_out: list[dict[str, Any]] = []
    for code, n in counts.most_common(take):
        subj = by_code.get(code)
        items_out.append(
            {
                "code": code,
                "name": subj.name if subj else code,
                "pp_n": n,
                "share": round(n / total, 4),
            }
        )
    return items_out


def import_pp_dump_core(
    analytics_db: Path,
    text: str,
    *,
    ktru_code: str,
    template: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    import_findings: bool = True,
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    if not code:
        raise PpDumpImportError("Укажите код КТРУ.")
    items = iter_pp_dump_items(text)
    if not items:
        raise PpDumpImportError("В файле нет предварительных предложений.")
    tmpl = resolve_ktru_template(code, cache_dir=cache_dir, template=template)
    if tmpl is None:
        tmpl = template_from_pp_dump_items(items, ktru_code=code)
        if tmpl is not None and cache_dir is not None:
            save_cached_template(cache_dir, code, tmpl)
    if tmpl is None:
        raise CoreSnapshotError(f"Нет шаблона РТС для КТРУ {code}.")
    combos, stats = build_pp_dump_combos(items, ktru_code=code, template=tmpl)
    if not combos:
        raise PpDumpImportError(f"Нет ПП с характеристиками шаблона РТС по КТРУ {code}.")
    demand = _demand_map_mod()
    core, total, share = demand.select_core(combos)
    if not core:
        core = combos[:1]
        total = stats["used"]
        share = (int(core[0]["n"]) / total) if total else 0.0
    regions = frequent_regions_from_pp_items(stats["used_items"])
    if not regions:
        regions = frequent_regions_from_pp_items(stats["used_items"], min_regions=1)
    findings = None
    if import_findings:
        findings = import_registry_find_dump(analytics_db, text, ktru_code=code, source="dump")
    meta = {
        "ktru": code,
        "source": "pp",
        "period": list(stats["period"]),
        "notices": total,
        "contracts": stats["items"],
        "used": stats["used"],
        "skipped_empty": stats["skipped_empty"],
        "skipped_incomplete": stats["skipped_incomplete"],
        "core_n": len(core),
        "core_share": round(share, 4),
        "core_target": demand.CORE_TARGET,
        "core_max": demand.CORE_MAX,
        "has_rts_template": True,
        "nmck_frequent_regions": regions,
    }
    variants = persist_core_snapshot(
        analytics_db,
        code,
        core,
        tmpl,
        total_notices=total,
        core_meta=meta,
        replace=True,
    )
    return {
        "ok": True,
        "ktru_code": code,
        "meta": meta,
        "variants": variants,
        "findings": findings,
        "stats": {
            "items": stats["items"],
            "used": stats["used"],
            "skipped_empty": stats["skipped_empty"],
            "skipped_incomplete": stats["skipped_incomplete"],
            "skipped_ktru": stats["skipped_ktru"],
            "combos": stats["combos"],
            "core_n": len(core),
            "core_share": round(share, 4),
            "regions_n": len(regions),
            "findings_imported": int((findings or {}).get("imported") or 0),
        },
    }

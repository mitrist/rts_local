"""Demand-core snapshot: EIS combos mapped to RTS characteristic value_codes."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

from catalog_pp_draft import is_locked_characteristic
from eis_analytics_db import (
    list_pp_core_ktru_codes,
    list_pp_core_variants,
    save_pp_core_snapshot,
)
from rts_ktru_cache import cache_dir_default, load_cached_template

CHAIR_KTRU_CODE = "31.01.11.150-00000003"
CABINET_KTRU_CODE = "31.01.12.139-00000001"
STUDENT_CHAIR_KTRU_CODE = "31.01.11.150-00000021"
SOAP_KTRU_CODE = "20.41.31.130-00000003"
MAP_READY = "ready"
MAP_VALIDATE = "needs_validation"
MAP_GAP = "gap"
_STATUS_RANK = {MAP_READY: 0, MAP_VALIDATE: 1, MAP_GAP: 2}
_EIS_TYPE_KIND_RE = re.compile(r"\s*\[тип\s+\d+\](?:\s*вид\s+\d+)?\s*$", re.IGNORECASE)


class CoreSnapshotError(ValueError):
    """Cannot build or refresh a demand-core snapshot."""


def _demand_map_mod():
    path = Path(__file__).resolve().parent / "tools" / "ktru_demand_map.py"
    spec = importlib.util.spec_from_file_location("ktru_demand_map", path)
    if spec is None or spec.loader is None:
        raise CoreSnapshotError("Не найден tools/ktru_demand_map.py.")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("ё", "е").split())


def _worse_status(left: str, right: str) -> str:
    if _STATUS_RANK.get(right, 2) > _STATUS_RANK.get(left, 2):
        return right
    return left


def _option_text(opt: dict[str, Any]) -> str:
    for key in ("qualityDescription", "concreteValue", "productCharacteristic"):
        text = str(opt.get(key) or "").strip()
        if text:
            return text
    min_v = str(opt.get("minValue") or "").strip()
    max_v = str(opt.get("maxValue") or "").strip()
    if min_v or max_v:
        return f"{min_v}…{max_v}".strip("…")
    return str(opt.get("valueCode") or opt.get("id") or "").strip()


def _option_value_code(opt: dict[str, Any]) -> str:
    return str(opt.get("valueCode") or "").strip() or str(opt.get("id") or "").strip()


def _preview(chars: list[dict[str, Any]], *, limit: int = 8) -> str:
    parts: list[str] = []
    for ch in chars:
        if not isinstance(ch, dict):
            continue
        lab = str(ch.get("display_label") or ch.get("match_key") or ch.get("characteristic_code") or "").strip()
        val = str(ch.get("value_display") or "").strip()
        if lab and val:
            parts.append(f"{lab}={val}")
        elif val:
            parts.append(val)
        if len(parts) >= limit:
            break
    extra = max(0, len(chars) - limit)
    text = "; ".join(parts) if parts else "—"
    if extra > 0:
        text += f"; +{extra}"
    return text


def _template_indexes(template: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_code: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for ch in template.get("characteristics") or []:
        if not isinstance(ch, dict) or ch.get("isActual") is False:
            continue
        code = str(ch.get("code") or "").strip()
        name = _norm(str(ch.get("name") or ""))
        if code:
            by_code[code] = ch
        if name:
            by_name[name] = ch
    return by_code, by_name


def _value_candidates(value: str) -> list[str]:
    raw = str(value or "").strip()
    out: list[str] = []
    for cand in (raw, _EIS_TYPE_KIND_RE.sub("", raw).strip()):
        nv = _norm(cand)
        if nv and nv not in out:
            out.append(nv)
    return out


def _display_as_numeric_cv(value: str) -> dict[str, Any] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = [p.strip() for p in re.split(r"…|\.\.\.", raw) if p.strip()]
    if len(parts) == 2:
        return {
            "minValue": parts[0],
            "maxValue": parts[1],
            "minMathNotation": "ge",
            "maxMathNotation": "le",
        }
    compact = raw.replace(" ", "")
    if re.fullmatch(r"\d+(?:[.,]\d+)?", compact):
        return {"minValue": raw, "minMathNotation": "ge"}
    return None


def _match_option(options: list[dict[str, Any]], value: str) -> tuple[dict[str, Any] | None, str]:
    cands = _value_candidates(value)
    if not cands:
        return None, ""
    want = _norm(value)
    for opt in options:
        if _norm(_option_text(opt)) == want:
            return opt, "exact"
    for nv in cands:
        for opt in options:
            qd = str(opt.get("qualityDescription") or "").strip()
            conc = str(opt.get("concreteValue") or "").strip()
            if _norm(qd) == nv or _norm(conc) == nv:
                return opt, "exact"
    from notice_to_rts_pp import parse_notice_value
    from rts_pp_api import _numeric_option_contains, _numeric_option_exact_match

    parsed = _display_as_numeric_cv(value)
    if parsed:
        for opt in options:
            if _numeric_option_exact_match(parsed, opt):
                return opt, "exact"

    notice = parse_notice_value(_EIS_TYPE_KIND_RE.sub("", str(value or "")).strip())
    if notice.get("minValue") or notice.get("maxValue") or notice.get("concreteValue"):
        for opt in options:
            if _numeric_option_exact_match(notice, opt):
                return opt, "exact"
        for opt in options:
            if _numeric_option_contains(notice, opt):
                return opt, "substring"
    for nv in cands:
        for opt in options:
            qd = _norm(str(opt.get("qualityDescription") or ""))
            conc = _norm(str(opt.get("concreteValue") or ""))
            if qd and (nv in qd or qd in nv):
                return opt, "substring"
            if conc and (nv in conc or conc in nv):
                return opt, "substring"
    return None, ""


def selection_eis_hash(chars: list[Any] | None) -> str:
    """Stable short hash of an RTS code/value_code fingerprint."""
    import hashlib

    key = demand_key(chars)
    if not key:
        return ""
    raw = ";".join(f"{code}={value}" for code, value in sorted(key))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def variant_selection_key(row: dict[str, Any] | None) -> frozenset[tuple[str, str]]:
    """RTS uniqueness fingerprint: all mapped code/value_code pairs, including locked."""
    if not isinstance(row, dict):
        return frozenset()
    chars = row.get("characteristics")
    return demand_key(chars)


def mark_duplicate_selections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set rts_dup_of to the first variant_no with the same RTS fingerprint."""
    first: dict[frozenset[tuple[str, str]], int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = variant_selection_key(row)
        no = int(row.get("variant_no") or 0)
        if not key or no < 1:
            row["rts_dup_of"] = None
            continue
        seen = first.get(key)
        if seen is None:
            first[key] = no
            row["rts_dup_of"] = None
        else:
            row["rts_dup_of"] = seen
    return rows


def variant_can_enqueue(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    status = str(row.get("map_status") or "")
    if status not in (MAP_READY, MAP_VALIDATE):
        return False
    if row.get("rts_dup_of"):
        return False
    chars = row.get("characteristics")
    if not isinstance(chars, list) or not chars:
        return False
    return any(
        isinstance(ch, dict)
        and str(ch.get("code") or "").strip()
        and str(ch.get("value_code") or "").strip()
        for ch in chars
    )


def map_eis_char_to_rts(
    ch: dict[str, Any],
    *,
    by_code: dict[str, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
) -> dict[str, str]:
    lab = str(
        ch.get("display_label") or ch.get("eis_char") or ch.get("name") or ch.get("match_key") or ""
    ).strip()
    eis_code = str(ch.get("characteristic_code") or ch.get("code") or "").strip()
    val = str(ch.get("value_display") or ch.get("eis_value") or ch.get("value") or "").strip()
    tmpl = by_code.get(eis_code) if eis_code else None
    if tmpl is None:
        tmpl = by_name.get(_norm(lab))
    if tmpl is None:
        return {
            "eis_char": lab or eis_code,
            "eis_value": val,
            "code": "",
            "value_code": "",
            "status": MAP_GAP,
            "reason": "нет поля в шаблоне РТС",
        }
    code = str(tmpl.get("code") or "").strip()
    options = [cv for cv in (tmpl.get("characteristicValues") or []) if isinstance(cv, dict)]
    if not options:
        return {
            "eis_char": lab or eis_code,
            "eis_value": val,
            "code": code,
            "value_code": "",
            "status": MAP_VALIDATE,
            "reason": "свободное значение, нет value_code",
        }
    hit, kind = _match_option(options, val)
    if hit is None:
        return {
            "eis_char": lab or eis_code,
            "eis_value": val,
            "code": code,
            "value_code": "",
            "status": MAP_GAP,
            "reason": "нет значения в шаблоне РТС",
        }
    value_code = _option_value_code(hit)
    if not value_code:
        return {
            "eis_char": lab or eis_code,
            "eis_value": val,
            "code": code,
            "value_code": "",
            "status": MAP_VALIDATE,
            "reason": "у опции РТС нет value_code",
        }
    if kind == "exact":
        return {
            "eis_char": lab or eis_code,
            "eis_value": val,
            "code": code,
            "value_code": value_code,
            "status": MAP_READY,
            "reason": "",
        }
    return {
        "eis_char": lab or eis_code,
        "eis_value": val,
        "code": code,
        "value_code": value_code,
        "status": MAP_VALIDATE,
        "reason": "сопоставление по подстроке",
    }


def locked_code_set(template: dict[str, Any] | None) -> set[str]:
    if not isinstance(template, dict) or not template:
        return set()
    return {row["code"] for row in locked_selections(template) if row.get("code")}


def demand_key(
    selection: dict[str, str] | list[Any] | None,
    *,
    locked: set[str] | None = None,
) -> frozenset[tuple[str, str]]:
    skip = locked or set()
    pairs: list[tuple[str, str]] = []
    if isinstance(selection, dict):
        items = selection.items()
    elif isinstance(selection, list):
        items = (
            (str(ch.get("code") or ""), str(ch.get("value_code") or ""))
            for ch in selection
            if isinstance(ch, dict)
        )
    else:
        items = ()
    for code, value_code in items:
        c = str(code or "").strip()
        v = str(value_code or "").strip()
        if c and v and c not in skip:
            pairs.append((c, v))
    return frozenset(pairs)


def _map_raw_characteristic_value(
    raw_val: dict[str, Any],
    options: list[dict[str, Any]],
) -> str:
    vc = str(raw_val.get("valueCode") or "").strip()
    if vc and any(_option_value_code(opt) == vc for opt in options):
        return vc
    hit, _kind = _match_option(options, _option_text(raw_val))
    if hit is None:
        from rts_pp_api import _resolve_value_option

        hit = _resolve_value_option(raw_val, options)
    if hit is None:
        rid = str(raw_val.get("id") or "").strip()
        if rid:
            hit = next((opt for opt in options if str(opt.get("id") or "").strip() == rid), None)
    return _option_value_code(hit) if hit is not None else ""


def find_selection_pairs(
    find_item: dict[str, Any] | None,
    template: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Map competitor/PP ktruCharacteristics to all RTS code/value_code pairs.

    Multi-select fields (several ``characteristicValues``) keep every mapped value.
    """
    if not isinstance(find_item, dict) or not isinstance(template, dict) or not template:
        return []
    by_code, by_name = _template_indexes(template)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    raw_chars = find_item.get("ktruCharacteristics")
    if not isinstance(raw_chars, list):
        return out
    for ch in raw_chars:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or ch.get("characteristicCode") or "").strip()
        name = str(ch.get("name") or ch.get("characteristicName") or "").strip()
        tmpl = by_code.get(code) if code else None
        if tmpl is None:
            tmpl = by_name.get(_norm(name))
        if tmpl is None:
            continue
        tcode = str(tmpl.get("code") or "").strip()
        options = [cv for cv in (tmpl.get("characteristicValues") or []) if isinstance(cv, dict)]
        vals = [v for v in (ch.get("characteristicValues") or []) if isinstance(v, dict)]
        if not tcode or not vals:
            continue
        for raw_val in vals:
            mapped = _map_raw_characteristic_value(raw_val, options)
            if not mapped:
                continue
            pair = (tcode, mapped)
            if pair in seen:
                continue
            seen.add(pair)
            out.append({"code": tcode, "value_code": mapped})
    return out


def find_selection(find_item: dict[str, Any] | None, template: dict[str, Any] | None) -> dict[str, str]:
    """Map competitor find ktruCharacteristics to {code: last value_code} via Ktru/get template."""
    out: dict[str, str] = {}
    for row in find_selection_pairs(find_item, template):
        out[row["code"]] = row["value_code"]
    return out


def empty_price_stats() -> dict[str, Any]:
    return {
        "price_ladder": [],
        "pp_n": 0,
        "rungs": 0,
        "top5": {"open_slots": 5, "need_le": None, "note": "нет ПП в опросе"},
    }


def price_ladder_from_prices(prices: list[float]) -> dict[str, Any]:
    ordered = sorted(round(float(p), 2) for p in prices)
    if not ordered:
        return empty_price_stats()
    counts: dict[float, int] = {}
    for p in ordered:
        counts[p] = counts.get(p, 0) + 1
    ladder = [{"i": i, "price": price, "n": n} for i, (price, n) in enumerate(sorted(counts.items()), start=1)]
    n = len(ordered)
    if n < 5:
        open_slots = 5 - n
        top5 = {
            "open_slots": open_slots,
            "need_le": None,
            "note": f"свободно {open_slots} из 5",
        }
    else:
        need_le = ordered[4]
        top5 = {
            "open_slots": 0,
            "need_le": need_le,
            "note": "вход в 5: не дороже 5-го ПП",
        }
    return {
        "price_ladder": ladder,
        "pp_n": n,
        "rungs": len(ladder),
        "top5": top5,
    }


def locked_selections(template: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for ch in template.get("characteristics") or []:
        if not isinstance(ch, dict) or ch.get("isActual") is False:
            continue
        if not is_locked_characteristic(ch):
            continue
        code = str(ch.get("code") or "").strip()
        options = [cv for cv in (ch.get("characteristicValues") or []) if isinstance(cv, dict)]
        if not code or not options:
            continue
        value_code = _option_value_code(options[0])
        if not value_code:
            continue
        out.append({"code": code, "value_code": value_code})
    return out


def map_combo_to_rts(combo_chars: list[Any], template: dict[str, Any] | None) -> dict[str, Any]:
    chars = [ch for ch in combo_chars if isinstance(ch, dict)]
    if not isinstance(template, dict) or not template:
        return {
            "map_status": MAP_GAP,
            "map_reason": "нет шаблона РТС",
            "characteristics": [],
            "fields": [],
            "preview": _preview(chars),
        }
    by_code, by_name = _template_indexes(template)
    selections = locked_selections(template)
    locked_codes = {row["code"] for row in selections}
    seen_pairs = {(row["code"], row["value_code"]) for row in selections}
    fields: list[dict[str, str]] = []
    status = MAP_READY
    reasons: list[str] = []
    if not chars:
        status = MAP_VALIDATE
        reasons.append("в комбинации ЕИС нет характеристик")
    for ch in chars:
        mapped = map_eis_char_to_rts(ch, by_code=by_code, by_name=by_name)
        fields.append(mapped)
        status = _worse_status(status, mapped["status"])
        if mapped["reason"]:
            reasons.append(f"{mapped['eis_char']}: {mapped['reason']}")
        code = mapped.get("code") or ""
        value_code = mapped.get("value_code") or ""
        if not code or not value_code or code in locked_codes:
            continue
        pair = (code, value_code)
        if pair in seen_pairs:
            continue
        selections.append({"code": code, "value_code": value_code})
        seen_pairs.add(pair)
    return {
        "map_status": status,
        "map_reason": "; ".join(reasons)[:2000],
        "characteristics": selections,
        "fields": fields,
        "preview": _preview(chars),
    }


def resolve_ktru_template(
    ktru_code: str,
    *,
    cache_dir: Path | None = None,
    template: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if isinstance(template, dict) and template:
        return template
    code = str(ktru_code or "").strip()
    if not code or cache_dir is None:
        return None
    return load_cached_template(cache_dir, code, ttl_sec=0)


def build_snapshot_rows(
    ktru_code: str,
    core: list[dict[str, Any]],
    template: dict[str, Any] | None,
    *,
    existing: list[dict[str, Any]] | None = None,
    total_notices: int = 0,
    core_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    code = str(ktru_code or "").strip()
    by_hash = {str(row.get("eis_hash") or ""): row for row in (existing or [])}
    max_no = 0
    for row in existing or []:
        try:
            max_no = max(max_no, int(row.get("variant_no") or 0))
        except (TypeError, ValueError):
            continue
    total = int(total_notices or 0)
    if total <= 0:
        total = sum(int(c.get("n") or 0) for c in core) or 1
    meta = dict(core_meta or {})
    rows: list[dict[str, Any]] = []
    for combo in core:
        eis_hash = str(combo.get("hash") or combo.get("eis_hash") or "").strip()
        if not eis_hash:
            continue
        mapped = map_combo_to_rts(list(combo.get("chars") or []), template)
        n = int(combo.get("n") or 0)
        share = (n / total) if total else 0.0
        known = by_hash.get(eis_hash)
        if known is not None:
            variant_no = int(known.get("variant_no") or 0)
        else:
            max_no += 1
            variant_no = max_no
        row_meta = dict(meta)
        raw_prices = combo.get("prices")
        if isinstance(raw_prices, list):
            prices = []
            for item in raw_prices:
                try:
                    value = float(item)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    prices.append(round(value, 2))
            if prices:
                row_meta["nmck_prices"] = prices
        rows.append(
            {
                "ktru_code": code,
                "variant_no": variant_no,
                "eis_hash": eis_hash,
                "n": n,
                "share": round(share, 6),
                "map_status": mapped["map_status"],
                "map_reason": mapped["map_reason"],
                "chars_preview": str(combo.get("preview") or mapped["preview"]),
                "characteristics": mapped["characteristics"],
                "core_meta": row_meta,
            }
        )
    rows.sort(key=lambda row: int(row["variant_no"]))
    return rows


def persist_core_snapshot(
    analytics_db: Path,
    ktru_code: str,
    core: list[dict[str, Any]],
    template: dict[str, Any] | None,
    *,
    total_notices: int = 0,
    core_meta: dict[str, Any] | None = None,
    replace: bool = False,
) -> list[dict[str, Any]]:
    code = str(ktru_code or "").strip()
    existing = [] if replace else list_pp_core_variants(analytics_db, code)
    meta = dict(core_meta or {})
    if isinstance(template, dict) and template.get("version") is not None:
        meta["template_version"] = template.get("version")
    rows = build_snapshot_rows(
        code,
        core,
        template,
        existing=existing,
        total_notices=total_notices,
        core_meta=meta,
    )
    save_pp_core_snapshot(analytics_db, code, rows)
    return list_pp_core_variants(analytics_db, code)


def refresh_core_snapshot(
    analytics_db: Path,
    history_db: Path,
    ktru_code: str,
    *,
    cache_dir: Path | None = None,
    template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    if not code:
        raise CoreSnapshotError("Укажите код КТРУ.")
    if not Path(history_db).is_file():
        raise CoreSnapshotError("База eis_history недоступна (EIS_HISTORY_DB).")
    demand = _demand_map_mod()
    rows = demand.load_rows(Path(history_db), code)
    if not rows:
        raise CoreSnapshotError(f"В eis_history нет строк КТРУ {code}.")
    combos = demand.build_combos(rows)
    core, total, share = demand.select_core(combos)
    tmpl = resolve_ktru_template(code, cache_dir=cache_dir, template=template)
    dates = [str(r.get("observed_at_utc") or "") for r in rows if r.get("observed_at_utc")]
    period = (min(dates)[:10], max(dates)[:10]) if dates else ("", "")
    meta = {
        "ktru": code,
        "period": period,
        "notices": total,
        "core_n": len(core),
        "core_share": round(share, 4),
        "core_target": demand.CORE_TARGET,
        "core_max": demand.CORE_MAX,
        "has_rts_template": bool(tmpl),
    }
    variants = persist_core_snapshot(
        analytics_db,
        code,
        core,
        tmpl,
        total_notices=total,
        core_meta=meta,
    )
    return {"ok": True, "ktru_code": code, "meta": meta, "variants": variants}


def default_core_ktru(analytics_db: Path) -> str:
    codes = list_pp_core_ktru_codes(analytics_db)
    if CHAIR_KTRU_CODE in codes:
        return CHAIR_KTRU_CODE
    if CABINET_KTRU_CODE in codes:
        return CABINET_KTRU_CODE
    if codes:
        return codes[0]
    return CHAIR_KTRU_CODE


def cache_dir_from_meta(meta_dir: Path) -> Path:
    return cache_dir_default(meta_dir)


def _chars_from_preview(preview: str) -> list[dict[str, str]]:
    chars: list[dict[str, str]] = []
    text = str(preview or "").strip()
    if not text or text == "—":
        return chars
    for part in text.split(";"):
        chunk = part.strip()
        if "=" not in chunk:
            continue
        lab, val = chunk.split("=", 1)
        name = lab.strip()
        value = val.strip()
        if name and value:
            chars.append({"display_label": name, "value_display": value})
    return chars


_CODEISH_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-z]+$", re.IGNORECASE)


def _is_codeish(text: str) -> bool:
    return bool(_CODEISH_RE.fullmatch(str(text or "").strip()))


def display_mapped_characteristics(
    characteristics: list[Any] | None,
    template: dict[str, Any] | None = None,
    *,
    chars_preview: str = "",
) -> list[dict[str, str]]:
    """Human-readable parameter/value rows for a mapped RTS selection."""
    preview_rows = _chars_from_preview(chars_preview)
    by_code: dict[str, dict[str, Any]] = {}
    if isinstance(template, dict) and template:
        by_code, _by_name = _template_indexes(template)
    rows: list[dict[str, str]] = []
    source = characteristics if isinstance(characteristics, list) else []
    for ch in source:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or ch.get("characteristic_code") or "").strip()
        value_code = str(ch.get("value_code") or ch.get("valueCode") or "").strip()
        label = str(ch.get("display_label") or ch.get("eis_char") or ch.get("name") or "").strip()
        value = str(ch.get("value_display") or ch.get("eis_value") or ch.get("value") or "").strip()
        tmpl = by_code.get(code) if code else None
        if tmpl is not None:
            name = str(tmpl.get("name") or "").strip()
            if name:
                label = name
            options = [cv for cv in (tmpl.get("characteristicValues") or []) if isinstance(cv, dict)]
            hit = next((opt for opt in options if _option_value_code(opt) == value_code), None)
            if hit is not None:
                text = _option_text(hit)
                if text:
                    value = text
        if (not value or _is_codeish(value)) and label:
            for prev in preview_rows:
                if prev["display_label"] == label:
                    value = prev["value_display"]
                    break
        if not label:
            label = code
        if not value:
            value = value_code
        if not label and not value:
            continue
        rows.append(
            {
                "display_label": label or "—",
                "value_display": value or "—",
                "code": code,
                "value_code": value_code,
            }
        )
    if preview_rows and (not rows or all(_is_codeish(r["display_label"]) for r in rows)):
        return [
            {
                "display_label": prev["display_label"],
                "value_display": prev["value_display"],
                "code": "",
                "value_code": "",
            }
            for prev in preview_rows
        ]
    return rows


def sync_core_snapshot_template(
    analytics_db: Path,
    ktru_code: str,
    template: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Re-map stored core selections onto the current Ktru/get dictionary."""
    code = str(ktru_code or "").strip()
    rows = list_pp_core_variants(analytics_db, code)
    if not rows or not isinstance(template, dict) or not template:
        return rows
    ver = template.get("version")
    meta0 = rows[0].get("core_meta") if isinstance(rows[0].get("core_meta"), dict) else {}
    if ver is not None and meta0.get("template_version") == ver:
        return rows
    updated: list[dict[str, Any]] = []
    for row in rows:
        preview_chars = _chars_from_preview(str(row.get("chars_preview") or ""))
        if not preview_chars:
            existing = row.get("characteristics")
            if isinstance(existing, list):
                preview_chars = [
                    {
                        "display_label": str(ch.get("code") or ""),
                        "characteristic_code": str(ch.get("code") or ""),
                        "value_display": str(ch.get("value_code") or ""),
                    }
                    for ch in existing
                    if isinstance(ch, dict)
                ]
        mapped = map_combo_to_rts(preview_chars, template)
        meta = dict(row.get("core_meta") or {}) if isinstance(row.get("core_meta"), dict) else {}
        if ver is not None:
            meta["template_version"] = ver
        updated.append(
            {
                **row,
                "characteristics": mapped["characteristics"],
                "map_status": mapped["map_status"],
                "map_reason": mapped["map_reason"],
                "core_meta": meta,
            }
        )
    save_pp_core_snapshot(analytics_db, code, updated)
    return list_pp_core_variants(analytics_db, code)

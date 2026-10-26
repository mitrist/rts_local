"""Match RTS ЭЗТ lot-item characteristics to a virtual PP set."""

from __future__ import annotations

from typing import Any

from pp_core_catalog import (
    MAP_GAP,
    _EIS_TYPE_KIND_RE,
    _norm,
    demand_key,
    display_mapped_characteristics,
    find_selection_pairs,
    locked_code_set,
    map_combo_to_rts,
)
from region_aliases import resolve_region
from virtual_pp_set import VIRTUAL_DELIVERY_DAYS_MAX, VIRTUAL_DELIVERY_DAYS_MIN
from virtual_pp_store import CHAR_HIT, CHAR_MISS, CHAR_UNMAPPED

DEFAULT_QTY_RISK = 0


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


def lot_item_to_find_item(item: Any) -> dict[str, Any]:
    """Convert lot-items Characteristics (Name + quality/min/max) to find_selection shape."""
    chars_raw: list[Any]
    if hasattr(item, "characteristics"):
        chars_raw = list(item.characteristics or [])
    elif isinstance(item, dict):
        chars_raw = list(item.get("Characteristics") or item.get("characteristics") or [])
    else:
        chars_raw = []
    out_chars: list[dict[str, Any]] = []
    for ch in chars_raw:
        if hasattr(ch, "name"):
            name = str(ch.name or "")
            values = []
            for val in ch.values or []:
                values.append(
                    {
                        "qualityDescription": getattr(val, "quality_description", None),
                        "concreteValue": getattr(val, "concrete_value", None),
                        "minValue": getattr(val, "min_value", None),
                        "maxValue": getattr(val, "max_value", None),
                    }
                )
        elif isinstance(ch, dict):
            name = str(ch.get("Name") or ch.get("name") or "")
            raw_vals = ch.get("CharactericticValues") or ch.get("CharacteristicValues") or ch.get("values") or []
            values = []
            if isinstance(raw_vals, list):
                for val in raw_vals:
                    if not isinstance(val, dict):
                        continue
                    values.append(
                        {
                            "qualityDescription": val.get("QualityDescription") or val.get("qualityDescription"),
                            "concreteValue": val.get("ConcreteValue") or val.get("concreteValue"),
                            "minValue": val.get("MinValue") or val.get("minValue"),
                            "maxValue": val.get("MaxValue") or val.get("maxValue"),
                        }
                    )
        else:
            continue
        if name:
            out_chars.append({"name": name, "characteristicValues": values})
    return {"ktruCharacteristics": out_chars}


def lot_item_chars_preview(item: Any) -> list[dict[str, Any]]:
    find_item = lot_item_to_find_item(item)
    preview: list[dict[str, Any]] = []
    for ch in find_item.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict):
            continue
        vals = ch.get("characteristicValues") or []
        val0 = vals[0] if vals and isinstance(vals[0], dict) else {}
        text = str(
            val0.get("qualityDescription")
            or val0.get("concreteValue")
            or "…".join(
                x for x in (str(val0.get("minValue") or ""), str(val0.get("maxValue") or "")) if x
            )
            or ""
        ).strip()
        preview.append({"name": str(ch.get("name") or ""), "value": text})
    return preview


def map_lot_item_selection(
    item: Any,
    template: dict[str, Any] | None,
) -> tuple[list[dict[str, str]], str]:
    find_item = lot_item_to_find_item(item)
    named = [ch for ch in find_item.get("ktruCharacteristics") or [] if str(ch.get("name") or "").strip()]
    if not named:
        return [], CHAR_UNMAPPED
    selection = find_selection_pairs(find_item, template)
    codes = {row["code"] for row in selection}
    if len(codes) < len(named):
        return selection, CHAR_UNMAPPED
    return selection, "ok"


def _region_codes(row: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    regions = row.get("regions")
    if not isinstance(regions, list):
        return codes
    for item in regions:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("oktmo") or "").strip()
        if code:
            codes.add(code)
    return codes


def region_offer_unit_price(row: dict[str, Any] | None, subject_oktmo: str) -> float | None:
    want = str(subject_oktmo or "").strip()
    if not want or not isinstance(row, dict):
        return None
    regions = row.get("regions")
    if not isinstance(regions, list):
        return None
    for item in regions:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("oktmo") or "").strip()
        if code != want:
            continue
        raw = item.get("unit_price")
        if raw is None:
            return None
        try:
            return float(str(raw).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            return None
    return None


def _variant_has_display_mismatch(
    row: dict[str, Any],
    notice_chars: list[Any] | None,
    template: dict[str, Any] | None,
) -> bool:
    """True when a named notice value disagrees with the variant (as in the journal modal)."""
    if not notice_chars or not isinstance(row, dict):
        return False
    virtual = display_mapped_characteristics(
        row.get("characteristics") if isinstance(row.get("characteristics"), list) else [],
        template if isinstance(template, dict) else None,
        chars_preview=str(row.get("chars_preview") or ""),
    )
    rows = compare_virtual_notice_characteristics(virtual, notice_chars)
    return any(item.get("row_class") == "mismatch" for item in rows)


def pick_best_variant(
    notice_key: frozenset[tuple[str, str]],
    variants: list[dict[str, Any]],
    *,
    locked: set[str] | None = None,
    required_oktmo: str | None = None,
    notice_chars: list[Any] | None = None,
    template: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    skip = locked or set()
    want = str(required_oktmo or "").strip()
    hits: list[dict[str, Any]] = []
    for row in variants:
        variant_key = demand_key(row.get("characteristics"), locked=skip)
        if notice_key and notice_key <= variant_key:
            if want:
                if want not in _region_codes(row):
                    continue
            if _variant_has_display_mismatch(row, notice_chars, template):
                continue
            hits.append(row)
    if not hits:
        return None
    hits.sort(
        key=lambda row: (
            0 if int(row.get("in_core") or 0) else 1,
            len(demand_key(row.get("characteristics"), locked=skip)),
            int(row.get("variant_no") or 0),
        )
    )
    return hits[0]


def match_eis_html_line(
    chars: list[Any],
    variants: list[dict[str, Any]],
    template: dict[str, Any] | None,
    *,
    region_raw: str = "",
    subject_oktmo: str = "",
    nmck: float | None = None,
    quantity: float | None = None,
    unit_price: float | None = None,
    total: float | None = None,
    own_application: bool = False,
    qty_risk_threshold: float = DEFAULT_QTY_RISK,
) -> dict[str, Any]:
    """Match EIS HTML notice chars (display_label/value_display) to the virtual set.

    Same RTS mapping as demand core. Term is ignored.
    If variants carry ``regions``, subject ОКТМО must be in that matrix.
    """
    mapped = map_combo_to_rts(list(chars or []), template)
    locked = locked_code_set(template)
    selection: list[dict[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for row in mapped.get("characteristics") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        value_code = str(row.get("value_code") or "").strip()
        if not code or not value_code or (code, value_code) in seen_pairs:
            continue
        seen_pairs.add((code, value_code))
        selection.append({"code": code, "value_code": value_code})
    notice_key = demand_key(selection, locked=locked)
    qty = quantity
    region = resolve_region(region_raw)
    oktmo = str(subject_oktmo or region.subject_oktmo or "").strip()
    qty_risk = bool(qty_risk_threshold and qty is not None and qty >= float(qty_risk_threshold))
    preview = [
        {
            "name": str(ch.get("display_label") or ch.get("name") or ch.get("match_key") or ""),
            "value": str(ch.get("value_display") or ch.get("value") or ""),
        }
        for ch in chars
        if isinstance(ch, dict)
    ]
    event: dict[str, Any] = {
        "char_status": CHAR_MISS,
        "variant_no": None,
        "in_core": None,
        "subject_oktmo": region.subject_oktmo,
        "region_raw": region.raw or str(region_raw or "").strip(),
        "region_source": region.source,
        "unit_price": unit_price,
        "quantity": qty,
        "total": total,
        "nmck": nmck if nmck is not None else total,
        "own_application": bool(own_application),
        "qty_risk": qty_risk,
        "offer_unit_price": None,
        "delivery_ok": True,
        "delivery_days_min": VIRTUAL_DELIVERY_DAYS_MIN,
        "delivery_days_max": VIRTUAL_DELIVERY_DAYS_MAX,
        "characteristics": preview,
        "selection": selection,
        "map_status": mapped.get("map_status") or "",
        "map_reason": mapped.get("map_reason") or "",
    }
    event["subject_oktmo"] = oktmo or region.subject_oktmo
    gaps = [
        field
        for field in mapped.get("fields") or []
        if isinstance(field, dict) and field.get("status") == MAP_GAP
    ]
    if not template or not notice_key:
        event["char_status"] = CHAR_UNMAPPED
        return event
    require_region = any(_region_codes(row) for row in variants if isinstance(row, dict))
    required_oktmo = oktmo if require_region else ""
    if require_region and (not required_oktmo or required_oktmo == "unmapped"):
        event["char_status"] = CHAR_MISS
        return event
    hit = pick_best_variant(
        notice_key,
        variants,
        locked=locked,
        required_oktmo=required_oktmo or None,
        notice_chars=chars,
        template=template,
    )
    if hit is None:
        event["char_status"] = CHAR_UNMAPPED if gaps else CHAR_MISS
        return event
    event["char_status"] = CHAR_HIT
    event["variant_no"] = int(hit.get("variant_no") or 0)
    event["in_core"] = 1 if int(hit.get("in_core") or 0) else 0
    event["eis_hash"] = str(hit.get("eis_hash") or "")
    event["offer_unit_price"] = region_offer_unit_price(hit, required_oktmo or oktmo)
    return event


def match_lot_item(
    item: Any,
    variants: list[dict[str, Any]],
    template: dict[str, Any] | None,
    *,
    region_raw: str = "",
    nmck: float | None = None,
    own_application: bool = False,
    qty_risk_threshold: float = DEFAULT_QTY_RISK,
    delivery_days: int | None = None,
) -> dict[str, Any]:
    """Compare lot-item chars to virtual variants. Region is stats-only; term is ignored."""
    del delivery_days  # virtual corridor 1–100; never a match axis
    selection, map_status = map_lot_item_selection(item, template)
    locked = locked_code_set(template)
    notice_key = demand_key(selection, locked=locked)
    qty = _item_qty(item)
    unit_price = _item_unit_price(item)
    total = _item_total(item)
    region = resolve_region(region_raw)
    qty_risk = bool(qty_risk_threshold and qty is not None and qty >= float(qty_risk_threshold))
    event: dict[str, Any] = {
        "char_status": CHAR_MISS,
        "variant_no": None,
        "in_core": None,
        "subject_oktmo": region.subject_oktmo,
        "region_raw": region.raw or str(region_raw or "").strip(),
        "region_source": region.source,
        "unit_price": unit_price,
        "quantity": qty,
        "total": total,
        "nmck": nmck if nmck is not None else total,
        "own_application": bool(own_application),
        "qty_risk": qty_risk,
        "delivery_ok": True,
        "delivery_days_min": VIRTUAL_DELIVERY_DAYS_MIN,
        "delivery_days_max": VIRTUAL_DELIVERY_DAYS_MAX,
        "characteristics": lot_item_chars_preview(item),
        "selection": selection,
    }
    if map_status != "ok" or not notice_key:
        event["char_status"] = CHAR_UNMAPPED
        return event
    hit = pick_best_variant(
        notice_key,
        variants,
        locked=locked,
        notice_chars=event.get("characteristics") if isinstance(event.get("characteristics"), list) else None,
        template=template,
    )
    if hit is None:
        event["char_status"] = CHAR_MISS
        return event
    event["char_status"] = CHAR_HIT
    event["variant_no"] = int(hit.get("variant_no") or 0)
    event["in_core"] = 1 if int(hit.get("in_core") or 0) else 0
    event["eis_hash"] = str(hit.get("eis_hash") or "")
    return event


def _item_qty(item: Any) -> float | None:
    if hasattr(item, "quantity_text"):
        return _ru_float(item.quantity_text)
    if isinstance(item, dict):
        return _ru_float(item.get("QuantityTextValue") or item.get("Quantity") or item.get("quantity"))
    return None


def _item_unit_price(item: Any) -> float | None:
    if hasattr(item, "price_amount"):
        return _ru_float(item.price_amount)
    if isinstance(item, dict):
        price = item.get("PricePerItem")
        if isinstance(price, dict):
            return _ru_float(price.get("Amount") or price.get("AmountInString"))
        return _ru_float(item.get("unit_price"))
    return None


def _item_total(item: Any) -> float | None:
    if hasattr(item, "total_amount"):
        return _ru_float(item.total_amount)
    if isinstance(item, dict):
        total = item.get("TotalSum")
        if isinstance(total, dict):
            return _ru_float(total.get("Amount") or total.get("AmountInString"))
        return _ru_float(item.get("total"))
    return None


def _char_label(ch: dict[str, Any]) -> str:
    return str(
        ch.get("display_label") or ch.get("eis_char") or ch.get("name") or ch.get("match_key") or ""
    ).strip()


def _char_value(ch: dict[str, Any]) -> str:
    return str(
        ch.get("virtual_value")
        or ch.get("value_display")
        or ch.get("eis_value")
        or ch.get("value")
        or ""
    ).strip()


def _value_key(text: str) -> str:
    stripped = _EIS_TYPE_KIND_RE.sub("", str(text or "")).strip()
    return _norm(stripped)


def _join_unique_values(values: list[str]) -> str:
    seen: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.append(text)
    return "; ".join(seen)


def _collect_named_values(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ch in rows:
        label = _char_label(ch)
        value = _char_value(ch)
        key = _norm(label)
        if not key:
            continue
        if key not in by_key:
            order.append(key)
            by_key[key] = {"label": label, "values": []}
        if value and value not in by_key[key]["values"]:
            by_key[key]["values"].append(value)
    return by_key, order


def compare_virtual_notice_characteristics(
    virtual_rows: list[Any] | None,
    notice_rows: list[Any] | None,
) -> list[dict[str, str]]:
    """Align virtual PP chars with notice chars by normalized parameter name.

    Several values of the same parameter (multi-select) become one row. A hit
    requires every notice value of that parameter to be present on the variant;
    extra variant values are allowed.
    """
    virtual: list[dict[str, Any]] = [ch for ch in (virtual_rows or []) if isinstance(ch, dict)]
    notice: list[dict[str, Any]] = [ch for ch in (notice_rows or []) if isinstance(ch, dict)]
    vmap, vorder = _collect_named_values(virtual)
    nmap, norder = _collect_named_values(notice)
    used: set[str] = set()
    out: list[dict[str, str]] = []
    has_notice = bool(nmap)
    for key in vorder:
        used.add(key)
        virt_vals = list(vmap[key]["values"])
        notice_vals = list((nmap.get(key) or {}).get("values") or [])
        virt_text = _join_unique_values(virt_vals)
        notice_text = _join_unique_values(notice_vals)
        virt_keys = {_value_key(v) for v in virt_vals if _value_key(v)}
        notice_keys = {_value_key(v) for v in notice_vals if _value_key(v)}
        if has_notice and notice_keys and virt_keys and notice_keys <= virt_keys:
            row_class = "match"
        elif has_notice and notice_keys and virt_keys:
            row_class = "mismatch"
        elif has_notice and notice_keys and not virt_keys:
            row_class = "notice_only"
        elif has_notice and virt_keys and not notice_keys:
            row_class = "virtual_only"
        else:
            row_class = "neutral"
        out.append(
            {
                "display_label": str(vmap[key]["label"] or "—"),
                "value_display": virt_text,
                "virtual_value": virt_text,
                "notice_value": notice_text,
                "row_class": row_class,
            }
        )
    for key in norder:
        if key in used:
            continue
        hit = nmap[key]
        notice_text = _join_unique_values(list(hit.get("values") or []))
        out.append(
            {
                "display_label": str(hit.get("label") or "—"),
                "value_display": "",
                "virtual_value": "",
                "notice_value": notice_text,
                "row_class": "notice_only" if has_notice else "neutral",
            }
        )
    return out

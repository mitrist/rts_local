"""Map customer monitor find JSON into supplier PP draft items."""

from __future__ import annotations

from typing import Any

from notice_to_rts_pp import (
    ApplyResult,
    _allows_multiple_values,
    _compare_value_sets,
    _draft_char_maps,
    _draft_value_signatures,
    _norm_label,
    _value_signature,
    ensure_item_ktru_fields,
    fill_remaining_required_from_template,
    render_characteristic_values,
)
from participation_price import (
    compute_participation_economics,
    format_rts_unit_price_from_float,
    get_price_ord_catalog,
    participation_economics_disabled,
    parse_money_amount,
)
from rts_pp_api import (
    DEFAULT_VAT_RATE_CODE,
    _characteristic_value_for_api,
    _map_create_characteristic,
    _search_char_options,
    build_create_payload,
    format_rts_unit_price,
)

COMPETITOR_DRAFT_PRICE_FACTOR = 0.95


def _find_draft_characteristic(
    draft_item: dict[str, Any],
    find_char: dict[str, Any],
) -> dict[str, Any] | None:
    by_code, by_name = _draft_char_maps(draft_item)
    code = str(find_char.get("code") or find_char.get("characteristicCode") or "").strip()
    if code and code in by_code:
        return by_code[code]
    name_key = _norm_label(str(find_char.get("name") or find_char.get("characteristicName") or ""))
    if name_key and name_key in by_name:
        return by_name[name_key]
    return None


def find_template_char(ktru_item: dict[str, Any], find_char: dict[str, Any]) -> dict[str, Any] | None:
    """Match find characteristic to supplier KTRU/search template by code or name."""
    code = str(find_char.get("code") or find_char.get("characteristicCode") or "").strip()
    name_key = _norm_label(str(find_char.get("name") or find_char.get("characteristicName") or ""))
    by_code: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for ch in ktru_item.get("characteristics") or []:
        if not isinstance(ch, dict):
            continue
        c = str(ch.get("code") or "").strip()
        if c:
            by_code[c] = ch
        nm = _norm_label(str(ch.get("name") or ""))
        if nm:
            by_name[nm] = ch
    if code and code in by_code:
        return by_code[code]
    if name_key and name_key in by_name:
        return by_name[name_key]
    return None


def _find_value_semantics(cv: dict[str, Any]) -> dict[str, Any]:
    """Semantic fields from find value — without registry IDs."""
    return {
        "qualityDescription": cv.get("qualityDescription"),
        "concreteValue": cv.get("concreteValue"),
        "minValue": cv.get("minValue"),
        "maxValue": cv.get("maxValue"),
        "minMathNotation": cv.get("minMathNotation"),
        "maxMathNotation": cv.get("maxMathNotation"),
        "okeiCode": cv.get("okeiCode"),
        "okeiFullName": cv.get("okeiFullName"),
        "valueFormatId": cv.get("valueFormatId"),
        "valueCode": cv.get("valueCode"),
        "productCharacteristic": cv.get("productCharacteristic"),
    }


def _has_value_semantics(sem: dict[str, Any]) -> bool:
    return bool(
        sem.get("qualityDescription")
        or sem.get("concreteValue")
        or sem.get("minValue")
        or sem.get("maxValue")
    )


def build_find_rts_values(
    ktru_item: dict[str, Any],
    template_char: dict[str, Any],
    find_char: dict[str, Any],
    result: ApplyResult,
) -> list[dict[str, Any]]:
    """Map find characteristicValues onto supplier template IDs."""
    code = str(template_char.get("code") or "").strip()
    options = _search_char_options(ktru_item, code)
    out: list[dict[str, Any]] = []
    seen: set[object] = set()
    raw_vals = find_char.get("characteristicValues")
    if not isinstance(raw_vals, list):
        return out
    for cv in raw_vals:
        if not isinstance(cv, dict):
            continue
        sem = _find_value_semantics(cv)
        if not _has_value_semantics(sem):
            continue
        mapped = _characteristic_value_for_api(sem, options=options, characteristic=template_char)
        if not _has_value_semantics(mapped):
            continue
        token: object = mapped.get("id")
        if token is None:
            token = (
                mapped.get("qualityDescription"),
                mapped.get("minValue"),
                mapped.get("maxValue"),
                mapped.get("concreteValue"),
            )
        if token in seen:
            continue
        seen.add(token)
        out.append(mapped)
        if mapped.get("id") is not None:
            result.value_match_exact += 1
        else:
            result.value_unmatched += 1
    if not _allows_multiple_values(template_char) and len(out) > 1:
        return out[:1]
    return out


def build_find_ktru_characteristics(
    ktru_item: dict[str, Any],
    find_item: dict[str, Any],
) -> tuple[list[dict[str, Any]], ApplyResult]:
    """Build supplier characteristic rows from find, keyed by template code."""
    result = ApplyResult()
    by_template_code: dict[str, dict[str, Any]] = {}
    find_chars = find_item.get("ktruCharacteristics")
    if not isinstance(find_chars, list):
        find_chars = []

    for find_char in find_chars:
        if not isinstance(find_char, dict):
            continue
        template = find_template_char(ktru_item, find_char)
        label = str(find_char.get("name") or find_char.get("code") or "—").strip()
        code_raw = str(find_char.get("code") or find_char.get("characteristicCode") or "").strip()
        if template is None:
            result.unmatched_find.append({"label": label, "code": code_raw})
            continue
        code = str(template.get("code") or "").strip()
        values = build_find_rts_values(ktru_item, template, find_char, result)
        if not values:
            continue
        if code in by_template_code:
            existing = by_template_code[code].setdefault("characteristicValues", [])
            present = {row.get("id") for row in existing if isinstance(row, dict)}
            for row in values:
                if row.get("id") not in present:
                    existing.append(row)
                    present.add(row.get("id"))
            continue
        mapped = _map_create_characteristic(template)
        mapped["characteristicValues"] = values
        by_template_code[code] = mapped
        result.matched += 1

    included_codes = set(by_template_code)
    out = list(by_template_code.values())
    for template in ktru_item.get("characteristics") or []:
        if not isinstance(template, dict) or not template.get("isRequired"):
            continue
        code = str(template.get("code") or "").strip()
        if code and code not in included_codes:
            out.append(_map_create_characteristic(template))
            name = str(template.get("name") or code)
            if name not in result.unfilled_required:
                result.unfilled_required.append(name)

    return out, result


def build_find_create_payload(
    ktru_item: dict[str, Any],
    find_item: dict[str, Any],
    *,
    unit_from_line: str | None = None,
) -> tuple[dict[str, Any], ApplyResult]:
    """Create payload with find values overlaid on KTRU template characteristics."""
    find_mapped, apply_result = build_find_ktru_characteristics(ktru_item, find_item)
    by_code = {
        str(ch.get("code") or "").strip(): ch
        for ch in find_mapped
        if isinstance(ch, dict) and str(ch.get("code") or "").strip()
    }
    payload = build_create_payload(ktru_item, unit_from_line=unit_from_line)
    for ch in payload.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or "").strip()
        overlay = by_code.get(code)
        if overlay and overlay.get("characteristicValues"):
            ch["characteristicValues"] = overlay["characteristicValues"]
    return payload, apply_result


def apply_find_to_draft_item(
    item: dict[str, Any],
    find_item: dict[str, Any],
    ktru_item: dict[str, Any],
) -> ApplyResult:
    """Merge find ktruCharacteristics into supplier draft GET item by template code."""
    result = ApplyResult()
    find_chars = find_item.get("ktruCharacteristics")
    if not isinstance(find_chars, list):
        find_chars = []

    for find_char in find_chars:
        if not isinstance(find_char, dict):
            continue
        label = str(find_char.get("name") or find_char.get("code") or "—").strip()
        code_raw = str(find_char.get("code") or find_char.get("characteristicCode") or "").strip()
        template = find_template_char(ktru_item, find_char)
        if template is None:
            result.unmatched_find.append({"label": label, "code": code_raw})
            continue
        target = _find_draft_characteristic(item, find_char)
        if target is None:
            result.unmatched_find.append(
                {"label": label, "code": code_raw, "detail": "нет в GET-черновике"}
            )
            continue
        values = build_find_rts_values(ktru_item, template, find_char, result)
        if not values:
            continue
        target["characteristicValues"] = values
        result.matched += 1

    fill_remaining_required_from_template(item, ktru_item, result)
    ensure_item_ktru_fields(item, ktru_item)
    return result


def delivery_regions_count(find_item: dict[str, Any]) -> int | None:
    """Unique delivery regions (oktmo) in «Условия поставки товара»."""
    places = find_item.get("deliveryPlaces")
    if not isinstance(places, list):
        return None
    if not places:
        return 0
    codes: set[str] = set()
    for place in places:
        if not isinstance(place, dict):
            continue
        oktmo = place.get("oktmo")
        if isinstance(oktmo, dict):
            code = str(oktmo.get("code") or "").strip()
            if code:
                codes.add(code)
    return len(codes) if codes else len(places)


def delivery_places_count(find_item: dict[str, Any]) -> int | None:
    """Number of rows in RTS «Условия поставки товара» (deliveryPlaces)."""
    places = find_item.get("deliveryPlaces")
    if not isinstance(places, list):
        return None
    return len(places)


def delivery_params_from_find(find_item: dict[str, Any]) -> dict[str, Any]:
    """Extract delivery / price_ord inputs from find deliveryPlaces[0]."""
    out: dict[str, Any] = {
        "delivery_place": "",
        "region": "",
        "quantity_text": "",
        "competitor_unit_price": "",
        "min_delivery_days": None,
        "max_delivery_days": None,
        "oktmo": None,
    }
    places = find_item.get("deliveryPlaces")
    if not isinstance(places, list) or not places:
        return out
    place = places[0]
    if not isinstance(place, dict):
        return out
    oktmo = place.get("oktmo")
    if isinstance(oktmo, dict):
        out["oktmo"] = dict(oktmo)
        name = str(oktmo.get("name") or "").strip()
        if name:
            out["delivery_place"] = name
            out["region"] = name
    out["quantity_text"] = str(place.get("minQuantity") or place.get("maxQuantity") or "").strip()
    out["competitor_unit_price"] = str(place.get("unitPrice") or "").strip()
    out["min_delivery_days"] = place.get("minDeliveryDays")
    out["max_delivery_days"] = place.get("maxDeliveryDays")
    return out


def competitor_unit_price_amount(
    find_item: dict[str, Any],
    *,
    fallback_unit_price: float | None = None,
) -> float | None:
    """Unit price from find deliveryPlaces[0], else optional fallback (registry/finding)."""
    params = delivery_params_from_find(find_item)
    price = parse_money_amount(str(params.get("competitor_unit_price") or ""))
    if price is not None:
        return price
    if fallback_unit_price is not None:
        try:
            value = float(fallback_unit_price)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None
    return None


def competitor_draft_unit_price(
    find_item: dict[str, Any],
    *,
    fallback_unit_price: float | None = None,
    discount_factor: float = COMPETITOR_DRAFT_PRICE_FACTOR,
) -> str | None:
    """Draft unit price: competitor PP price × discount (default −5%)."""
    raw = competitor_unit_price_amount(find_item, fallback_unit_price=fallback_unit_price)
    if raw is None:
        return None
    factor = float(discount_factor)
    if factor <= 0:
        return None
    return format_rts_unit_price_from_float(raw * factor)


def participation_economics_for_finding(
    find_item: dict[str, Any],
    *,
    ktru_code: str,
    price_ord_enabled: bool = True,
) -> dict[str, Any]:
    if not price_ord_enabled:
        return participation_economics_disabled()
    params = delivery_params_from_find(find_item)
    return compute_participation_economics(
        ktru_code=str(ktru_code or "").strip(),
        delivery_place=str(params.get("delivery_place") or ""),
        region=str(params.get("region") or ""),
        price_per_unit_text=str(params.get("competitor_unit_price") or ""),
        quantity_text=str(params.get("quantity_text") or ""),
        catalog=get_price_ord_catalog(),
    )


def apply_find_delivery(
    item: dict[str, Any],
    find_item: dict[str, Any],
    *,
    price_per_unit: str | None = None,
) -> None:
    """Merge find delivery place fields; unitPrice from calculated draft price."""
    find_places = find_item.get("deliveryPlaces")
    find_place = find_places[0] if isinstance(find_places, list) and find_places else None
    places = item.get("deliveryPlaces")
    if not isinstance(places, list) or not places:
        item["deliveryPlaces"] = [{}]
        places = item["deliveryPlaces"]
    dp = places[0] if isinstance(places[0], dict) else {}
    places[0] = dp

    if isinstance(find_place, dict):
        oktmo = find_place.get("oktmo")
        if isinstance(oktmo, dict) and oktmo.get("code"):
            dp["oktmo"] = dict(oktmo)
        for key in ("minQuantity", "maxQuantity", "available", "blocked"):
            if find_place.get(key) is not None:
                dp[key] = find_place[key]
        if find_place.get("minDeliveryDays") is not None:
            dp["minDeliveryDays"] = find_place["minDeliveryDays"]
        if find_place.get("maxDeliveryDays") is not None:
            dp["maxDeliveryDays"] = find_place["maxDeliveryDays"]
        if find_place.get("vatRateCode"):
            dp["vatRateCode"] = find_place["vatRateCode"]

    dp.setdefault("vatRateCode", DEFAULT_VAT_RATE_CODE)
    unit_price = format_rts_unit_price(price_per_unit or "")
    if unit_price:
        dp["unitPrice"] = unit_price


def compare_find_to_draft(
    find_item: dict[str, Any],
    draft_item: dict[str, Any],
    *,
    ktru_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare find characteristics with final supplier draft item."""
    del ktru_item
    rows: list[dict[str, Any]] = []
    compared_draft_codes: set[str] = set()
    find_chars = find_item.get("ktruCharacteristics")
    if not isinstance(find_chars, list):
        find_chars = []

    for find_char in find_chars:
        if not isinstance(find_char, dict):
            continue
        label = str(find_char.get("name") or find_char.get("code") or "—").strip()
        code = str(find_char.get("code") or "").strip() or None
        find_values = render_characteristic_values(find_char.get("characteristicValues") or [])
        expected = {_value_signature(v) for v in (find_char.get("characteristicValues") or []) if isinstance(v, dict)}
        draft_char = _find_draft_characteristic(draft_item, find_char)

        if draft_char is None:
            rows.append(
                {
                    "find_label": label,
                    "find_code": code,
                    "find_values": find_values,
                    "rts_code": code,
                    "rts_name": None,
                    "draft_values": [],
                    "status": "unmatched_find",
                    "detail": "характеристика не найдена в черновике",
                }
            )
            continue

        draft_code = str(draft_char.get("code") or "").strip()
        if draft_code:
            compared_draft_codes.add(draft_code)
        draft_vals_raw = [v for v in (draft_char.get("characteristicValues") or []) if isinstance(v, dict)]
        draft_values = render_characteristic_values(draft_vals_raw)
        if not draft_vals_raw:
            rows.append(
                {
                    "find_label": label,
                    "find_code": code,
                    "find_values": find_values,
                    "rts_code": draft_code or code,
                    "rts_name": str(draft_char.get("name") or "") or None,
                    "draft_values": [],
                    "status": "missing_in_draft",
                    "detail": "пустые characteristicValues в черновике",
                }
            )
            continue

        actual = _draft_value_signatures(draft_char)
        status, detail = _compare_value_sets(
            notice_values=find_values,
            draft_values=draft_values,
            expected=expected,
            actual=actual,
        )
        rows.append(
            {
                "find_label": label,
                "find_code": code,
                "find_values": find_values,
                "rts_code": draft_code or code,
                "rts_name": str(draft_char.get("name") or "") or None,
                "draft_values": draft_values,
                "status": status,
                "detail": detail,
            }
        )

    for ch in draft_item.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or "").strip()
        if not code or code in compared_draft_codes:
            continue
        draft_vals_raw = [v for v in (ch.get("characteristicValues") or []) if isinstance(v, dict)]
        if not draft_vals_raw:
            continue
        rows.append(
            {
                "find_label": "",
                "find_code": None,
                "find_values": [],
                "rts_code": code,
                "rts_name": str(ch.get("name") or "") or None,
                "draft_values": render_characteristic_values(draft_vals_raw),
                "status": "auto_filled",
                "detail": "заполнено автоматически",
            }
        )

    ok_count = sum(1 for row in rows if row.get("status") == "ok")
    issues = len(rows) - ok_count
    return {"total": len(rows), "ok": ok_count, "issues": issues, "rows": rows}

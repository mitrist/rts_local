"""Build RTS PP draft payload from KTRU template + operator region prices."""

from __future__ import annotations

from typing import Any

from ktru_country_origin import CountryOfOrigin
from notice_rts_temporary import force_registry_number_not_provided
from notice_to_rts_pp import ensure_item_ktru_fields
from participation_price import parse_money_amount
from rts_national_mode import apply_national_mode_to_draft_item, country_dictionary_record, okpd2_code_from_ktru
from rts_oktmo_catalog import OktmoCatalogError, get_by_code
from rts_pp_api import (
    DEFAULT_DELIVERY,
    _fix_characteristic_value_ids,
    _search_char_options,
    _set_characteristic_value,
    _set_characteristic_values,
    format_rts_quantity,
    format_rts_unit_price,
)

CATALOG_PURCHASE_NUMBER = "catalog"
CATALOG_RF_COUNTRY = CountryOfOrigin("643", "Россия")
CATALOG_MIN_QUANTITY = format_rts_quantity("1")


class CatalogPpDraftError(ValueError):
    """Invalid catalog wizard input (unknown OKTMO, empty price, bad characteristic)."""


def is_locked_characteristic(ch: dict[str, Any]) -> bool:
    kind = ch.get("characteristicKind") if isinstance(ch.get("characteristicKind"), dict) else {}
    try:
        return int(kind.get("id") or 0) == 1
    except (TypeError, ValueError):
        return "Неизменяемая" in str(kind.get("name") or "")


def _option_label(cv: dict[str, Any]) -> str:
    for key in ("qualityDescription", "concreteValue", "productCharacteristic"):
        text = str(cv.get(key) or "").strip()
        if text:
            return text
    min_v = str(cv.get("minValue") or "").strip()
    max_v = str(cv.get("maxValue") or "").strip()
    if min_v or max_v:
        return f"{min_v}…{max_v}".strip("…")
    return str(cv.get("valueCode") or cv.get("id") or "").strip()


def _match_option(options: list[dict[str, Any]], value_code: str) -> dict[str, Any] | None:
    key = str(value_code or "").strip()
    if not key:
        return None
    for cv in options:
        if str(cv.get("valueCode") or "").strip() == key:
            return cv
        if str(cv.get("id") or "").strip() == key:
            return cv
    folded = " ".join(key.lower().replace("ё", "е").split())
    for cv in options:
        label = " ".join(_option_label(cv).lower().replace("ё", "е").split())
        if label and label == folded:
            return cv
    return None


def catalog_rf_country_dto() -> dict[str, Any]:
    rec = country_dictionary_record(CATALOG_RF_COUNTRY)
    rec["name"] = "Россия"
    return rec


def catalog_template_for_form(ktru_item: dict[str, Any]) -> dict[str, Any]:
    """KTRU template reduced to fields the /pp-create wizard needs."""
    okeis = ktru_item.get("okeis") or []
    unit = {"code": "796", "name": "Штука"}
    if okeis and isinstance(okeis[0], dict):
        unit = {
            "code": str(okeis[0].get("code") or "796"),
            "name": str(okeis[0].get("fullName") or okeis[0].get("name") or "Штука"),
        }
    chars: list[dict[str, Any]] = []
    for ch in ktru_item.get("characteristics") or []:
        if not isinstance(ch, dict) or ch.get("isActual") is False:
            continue
        options: list[dict[str, Any]] = []
        for cv in ch.get("characteristicValues") or []:
            if not isinstance(cv, dict):
                continue
            options.append(
                {
                    "id": cv.get("id"),
                    "value_code": str(cv.get("valueCode") or "").strip(),
                    "label": _option_label(cv),
                }
            )
        locked = is_locked_characteristic(ch)
        chars.append(
            {
                "code": str(ch.get("code") or "").strip(),
                "name": str(ch.get("name") or "").strip(),
                "is_required": bool(ch.get("isRequired")),
                "locked": locked,
                "kind_name": str((ch.get("characteristicKind") or {}).get("name") or ""),
                "options": options,
                "value_code": options[0]["value_code"] if locked and options else "",
                "label": options[0]["label"] if locked and options else "",
            }
        )
    return {
        "code": str(ktru_item.get("code") or "").strip(),
        "name": str(ktru_item.get("name") or "").strip(),
        "version": ktru_item.get("version"),
        "unit": unit,
        "characteristics": chars,
        "country": {"code": "643", "name": "Россия"},
        "registry_number_not_provided": True,
    }


def normalize_characteristic_selections(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CatalogPpDraftError("characteristics должен быть массивом.")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        value_code = str(row.get("value_code") or row.get("valueCode") or "").strip()
        if not code or not value_code:
            continue
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "value_code": value_code})
    return out


def _positive_rts_amount(raw: str) -> str:
    formatted = format_rts_unit_price(raw)
    amount = parse_money_amount(formatted.replace(",", ".")) if formatted else None
    if amount is None or amount <= 0:
        return ""
    return formatted


def normalize_region_prices(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise CatalogPpDraftError("Выберите хотя бы один регион с ценой и количеством больше 0.")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if not code or code in seen:
            continue
        price = _positive_rts_amount(str(row.get("unit_price") or row.get("unitPrice") or ""))
        qty_src = str(row.get("quantity") or row.get("qty") or row.get("minQuantity") or "")
        quantity = format_rts_quantity(qty_src)
        qty_amount = parse_money_amount(quantity.replace(",", ".")) if quantity else None
        if not price or qty_amount is None or qty_amount <= 0:
            continue
        try:
            subject = get_by_code(code)
        except OktmoCatalogError as exc:
            raise CatalogPpDraftError(str(exc)) from exc
        seen.add(code)
        out.append(
            {
                "code": subject.code,
                "name": subject.name,
                "unit_price": price,
                "quantity": quantity,
            }
        )
    if not out:
        raise CatalogPpDraftError("Выберите хотя бы один регион с ценой и количеством больше 0.")
    return out


def build_catalog_delivery_places(regions: list[dict[str, str]]) -> list[dict[str, Any]]:
    places: list[dict[str, Any]] = []
    for region in regions:
        qty = region["quantity"]
        place = {
            "kladr": DEFAULT_DELIVERY["kladr"],
            "oktmo": {"code": region["code"], "name": region["name"]},
            "unitPrice": region["unit_price"],
            "minQuantity": CATALOG_MIN_QUANTITY,
            "maxQuantity": qty,
            "vatRateCode": DEFAULT_DELIVERY["vatRateCode"],
            "minDeliveryDays": DEFAULT_DELIVERY["minDeliveryDays"],
            "maxDeliveryDays": DEFAULT_DELIVERY["maxDeliveryDays"],
            "blocked": "0,0",
            "available": qty,
        }
        places.append(place)
    return places


def apply_catalog_characteristics(
    item: dict[str, Any],
    ktru_item: dict[str, Any],
    selections: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Set locked values always; optional selects only when the operator picked a value."""
    chosen_lists: dict[str, list[str]] = {}
    for row in selections:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        value_code = str(row.get("value_code") or "").strip()
        if not code or not value_code:
            continue
        bucket = chosen_lists.setdefault(code, [])
        if value_code not in bucket:
            bucket.append(value_code)
    applied: list[dict[str, str]] = []
    chars = item.get("ktruCharacteristics")
    if not isinstance(chars, list):
        chars = []
        item["ktruCharacteristics"] = chars
    by_code = {
        str(ch.get("code") or "").strip(): ch
        for ch in chars
        if isinstance(ch, dict) and str(ch.get("code") or "").strip()
    }
    for tpl in ktru_item.get("characteristics") or []:
        if not isinstance(tpl, dict) or tpl.get("isActual") is False:
            continue
        code = str(tpl.get("code") or "").strip()
        if not code:
            continue
        ch = by_code.get(code)
        if ch is None:
            continue
        options = _search_char_options(ktru_item, code)
        if is_locked_characteristic(tpl) and options:
            _set_characteristic_value(ch, options[0], options=options)
            applied.append({"name": str(ch.get("name") or tpl.get("name") or code), "value": _option_label(options[0])})
            continue
        value_codes = chosen_lists.get(code) or []
        if not value_codes:
            ch["characteristicValues"] = []
            continue
        matched_opts: list[dict[str, Any]] = []
        labels: list[str] = []
        for value_code in value_codes:
            matched = _match_option(options, value_code)
            if matched is None:
                raise CatalogPpDraftError(
                    f"Значение «{value_code}» нет в шаблоне характеристики «{tpl.get('name') or code}»."
                )
            matched_opts.append(matched)
            labels.append(_option_label(matched))
        _set_characteristic_values(ch, matched_opts, options=options)
        applied.append(
            {
                "name": str(ch.get("name") or tpl.get("name") or code),
                "value": "; ".join(labels),
            }
        )
    unknown = [code for code in chosen_lists if code not in by_code]
    if unknown:
        raise CatalogPpDraftError(f"Неизвестная характеристика КТРУ: {unknown[0]}.")
    _fix_characteristic_value_ids(item, ktru_item)
    return applied


def apply_catalog_to_draft_item(
    item: dict[str, Any],
    ktru_item: dict[str, Any],
    *,
    selections: list[dict[str, str]],
    regions: list[dict[str, str]],
    info_types_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill get_draft item: chars, RF origin, Нет в реестре, deliveryPlaces."""
    ensure_item_ktru_fields(item, ktru_item)
    applied_chars = apply_catalog_characteristics(item, ktru_item, selections)
    apply_national_mode_to_draft_item(
        item,
        CATALOG_RF_COUNTRY,
        info_types_payload=info_types_payload,
    )
    item["countriesOfOrigin"] = [catalog_rf_country_dto()]
    force_registry_number_not_provided(item)
    item["deliveryPlaces"] = build_catalog_delivery_places(regions)
    _fix_characteristic_value_ids(item, ktru_item)
    return {
        "source": "catalog",
        "ktru_code": str(ktru_item.get("code") or "").strip(),
        "ktru_name": str(ktru_item.get("name") or "").strip(),
        "characteristics": applied_chars,
        "regions_count": len(regions),
        "country_code": "643",
        "country_name": "Россия",
        "registry_number_not_provided": True,
        "okpd2": okpd2_code_from_ktru(str(ktru_item.get("code") or ""), ktru_item),
    }

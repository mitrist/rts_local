"""Build RTS temporary/create payload from Trades creation-data only (no local KTRU cache)."""

from __future__ import annotations

from typing import Any

from ktru_country_origin import CountryOfOrigin
from rts_national_mode import (
    apply_national_mode_to_draft_item,
    okpd2_code_from_ktru,
)
from rts_pp_api import RtsPpApiError, format_rts_unit_price

_RF = CountryOfOrigin("643", "Российская Федерация")

_MINPROMTORG_CODES = ("N616", "N617", "N878", "N1236")

_TYPE_NAMES = {
    1: "Качественная",
    2: "Количественная",
}

# creation-data does not carry kind/choice; temporary/create dump uses these for notice positions.
_DEFAULT_KIND = {"id": 2, "name": "Изменемая заказчиком с выбором одного значения"}
_DEFAULT_CHOICE = {"id": 2, "name": "Множественный выбор"}


def creation_data_ktru_code(creation_data: dict[str, Any]) -> str:
    return str(creation_data.get("ktruCode") or "").strip()


def assert_creation_data_matches_ktru(
    creation_data: dict[str, Any],
    expected_ktru: str,
) -> None:
    got = creation_data_ktru_code(creation_data)
    want = str(expected_ktru or "").strip()
    if not got or not want or got != want:
        raise RtsPpApiError(
            f"КТРУ creation-data «{got or '—'}» не совпадает с позицией «{want or '—'}» "
            f"(starNotificationNumber, вероятно, не та позиция).",
            reason="star_mismatch",
            detail={"creation_ktru": got, "expected_ktru": want},
        )


def _type_block(type_id: Any) -> dict[str, Any] | None:
    if type_id is None:
        return None
    try:
        tid = int(type_id)
    except (TypeError, ValueError):
        return {"id": type_id, "name": None}
    return {"id": tid, "name": _TYPE_NAMES.get(tid)}


def _value_from_creation(cv: dict[str, Any], *, characteristic_id: Any) -> dict[str, Any]:
    vid = cv.get("dictionaryId")
    row: dict[str, Any] = {
        "dictionaryId": vid,
        "qualityDescription": cv.get("qualityDescription"),
        "concreteValue": cv.get("concreteValue"),
        "minValue": cv.get("minValue"),
        "maxValue": cv.get("maxValue"),
        "minMathNotation": cv.get("minMathNotation"),
        "maxMathNotation": cv.get("maxMathNotation"),
        "okeiCode": cv.get("okeiCode"),
        "valueFormatId": cv.get("valueFormatId"),
        "characteristicId": characteristic_id,
    }
    if vid is not None:
        try:
            row["id"] = int(vid)
        except (TypeError, ValueError):
            pass
    return row


def map_ktru_characteristics_from_creation_data(
    creation_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Map notice snapshot characteristics → temporary/create shape (RTS-only)."""
    raw = creation_data.get("ktruCharacteristics")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for ch in raw:
        if not isinstance(ch, dict):
            continue
        cid = ch.get("dictionaryId")
        values_raw = ch.get("ktruCharacteristicValues")
        values: list[dict[str, Any]] = []
        if isinstance(values_raw, list):
            for cv in values_raw:
                if isinstance(cv, dict):
                    values.append(_value_from_creation(cv, characteristic_id=cid))
        required = ch.get("isRequired")
        out.append(
            {
                "id": cid,
                "code": ch.get("code"),
                "name": ch.get("name"),
                "isActual": True if ch.get("isActual") is None else bool(ch.get("isActual")),
                "isRequired": bool(required) if required is not None else False,
                "characteristicType": _type_block(ch.get("typeId")),
                "characteristicKind": dict(_DEFAULT_KIND),
                "characteristicChoiceType": dict(_DEFAULT_CHOICE),
                "characteristicValues": values,
            }
        )
    return out


def _delivery_place_from_creation_data(
    creation_data: dict[str, Any],
    *,
    unit_price_override: str | None = None,
) -> dict[str, Any]:
    oktmo_code = str(creation_data.get("deliveryPlaceOktmoCode") or "").strip()
    oktmo_name = str(creation_data.get("deliveryPlaceName") or "").strip()
    unit_price = unit_price_override
    if not unit_price:
        unit_price = format_rts_unit_price(str(creation_data.get("unitPrice") or "").strip()) or str(
            creation_data.get("unitPrice") or ""
        )
    else:
        unit_price = format_rts_unit_price(unit_price) or unit_price

    min_q = creation_data.get("minQuantity")
    max_q = creation_data.get("maxQuantity")
    min_days = creation_data.get("minDeliveryDays")
    max_days = creation_data.get("maxDeliveryDays")
    vat = creation_data.get("vatRate")
    vat_code = None
    if isinstance(vat, dict):
        vat_code = vat.get("code")
    elif vat is not None and str(vat).strip():
        vat_code = str(vat).strip()

    return {
        "kladr": {"code": None, "name": None},
        "oktmo": {"code": oktmo_code or None, "name": oktmo_name or None},
        "unitPrice": unit_price or "",
        "vatRateCode": vat_code,
        "minQuantity": "" if min_q is None else str(min_q),
        "maxQuantity": "" if max_q is None else str(max_q),
        "minDeliveryDays": "" if min_days is None else str(min_days),
        "maxDeliveryDays": "" if max_days is None else str(max_days),
        "blocked": "",
        "available": "",
        "uncompletedApplicationsCount": "",
    }


def build_temporary_create_payload(
    creation_data: dict[str, Any],
    *,
    unit_price_override: str | None = None,
    info_types_payload: dict[str, Any] | None = None,
    country: CountryOfOrigin | None = None,
) -> dict[str, Any]:
    """Assemble POST temporary/create body from RTS creation-data only.

    Country of origin comes from ``country_origin.csv`` (caller resolves via
    ``resolve_country_for_draft``). RF → national mode + «Нет в реестре»;
    other countries → countriesOfOrigin only (no RF registry flag).
    """
    if not isinstance(creation_data, dict):
        raise RtsPpApiError("Нужен creation_data.", reason="bad_request")

    code = creation_data_ktru_code(creation_data)
    if not code:
        raise RtsPpApiError("creation-data без ktruCode.", reason="creation_data_empty")

    origin = country if isinstance(country, CountryOfOrigin) else _RF
    okpd2_code = okpd2_for_creation(creation_data)
    payload: dict[str, Any] = {
        "ktru": {
            "code": code,
            "name": creation_data.get("ktruName"),
            "okpd2": {"code": okpd2_code} if okpd2_code else None,
            "version": creation_data.get("ktruVersion"),
        },
        "unit": {
            "code": str(creation_data.get("okeiCode") or "796"),
            "name": str(creation_data.get("okeiName") or "Штука"),
        },
        "tradeMark": str(creation_data.get("tradeMark") or ""),
        "certificateNameMedicalProduct": str(creation_data.get("certificateNameMedicalProduct") or ""),
        "documentExtractUrlMedicalProduct": str(
            creation_data.get("documentExtractUrlMedicalProduct") or ""
        ),
        "certificateNumberMedicalProduct": "",
        "countriesOfOrigin": [],
        "softwareCountriesOfOrigin": [],
        "ktruCharacteristics": map_ktru_characteristics_from_creation_data(creation_data),
        "minpromtorgInfos": [
            {
                "npaCode": c,
                "registryNumber": "",
                "productsFirstLevelComplianceType": None,
                "summaryScores": "",
                "isSelected": False,
            }
            for c in _MINPROMTORG_CODES
        ],
        "minpromtorgInfoDocuments": [],
        "nationalModeInfoGroups": [],
        "softwareNationalModeInfoGroups": [],
        "deliveryPlaces": [
            _delivery_place_from_creation_data(
                creation_data,
                unit_price_override=unit_price_override,
            )
        ],
        "countriesOfOriginDocuments": [],
        "otherDocuments": [],
        "m2DDocuments": [],
    }

    # For non-RF skip info-types so we do not attach RF industrial groups.
    types_for_nm = info_types_payload if origin.code == _RF.code else None
    apply_national_mode_to_draft_item(
        payload,
        origin,
        find_item=None,
        info_types_payload=types_for_nm,
    )
    if origin.code == _RF.code:
        # temporary/create (browser dump): even for isRestricted OKPD2 set «Нет в реестре».
        force_registry_number_not_provided(payload)
    else:
        payload["nationalModeInfoGroups"] = []
        payload["softwareNationalModeInfoGroups"] = []

    countries = payload.get("countriesOfOrigin")
    if isinstance(countries, list) and countries and isinstance(countries[0], dict):
        payload["countriesOfOrigin"] = [
            {
                "code": str(countries[0].get("code") or origin.code),
                "name": str(countries[0].get("name") or origin.name),
            }
        ]
    else:
        payload["countriesOfOrigin"] = [{"code": origin.code, "name": origin.name}]
    return payload


def force_registry_number_not_provided(payload: dict[str, Any]) -> None:
    """Set registryNumberNotProvided=true on all national-mode info rows (notice create)."""
    for key in ("nationalModeInfoGroups", "softwareNationalModeInfoGroups"):
        groups = payload.get(key)
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            infos = group.get("nationalModeInfos")
            if not isinstance(infos, list) or not infos:
                group["nationalModeInfos"] = [
                    {
                        "registryNumber": None,
                        "registryNumberNotProvided": True,
                        "summaryScore": None,
                        "summaryScoreNotProvided": None,
                        "radioElectronicProductsLevelTypeId": None,
                        "documentFiles": [],
                    }
                ]
                continue
            for info in infos:
                if not isinstance(info, dict):
                    continue
                info["registryNumberNotProvided"] = True
                if not str(info.get("registryNumber") or "").strip():
                    info["registryNumber"] = None
                info.setdefault("documentFiles", [])


def okpd2_for_creation(creation_data: dict[str, Any]) -> str:
    """OKPD2 for national-mode info-types: from KTRU code prefix (no dictionary lookup)."""
    return okpd2_code_from_ktru(creation_data_ktru_code(creation_data), None)

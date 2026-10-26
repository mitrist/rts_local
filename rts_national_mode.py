"""National mode (ПП 1875 / ст.14 44-ФЗ) for RTS PP drafts."""

from __future__ import annotations

from typing import Any

from ktru_country_origin import CountryOfOrigin, national_mode_groups_for_country

_RF_CODE = "643"

_INFO_KEYS = (
    "registryNumber",
    "registryNumberNotProvided",
    "summaryScore",
    "summaryScoreNotProvided",
    "radioElectronicProductsLevelTypeId",
)

_GROUP_FLAG_KEYS = ("isIndustrial", "isProhibited", "isRestricted")


def okpd2_code_from_ktru(ktru_code: str, ktru_item: dict[str, Any] | None = None) -> str:
    if isinstance(ktru_item, dict):
        okpd = ktru_item.get("okpd2")
        if isinstance(okpd, dict):
            code = str(okpd.get("code") or "").strip()
            if code:
                return code
        elif isinstance(okpd, str) and okpd.strip():
            return okpd.strip()
    code = str(ktru_code or "").strip()
    if "-" in code:
        return code.rsplit("-", 1)[0]
    return code


def country_from_find(find_item: dict[str, Any] | None) -> CountryOfOrigin | None:
    """First countriesOfOrigin from competitor find, if present."""
    if not isinstance(find_item, dict):
        return None
    raw = find_item.get("countriesOfOrigin")
    if not isinstance(raw, list) or not raw:
        return None
    first = raw[0]
    if not isinstance(first, dict):
        return None
    code = str(first.get("code") or "").strip()
    name = str(first.get("name") or "").strip()
    if not code:
        return None
    return CountryOfOrigin(code, name or code)


def country_dictionary_record(
    country: CountryOfOrigin,
    *,
    template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OKSM/country DTO as in find / LK select (not just code+name)."""
    if isinstance(template, dict) and str(template.get("code") or "").strip() == country.code:
        out = {
            "code": country.code,
            "name": str(template.get("name") or country.name),
            "description": template.get("description"),
            "version": template.get("version"),
            "actual": template.get("actual"),
            "isTemplate": template.get("isTemplate"),
            "category": template.get("category") if template.get("category") is not None else "Undefined",
        }
        return out
    return {
        "code": country.code,
        "name": country.name,
        "description": None,
        "version": None,
        "actual": None,
        "isTemplate": None,
        "category": "Undefined",
    }


def _info_types_flags(info_types_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(info_types_payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _GROUP_FLAG_KEYS:
        if key in info_types_payload and info_types_payload[key] is not None:
            out[key] = bool(info_types_payload[key])
    return out


def _default_mode_infos(*, restricted: bool) -> list[dict[str, Any]]:
    """Restricted lists (ПП 1875) reject registryNumberNotProvided=true."""
    if restricted:
        return [
            {
                "registryNumber": None,
                "registryNumberNotProvided": False,
                "summaryScore": None,
                "summaryScoreNotProvided": True,
                "radioElectronicProductsLevelTypeId": None,
                "documentFiles": [],
            }
        ]
    return [
        {
            "registryNumber": None,
            "registryNumberNotProvided": True,
            "summaryScore": None,
            "summaryScoreNotProvided": None,
            "radioElectronicProductsLevelTypeId": None,
            "documentFiles": [],
        }
    ]


def _sanitize_mode_infos(
    raw: Any,
    *,
    restricted: bool = False,
    preserve_explicit: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        return _default_mode_infos(restricted=restricted)
    out: list[dict[str, Any]] = []
    for info in raw:
        if not isinstance(info, dict):
            continue
        cleaned: dict[str, Any] = {}
        for key in _INFO_KEYS:
            if key in info:
                cleaned[key] = info[key]
        if "documentFiles" in info and isinstance(info.get("documentFiles"), list):
            cleaned["documentFiles"] = info["documentFiles"]
        else:
            cleaned.setdefault("documentFiles", [])
        # When inventing from info-types for restricted OKPD2, do not claim
        # «Нет в реестре». When copying find — keep competitor checkbox as-is.
        if restricted and not preserve_explicit:
            cleaned["registryNumberNotProvided"] = False
            if not str(cleaned.get("registryNumber") or "").strip():
                cleaned["registryNumber"] = None
            cleaned.setdefault("summaryScoreNotProvided", True)
        if cleaned:
            out.append(cleaned)
    return out or _default_mode_infos(restricted=restricted)


def sanitize_national_mode_groups(
    raw: Any,
    *,
    flags: dict[str, Any] | None = None,
    preserve_explicit: bool = False,
) -> list[dict[str, Any]]:
    """Drop RTS registry ids; keep type/country/info payload for draft/update."""
    if not isinstance(raw, list):
        return []
    flags = flags or {}
    restricted = bool(flags.get("isRestricted"))
    out: list[dict[str, Any]] = []
    for group in raw:
        if not isinstance(group, dict):
            continue
        type_id = str(group.get("nationalModeInfoTypeId") or group.get("nationalModeInfoType") or "").strip()
        country = str(group.get("countryCode") or "").strip()
        if not type_id or not country:
            continue
        row: dict[str, Any] = {
            "countryCode": country,
            "nationalModeInfoTypeId": type_id,
            "nationalModeInfos": _sanitize_mode_infos(
                group.get("nationalModeInfos"),
                restricted=restricted or bool(group.get("isRestricted")),
                preserve_explicit=preserve_explicit,
            ),
        }
        for key in _GROUP_FLAG_KEYS:
            if key in group and group[key] is not None:
                row[key] = group[key]
            elif key in flags:
                row[key] = flags[key]
        out.append(row)
    return out


def national_mode_from_find(
    find_item: dict[str, Any] | None,
    *,
    flags: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Copy national-mode blocks from competitor find when present."""
    if not isinstance(find_item, dict):
        return None
    # Preserve «Нет в реестре» / registryNumber from competitor even if OKPD is restricted.
    national = sanitize_national_mode_groups(
        find_item.get("nationalModeInfoGroups"),
        flags=flags,
        preserve_explicit=True,
    )
    software = sanitize_national_mode_groups(
        find_item.get("softwareNationalModeInfoGroups"),
        flags=flags,
        preserve_explicit=True,
    )
    if not national and not software:
        return None
    return national, software


def _rule_matches_country(rule: dict[str, Any], country_code: str) -> bool:
    code = str(country_code or "").strip()
    if not code:
        return False
    include = [str(x).strip() for x in (rule.get("includeCountryCodes") or []) if str(x).strip()]
    exclude = [str(x).strip() for x in (rule.get("excludeCountryCodes") or []) if str(x).strip()]
    if include:
        return code in include
    if exclude:
        return code not in exclude
    return False


def _group_for_type(
    country_code: str,
    type_id: str,
    *,
    flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    flags = flags or {}
    restricted = bool(flags.get("isRestricted"))
    row: dict[str, Any] = {
        "countryCode": country_code,
        "nationalModeInfoTypeId": type_id,
        "nationalModeInfos": _default_mode_infos(restricted=restricted),
    }
    for key in _GROUP_FLAG_KEYS:
        if key in flags:
            row[key] = flags[key]
    return row


def national_mode_from_info_types(
    info_types_payload: dict[str, Any] | None,
    country_code: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build groups from GET national-mode/info-types for a country of origin.

    Only the primary ``nationalModeInfoType`` is applied automatically.
    ``softwareNationalModeInfoType`` is left empty here — fill it by copying find
    (competitors usually omit the software slot unless required).
    """
    code = str(country_code or "").strip()
    if not code or not isinstance(info_types_payload, dict):
        return [], []
    rules = info_types_payload.get("nationalModeInfoTypes")
    if not isinstance(rules, list):
        return [], []
    flags = _info_types_flags(info_types_payload)
    national: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict) or not _rule_matches_country(rule, code):
            continue
        main_type = str(rule.get("nationalModeInfoType") or "").strip()
        if main_type:
            national.append(_group_for_type(code, main_type, flags=flags))
        break
    return national, []


def resolve_national_mode_groups(
    country: CountryOfOrigin,
    *,
    find_item: dict[str, Any] | None = None,
    info_types_payload: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Prefer find copy, then info-types, then legacy RF industrial+software stub.

    Returns (national, software, source) where source is find|info_types|legacy|empty.
    """
    flags = _info_types_flags(info_types_payload)
    from_find = national_mode_from_find(find_item, flags=flags)
    if from_find is not None:
        find_countries = {
            str(g.get("countryCode") or "").strip()
            for g in (from_find[0] + from_find[1])
            if isinstance(g, dict)
        }
        if not find_countries or country.code in find_countries or not country.code:
            return from_find[0], from_find[1], "find"

    if info_types_payload is not None:
        national, software = national_mode_from_info_types(info_types_payload, country.code)
        return national, software, "info_types"

    if country.code == _RF_CODE:
        national, software = national_mode_groups_for_country(country)
        return national, software, "legacy"
    return [], [], "empty"


def apply_national_mode_to_draft_item(
    item: dict[str, Any],
    country: CountryOfOrigin,
    *,
    find_item: dict[str, Any] | None = None,
    info_types_payload: dict[str, Any] | None = None,
) -> str:
    """Set countriesOfOrigin + national mode groups. Returns source tag."""
    country_template = None
    if isinstance(find_item, dict):
        raw_countries = find_item.get("countriesOfOrigin")
        if isinstance(raw_countries, list) and raw_countries and isinstance(raw_countries[0], dict):
            country_template = raw_countries[0]
    item["countriesOfOrigin"] = [
        country_dictionary_record(country, template=country_template)
    ]

    find_sw_countries: list[dict[str, Any]] = []
    if isinstance(find_item, dict):
        raw = find_item.get("softwareCountriesOfOrigin")
        if isinstance(raw, list):
            for x in raw:
                if not isinstance(x, dict) or not str(x.get("code") or "").strip():
                    continue
                find_sw_countries.append(
                    country_dictionary_record(
                        CountryOfOrigin(str(x["code"]).strip(), str(x.get("name") or "").strip()),
                        template=x,
                    )
                )
    item["softwareCountriesOfOrigin"] = find_sw_countries

    national, software, source = resolve_national_mode_groups(
        country,
        find_item=find_item,
        info_types_payload=info_types_payload,
    )
    item["nationalModeInfoGroups"] = national
    item["softwareNationalModeInfoGroups"] = software
    return source

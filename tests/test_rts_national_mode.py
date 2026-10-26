"""Tests for ПП 1875 national-mode fill (info-types + find copy)."""

from __future__ import annotations

from ktru_country_origin import CountryOfOrigin, apply_country_to_draft_item
from rts_national_mode import (
    country_dictionary_record,
    national_mode_from_find,
    national_mode_from_info_types,
    okpd2_code_from_ktru,
    resolve_national_mode_groups,
    sanitize_national_mode_groups,
)


def _industrial_info_types(*, restricted: bool = False) -> dict:
    return {
        "canFillSoftwareCountries": True,
        "isIndustrial": True,
        "isProhibited": False,
        "isRestricted": restricted,
        "nationalModeInfoTypes": [
            {
                "includeCountryCodes": ["643"],
                "excludeCountryCodes": None,
                "nationalModeInfoType": "IndustrialProductsRussianFederation",
                "softwareNationalModeInfoType": "SoftwareRussianFederation",
            },
            {
                "includeCountryCodes": None,
                "excludeCountryCodes": ["051", "112", "398", "417", "643"],
                "nationalModeInfoType": "IndustrialProductsForeignCountries",
                "softwareNationalModeInfoType": None,
            },
        ],
    }


def _software_info_types() -> dict:
    return {
        "canFillSoftwareCountries": False,
        "isIndustrial": False,
        "isProhibited": False,
        "isRestricted": False,
        "nationalModeInfoTypes": [
            {
                "includeCountryCodes": ["643"],
                "excludeCountryCodes": None,
                "nationalModeInfoType": "SoftwareRussianFederation",
                "softwareNationalModeInfoType": None,
            },
        ],
    }


def test_country_from_find():
    from rts_national_mode import country_from_find

    assert country_from_find(None) is None
    assert country_from_find({"countriesOfOrigin": []}) is None
    c = country_from_find(
        {
            "countriesOfOrigin": [
                {"code": "156", "name": "Китайская Народная Республика", "category": "Undefined"}
            ]
        }
    )
    assert c == CountryOfOrigin("156", "Китайская Народная Республика")


def test_okpd2_from_ktru_code_and_item():
    assert okpd2_code_from_ktru("26.20.13.000-00000001") == "26.20.13.000"
    assert okpd2_code_from_ktru(
        "x",
        {"okpd2": {"code": "58.29.11.000"}},
    ) == "58.29.11.000"


def test_country_dictionary_record_shape():
    rec = country_dictionary_record(CountryOfOrigin("643", "Российская Федерация"))
    assert rec["code"] == "643"
    assert rec["category"] == "Undefined"
    assert "description" in rec


def test_sanitize_strips_ids():
    raw = [
        {
            "id": 99,
            "countryCode": "643",
            "nationalModeInfoTypeId": "IndustrialProductsRussianFederation",
            "nationalModeInfos": [
                {"id": 1, "registryNumber": "123", "registryNumberNotProvided": False}
            ],
        }
    ]
    out = sanitize_national_mode_groups(raw)
    assert out[0]["nationalModeInfoTypeId"] == "IndustrialProductsRussianFederation"
    assert out[0]["nationalModeInfos"][0]["registryNumber"] == "123"
    assert out[0]["nationalModeInfos"][0]["registryNumberNotProvided"] is False
    assert "id" not in out[0]


def test_national_mode_from_find_copy():
    find = {
        "nationalModeInfoGroups": [
            {
                "id": 5,
                "countryCode": "643",
                "nationalModeInfoTypeId": "IndustrialProductsRussianFederation",
                "nationalModeInfos": [{"registryNumberNotProvided": True}],
            }
        ],
        "softwareNationalModeInfoGroups": [],
    }
    national, software = national_mode_from_find(find)  # type: ignore[misc]
    assert national[0]["nationalModeInfoTypeId"] == "IndustrialProductsRussianFederation"
    assert software == []
    assert "id" not in national[0]


def test_info_types_industrial_rf_primary_only():
    national, software = national_mode_from_info_types(_industrial_info_types(), "643")
    assert national[0]["nationalModeInfoTypeId"] == "IndustrialProductsRussianFederation"
    assert national[0]["isIndustrial"] is True
    assert national[0]["isRestricted"] is False
    assert national[0]["nationalModeInfos"][0]["registryNumberNotProvided"] is True
    assert software == []


def test_info_types_restricted_no_registry_not_provided():
    national, software = national_mode_from_info_types(
        _industrial_info_types(restricted=True), "643"
    )
    assert national[0]["isRestricted"] is True
    info = national[0]["nationalModeInfos"][0]
    assert info["registryNumberNotProvided"] is False
    assert software == []


def test_info_types_software_only_no_industrial():
    national, software = national_mode_from_info_types(_software_info_types(), "643")
    assert national[0]["nationalModeInfoTypeId"] == "SoftwareRussianFederation"
    assert software == []


def test_info_types_china_uses_foreign():
    national, software = national_mode_from_info_types(_industrial_info_types(), "156")
    assert national[0]["nationalModeInfoTypeId"] == "IndustrialProductsForeignCountries"
    assert software == []


def test_resolve_prefers_find_over_info_types():
    find = {
        "nationalModeInfoGroups": [
            {
                "countryCode": "643",
                "nationalModeInfoTypeId": "IndustrialProductsRussianFederation",
                "nationalModeInfos": [{"registryNumber": "REG-1", "registryNumberNotProvided": False}],
            }
        ],
        "softwareNationalModeInfoGroups": [],
    }
    national, software, source = resolve_national_mode_groups(
        CountryOfOrigin("643", "Российская Федерация"),
        find_item=find,
        info_types_payload=_industrial_info_types(restricted=True),
    )
    assert source == "find"
    assert software == []
    assert national[0]["nationalModeInfos"][0]["registryNumber"] == "REG-1"
    assert national[0]["isRestricted"] is True


def test_find_preserves_registry_not_provided_when_restricted():
    find = {
        "nationalModeInfoGroups": [
            {
                "countryCode": "643",
                "nationalModeInfoTypeId": "IndustrialProductsRussianFederation",
                "nationalModeInfos": [{"registryNumber": None, "registryNumberNotProvided": True}],
            }
        ],
        "softwareNationalModeInfoGroups": [],
    }
    national, software, source = resolve_national_mode_groups(
        CountryOfOrigin("643", "Российская Федерация"),
        find_item=find,
        info_types_payload=_industrial_info_types(restricted=True),
    )
    assert source == "find"
    assert national[0]["nationalModeInfos"][0]["registryNumberNotProvided"] is True
    assert software == []


def test_resolve_info_types_when_find_empty():
    national, software, source = resolve_national_mode_groups(
        CountryOfOrigin("643", "Российская Федерация"),
        find_item={"nationalModeInfoGroups": [], "softwareNationalModeInfoGroups": []},
        info_types_payload=_software_info_types(),
    )
    assert source == "info_types"
    assert national[0]["nationalModeInfoTypeId"] == "SoftwareRussianFederation"
    assert software == []


def test_apply_country_full_dto_and_software_okpd():
    item: dict = {
        "countriesOfOrigin": [],
        "nationalModeInfoGroups": [],
        "softwareNationalModeInfoGroups": [],
        "softwareCountriesOfOrigin": [],
    }
    source = apply_country_to_draft_item(
        item,
        CountryOfOrigin("643", "Российская Федерация"),
        info_types_payload=_software_info_types(),
    )
    assert source == "info_types"
    assert item["countriesOfOrigin"][0]["category"] == "Undefined"
    assert item["nationalModeInfoGroups"][0]["nationalModeInfoTypeId"] == "SoftwareRussianFederation"
    assert item["softwareNationalModeInfoGroups"] == []

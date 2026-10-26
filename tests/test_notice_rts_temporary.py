"""Tests for RTS temporary/create payload from creation-data."""

from __future__ import annotations

import pytest

from notice_rts_temporary import (
    assert_creation_data_matches_ktru,
    build_temporary_create_payload,
    creation_data_ktru_code,
    map_ktru_characteristics_from_creation_data,
    okpd2_for_creation,
)
from rts_pp_api import RtsPpApiError, notice_offer_edit_url, star_notification_number


def test_star_notification_number():
    assert star_notification_number("0369200012126000008") == "l0369200012126000008-1"
    assert notice_offer_edit_url("0369200012126000008").endswith(
        "notificationNumber=0369200012126000008&starNotificationNumber=l0369200012126000008-1"
    )


def test_assert_creation_data_matches_ktru():
    data = {"ktruCode": "22.22.10.000-00000005"}
    assert_creation_data_matches_ktru(data, "22.22.10.000-00000005")
    with pytest.raises(RtsPpApiError) as exc:
        assert_creation_data_matches_ktru(data, "99.99.99.999-00000001")
    assert exc.value.reason == "star_mismatch"


def test_map_characteristics_from_creation_data_only():
    creation = {
        "ktruCode": "22.22.10.000-00000005",
        "ktruCharacteristics": [
            {
                "dictionaryId": 674812,
                "code": "f76e2302-6",
                "name": "Вид материала",
                "typeId": 1,
                "isActual": True,
                "isRequired": True,
                "ktruCharacteristicValues": [
                    {"dictionaryId": 4263109, "qualityDescription": "Полиэтилен"}
                ],
            },
            {
                "dictionaryId": 674817,
                "code": "5e0c2e6e-b",
                "name": "Количество в рулоне",
                "typeId": 2,
                "ktruCharacteristicValues": [
                    {
                        "dictionaryId": 4263146,
                        "minValue": "10,0000",
                        "minMathNotation": "ge",
                        "okeiCode": "796",
                        "valueFormatId": 1,
                    }
                ],
            },
        ],
    }
    chars = map_ktru_characteristics_from_creation_data(creation)
    assert len(chars) == 2
    assert chars[0]["id"] == 674812
    assert chars[0]["characteristicType"]["id"] == 1
    assert chars[0]["characteristicValues"][0]["id"] == 4263109
    assert chars[0]["characteristicValues"][0]["qualityDescription"] == "Полиэтилен"
    assert chars[1]["characteristicValues"][0]["minValue"] == "10,0000"
    assert chars[1]["characteristicValues"][0]["okeiCode"] == "796"


def test_build_temporary_create_payload_rf_and_registry():
    creation = {
        "ktruCode": "22.22.10.000-00000005",
        "ktruName": "Мешок",
        "ktruVersion": 6,
        "okeiCode": "736",
        "okeiName": "РУЛ",
        "minQuantity": "1",
        "maxQuantity": "10,00000000000",
        "unitPrice": "200,4000",
        "maxDeliveryDays": 14,
        "deliveryPlaceName": "г Челябинск",
        "deliveryPlaceOktmoCode": "75701000001",
        "ktruCharacteristics": [
            {
                "dictionaryId": 674812,
                "code": "f76e2302-6",
                "name": "Вид материала",
                "typeId": 1,
                "ktruCharacteristicValues": [
                    {"dictionaryId": 4263109, "qualityDescription": "Полиэтилен"}
                ],
            }
        ],
    }
    info_types = {
        "isIndustrial": True,
        "isRestricted": True,
        "isProhibited": False,
        "nationalModeInfoTypes": [
            {
                "includeCountryCodes": ["643"],
                "nationalModeInfoType": "IndustrialProductsRussianFederation",
            }
        ],
    }
    payload = build_temporary_create_payload(
        creation,
        unit_price_override="190,00",
        info_types_payload=info_types,
    )
    assert creation_data_ktru_code(creation) == "22.22.10.000-00000005"
    assert okpd2_for_creation(creation) == "22.22.10.000"
    assert payload["ktru"]["code"] == "22.22.10.000-00000005"
    assert payload["ktru"]["okpd2"]["code"] == "22.22.10.000"
    assert payload["unit"]["code"] == "736"
    assert payload["ktruCharacteristics"][0]["characteristicValues"][0]["id"] == 4263109
    assert payload["countriesOfOrigin"][0]["code"] == "643"
    assert payload["deliveryPlaces"][0]["oktmo"]["code"] == "75701000001"
    assert payload["deliveryPlaces"][0]["unitPrice"] == "190,00"
    groups = payload["nationalModeInfoGroups"]
    assert groups
    assert groups[0].get("isRestricted") is True
    infos = groups[0]["nationalModeInfos"]
    assert infos
    # Browser temporary/create dump: restricted + «Нет в реестре».
    assert infos[0].get("registryNumberNotProvided") is True


def test_build_temporary_create_payload_china_no_registry():
    from ktru_country_origin import CountryOfOrigin

    creation = {
        "ktruCode": "26.20.15.000-00000028",
        "ktruName": "Товар",
        "okeiCode": "796",
        "okeiName": "Штука",
        "ktruCharacteristics": [],
    }
    china = CountryOfOrigin("156", "Китайская Народная Республика")
    payload = build_temporary_create_payload(
        creation,
        country=china,
        info_types_payload={
            "isIndustrial": True,
            "isRestricted": True,
            "nationalModeInfoTypes": [
                {
                    "includeCountryCodes": ["643"],
                    "nationalModeInfoType": "IndustrialProductsRussianFederation",
                }
            ],
        },
    )
    assert payload["countriesOfOrigin"] == [
        {"code": "156", "name": "Китайская Народная Республика"}
    ]
    assert payload["nationalModeInfoGroups"] == []
    assert payload["softwareNationalModeInfoGroups"] == []

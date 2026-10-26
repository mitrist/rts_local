from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from catalog_pp_draft import (
    CatalogPpDraftError,
    apply_catalog_to_draft_item,
    catalog_template_for_form,
    normalize_region_prices,
)
from eis_analytics_db import analytics_db_path, get_rts_pp_draft
from rts_pp_api import build_create_payload
from rts_pp_draft_service import create_pp_draft_from_catalog

ROOT = Path(__file__).resolve().parents[1]
CHAIR_PATH = ROOT / "deploy" / "310111150_03.txt"
CHAIR_CODE = "31.01.11.150-00000003"
LOCKED_SEAT = "8e50a5cd-4"
LOCKED_BACK = "cf3a76b9-4"
SELECT_BACK_MATERIAL = "94b59978-4"
SELECT_BACK_METAL = "c98c9acd-2"


def _chair_item() -> dict:
    return json.loads(CHAIR_PATH.read_text(encoding="utf-8"))


def test_chair_template_locked_and_selects():
    tpl = catalog_template_for_form(_chair_item())
    assert tpl["code"] == CHAIR_CODE
    assert tpl["unit"]["code"] == "796"
    by_code = {ch["code"]: ch for ch in tpl["characteristics"]}
    assert by_code[LOCKED_SEAT]["locked"] is True
    assert by_code[LOCKED_SEAT]["label"] == "Да"
    assert by_code[LOCKED_BACK]["locked"] is True
    assert by_code[SELECT_BACK_MATERIAL]["locked"] is False
    assert any(opt["value_code"] == SELECT_BACK_METAL for opt in by_code[SELECT_BACK_MATERIAL]["options"])


def test_apply_catalog_chair_two_regions():
    ktru_item = _chair_item()
    item = build_create_payload(ktru_item)
    regions = normalize_region_prices(
        [
            {"code": "01000000", "unit_price": "4500", "quantity": "10"},
            {"code": "03000000", "unit_price": "5100,50", "quantity": "3"},
        ]
    )
    report = apply_catalog_to_draft_item(
        item,
        ktru_item,
        selections=[{"code": SELECT_BACK_MATERIAL, "value_code": SELECT_BACK_METAL}],
        regions=regions,
    )
    by_code = {ch["code"]: ch for ch in item["ktruCharacteristics"]}
    seat_vals = by_code[LOCKED_SEAT]["characteristicValues"]
    back_vals = by_code[LOCKED_BACK]["characteristicValues"]
    material_vals = by_code[SELECT_BACK_MATERIAL]["characteristicValues"]
    assert seat_vals[0]["qualityDescription"] == "Да"
    assert back_vals[0]["qualityDescription"] == "Да"
    assert material_vals[0]["valueCode"] == SELECT_BACK_METAL
    assert item["countriesOfOrigin"][0]["code"] == "643"
    assert item["countriesOfOrigin"][0]["name"] == "Россия"
    infos = item["nationalModeInfoGroups"][0]["nationalModeInfos"]
    assert infos[0]["registryNumberNotProvided"] is True
    places = item["deliveryPlaces"]
    assert len(places) == 2
    assert places[0]["oktmo"]["code"] == "01000000"
    assert places[0]["oktmo"]["name"] == "Муниципальные образования Алтайского края"
    assert places[0]["unitPrice"] == "4500,0"
    assert places[1]["oktmo"]["code"] == "03000000"
    assert places[1]["unitPrice"] == "5100,50"
    assert places[0]["minQuantity"] == "1,0"
    assert places[0]["maxQuantity"] == "10,0"
    assert places[0]["available"] == "10,0"
    assert places[1]["minQuantity"] == "1,0"
    assert places[1]["maxQuantity"] == "3,0"
    assert places[0]["minDeliveryDays"] == 20
    assert places[0]["maxDeliveryDays"] == 40
    assert places[0]["vatRateCode"] == "R22"
    assert report["regions_count"] == 2
    assert any(row["name"] == "Вид материала спинки" and row["value"] == "Металл" for row in report["characteristics"])
    for code in (LOCKED_SEAT, LOCKED_BACK, SELECT_BACK_MATERIAL):
        val = by_code[code]["characteristicValues"][0]
        assert isinstance(val["characteristicId"], int)
    assert seat_vals[0]["characteristicId"] == 881281
    assert back_vals[0]["characteristicId"] == 881282


def test_apply_catalog_fills_characteristic_id_when_draft_parent_id_null():
    ktru_item = _chair_item()
    item = build_create_payload(ktru_item)
    for ch in item["ktruCharacteristics"]:
        ch["id"] = None
        for cv in ch.get("characteristicValues") or []:
            cv["characteristicId"] = None
    apply_catalog_to_draft_item(
        item,
        ktru_item,
        selections=[{"code": SELECT_BACK_MATERIAL, "value_code": SELECT_BACK_METAL}],
        regions=normalize_region_prices([{"code": "01000000", "unit_price": "4500", "quantity": "5"}]),
    )
    by_code = {ch["code"]: ch for ch in item["ktruCharacteristics"]}
    assert by_code[LOCKED_SEAT]["characteristicValues"][0]["characteristicId"] == 881281
    assert by_code[LOCKED_BACK]["characteristicValues"][0]["characteristicId"] == 881282
    assert by_code[SELECT_BACK_MATERIAL]["characteristicValues"][0]["characteristicId"] == 881277


def test_unknown_oktmo_rejected():
    with pytest.raises(CatalogPpDraftError):
        normalize_region_prices([{"code": "99999999", "unit_price": "100", "quantity": "5"}])


def test_region_without_quantity_rejected():
    with pytest.raises(CatalogPpDraftError):
        normalize_region_prices([{"code": "01000000", "unit_price": "4500"}])


def test_create_pp_draft_from_catalog_mocked(tmp_path):
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    cookie = meta / "cookies.txt"
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    ktru_item = _chair_item()
    store: dict = {}

    def fake_create(_cookie, payload, **_kw):
        store["create_chars"] = json.loads(json.dumps(payload.get("ktruCharacteristics") or []))
        store["item"] = {
            "id": 701001,
            "ktru": payload.get("ktru"),
            "ktruCharacteristics": payload.get("ktruCharacteristics"),
            "deliveryPlaces": payload.get("deliveryPlaces") or [],
            "countriesOfOrigin": [],
            "nationalModeInfoGroups": [],
            "softwareNationalModeInfoGroups": [],
        }
        return 701001

    def fake_get(_cookie, _draft_id, **_kw):
        return json.loads(json.dumps(store["item"]))

    def fake_update(_cookie, item, **_kw):
        store["item"] = json.loads(json.dumps(item))

    with patch("rts_pp_draft_service.validate_cookie_session"), patch(
        "rts_pp_draft_service.load_offers_cookie", return_value="cookie"
    ), patch(
        "rts_pp_draft_service.resolve_ktru_for_create", return_value=ktru_item
    ), patch(
        "rts_pp_draft_service.fetch_national_mode_info_types", return_value={}
    ), patch(
        "rts_pp_draft_service.create_draft", side_effect=fake_create
    ), patch(
        "rts_pp_draft_service.get_draft", side_effect=fake_get
    ), patch(
        "rts_pp_draft_service.update_draft", side_effect=fake_update
    ), patch(
        "rts_pp_draft_service.draft_search_by_id", return_value={"protectedId": "prot-1"}
    ):
        result = create_pp_draft_from_catalog(
            analytics_db=db,
            meta_dir=meta,
            cookie_path=cookie,
            ktru_code=CHAIR_CODE,
            characteristics=[{"code": SELECT_BACK_MATERIAL, "value_code": SELECT_BACK_METAL}],
            regions=[
                {"code": "01000000", "unit_price": "4500", "quantity": "10"},
                {"code": "03000000", "unit_price": "5100,50", "quantity": "3"},
            ],
            rts_profile="IPSokolova",
            created_by="IPSokolova",
        )
    assert result.ok is True
    assert result.rts_draft_id == 701001
    assert result.purchase_number == "catalog"
    assert result.line_key
    saved = get_rts_pp_draft(db, "catalog", result.line_key, rts_profile="IPSokolova")
    assert saved is not None
    assert saved["status"] == "created"
    fill = json.loads(saved["fill_report_json"])
    assert fill["ktru_name"] == "Стул на металлическом каркасе"
    assert fill["regions_count"] == 2
    item = store["item"]
    assert item["countriesOfOrigin"][0]["code"] == "643"
    assert item["deliveryPlaces"][0]["oktmo"]["code"] == "01000000"
    by_code = {ch["code"]: ch for ch in item["ktruCharacteristics"]}
    assert by_code[LOCKED_SEAT]["characteristicValues"][0]["qualityDescription"] == "Да"
    create_by_code = {ch["code"]: ch for ch in store["create_chars"]}
    assert create_by_code[SELECT_BACK_MATERIAL]["characteristicValues"][0]["valueCode"] == SELECT_BACK_METAL
    assert create_by_code[LOCKED_SEAT]["characteristicValues"][0]["qualityDescription"] == "Да"

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from catalog_pp_draft import CATALOG_PURCHASE_NUMBER, apply_catalog_to_draft_item, normalize_region_prices
from eis_analytics_db import (
    analytics_db_path,
    delete_rts_pp_draft,
    list_pp_core_queue,
    list_pp_core_variants,
    save_rts_pp_draft,
    update_pp_core_queue,
)
from pp_core_catalog import (
    CHAIR_KTRU_CODE,
    MAP_GAP,
    MAP_READY,
    MAP_VALIDATE,
    map_combo_to_rts,
    persist_core_snapshot,
    variant_can_enqueue,
)
from pp_core_queue import CoreQueueError, enqueue_core_variants, process_pp_core_queue
from rts_pp_api import build_create_payload
from rts_pp_draft_service import DraftResult

ROOT = Path(__file__).resolve().parents[1]
CHAIR_PATH = ROOT / "deploy" / "310111150_03.txt"
LOCKED_SEAT = "8e50a5cd-4"
LOCKED_BACK = "cf3a76b9-4"
SELECT_BACK_MATERIAL = "94b59978-4"
SELECT_BACK_METAL = "c98c9acd-2"


def _chair_item() -> dict:
    return json.loads(CHAIR_PATH.read_text(encoding="utf-8"))


def _ready_combo() -> dict:
    return {
        "hash": "h-ready",
        "n": 10,
        "preview": "Вид материала спинки=Металл",
        "chars": [
            {
                "display_label": "Вид материала спинки",
                "characteristic_code": SELECT_BACK_MATERIAL,
                "value_display": "Металл",
            }
        ],
    }


def _validate_combo() -> dict:
    return {
        "hash": "h-val",
        "n": 8,
        "preview": "Вид материала спинки=Металл хромированный",
        "chars": [
            {
                "display_label": "Вид материала спинки",
                "characteristic_code": SELECT_BACK_MATERIAL,
                "value_display": "Металл хромированный",
            }
        ],
    }


def test_map_chair_combo_ready_includes_locked():
    mapped = map_combo_to_rts(_ready_combo()["chars"], _chair_item())
    assert mapped["map_status"] == MAP_READY
    by_code = {row["code"]: row["value_code"] for row in mapped["characteristics"]}
    assert by_code[SELECT_BACK_MATERIAL] == SELECT_BACK_METAL
    assert LOCKED_SEAT in by_code
    assert LOCKED_BACK in by_code


def test_map_chair_combo_gap_unknown_field():
    mapped = map_combo_to_rts(
        [{"display_label": "Несуществующее поле", "value_display": "foo"}],
        _chair_item(),
    )
    assert mapped["map_status"] == MAP_GAP
    assert "нет поля" in mapped["map_reason"]


def test_map_chair_combo_eis_type_kind_is_ready():
    mapped = map_combo_to_rts(
        [
            {
                "display_label": "Вид материала спинки",
                "characteristic_code": SELECT_BACK_MATERIAL,
                "value_display": "Дерево [тип 1] вид 2",
            }
        ],
        _chair_item(),
    )
    assert mapped["map_status"] == MAP_READY
    by_code = {row["code"]: row["value_code"] for row in mapped["characteristics"]}
    assert by_code[SELECT_BACK_MATERIAL] == "a3d81b08-3"


def test_map_shelf_xml_range_to_rts_bucket():
    template = {
        "characteristics": [
            {
                "code": "de71e0c7-b",
                "name": "Ширина стеллажа",
                "isActual": True,
                "isRequired": False,
                "characteristicValues": [
                    {
                        "valueCode": "5827ad38-6",
                        "minValue": "1200,0000",
                        "maxValue": "1300,0000",
                        "minMathNotation": "ge",
                        "maxMathNotation": "lt",
                    }
                ],
            }
        ]
    }
    mapped = map_combo_to_rts(
        [
            {
                "display_label": "Ширина стеллажа",
                "characteristic_code": "de71e0c7-b",
                "value_display": "ОКЕИ Миллиметр greaterOrEqual мин 1200 less макс 1300 [тип 2] вид 3",
            }
        ],
        template,
    )
    assert mapped["map_status"] == MAP_READY
    by_code = {row["code"]: row["value_code"] for row in mapped["characteristics"]}
    assert by_code["de71e0c7-b"] == "5827ad38-6"


def test_map_chair_combo_substring_needs_validation():
    mapped = map_combo_to_rts(
        [
            {
                "display_label": "Вид материала спинки",
                "characteristic_code": SELECT_BACK_MATERIAL,
                "value_display": "Металл хромированный",
            }
        ],
        _chair_item(),
    )
    assert mapped["map_status"] == MAP_VALIDATE
    by_code = {row["code"]: row["value_code"] for row in mapped["characteristics"]}
    assert by_code[SELECT_BACK_MATERIAL] == SELECT_BACK_METAL
    assert variant_can_enqueue(
        {"map_status": mapped["map_status"], "characteristics": mapped["characteristics"]}
    )


def test_apply_catalog_from_mapped_snapshot():
    ktru_item = _chair_item()
    mapped = map_combo_to_rts(_ready_combo()["chars"], ktru_item)
    item = build_create_payload(ktru_item)
    regions = normalize_region_prices(
        [
            {"code": "01000000", "unit_price": "4500", "quantity": "10"},
            {"code": "03000000", "unit_price": "5100", "quantity": "3"},
        ]
    )
    apply_catalog_to_draft_item(
        item,
        ktru_item,
        selections=mapped["characteristics"],
        regions=regions,
    )
    by_code = {ch["code"]: ch for ch in item["ktruCharacteristics"]}
    assert by_code[SELECT_BACK_MATERIAL]["characteristicValues"][0]["valueCode"] == SELECT_BACK_METAL
    assert len(item["deliveryPlaces"]) == 2


def test_snapshot_stable_variant_no(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    tmpl = _chair_item()
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], tmpl, total_notices=10)
    extra = {
        "hash": "h-new",
        "n": 4,
        "preview": "x",
        "chars": _ready_combo()["chars"],
    }
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo(), extra], tmpl, total_notices=14)
    rows = list_pp_core_variants(db, CHAIR_KTRU_CODE)
    by_hash = {row["eis_hash"]: row["variant_no"] for row in rows}
    assert by_hash["h-ready"] == 1
    assert by_hash["h-new"] == 2


def test_enqueue_skips_gap_and_duplicate_active(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    tmpl = _chair_item()
    gap = {
        "hash": "h-gap",
        "n": 3,
        "preview": "gap",
        "chars": [{"display_label": "Нет такого", "value_display": "x"}],
    }
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo(), gap], tmpl, total_notices=13)
    regions = [{"code": "01000000", "unit_price": "4500", "quantity": "2"}]
    first = enqueue_core_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1, 2, 1],
        regions=regions,
        rts_profile="IPSokolova",
    )
    assert first.enqueued == 1
    assert any(row["variant_no"] == 2 for row in first.skipped)
    try:
        enqueue_core_variants(
            db,
            ktru_code=CHAIR_KTRU_CODE,
            variant_nos=[1],
            regions=regions,
            rts_profile="IPSokolova",
        )
        raise AssertionError("expected CoreQueueError")
    except CoreQueueError as exc:
        assert "Ни один вариант" in str(exc)


def test_enqueue_allows_needs_validation(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_validate_combo()], _chair_item(), total_notices=8)
    rows = list_pp_core_variants(db, CHAIR_KTRU_CODE)
    assert rows[0]["map_status"] == MAP_VALIDATE
    result = enqueue_core_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=[{"code": "01000000", "unit_price": "4500", "quantity": "2"}],
        rts_profile="IPSokolova",
    )
    assert result.enqueued == 1


def test_enqueue_skips_duplicate_rts_selection(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    dup = {
        "hash": "h-dup",
        "n": 4,
        "preview": "same",
        "chars": _ready_combo()["chars"],
    }
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo(), dup], _chair_item(), total_notices=14)
    result = enqueue_core_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1, 2],
        regions=[{"code": "01000000", "unit_price": "4500", "quantity": "2"}],
        rts_profile="IPSokolova",
    )
    assert result.enqueued == 1
    skip = {row["variant_no"]: row["reason"] for row in result.skipped}
    assert 2 in skip
    assert "набор характеристик РТС совпадает с вариантом 1" in skip[2]


def test_enqueue_rejects_empty_regions(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    try:
        enqueue_core_variants(
            db,
            ktru_code=CHAIR_KTRU_CODE,
            variant_nos=[1],
            regions=[],
            rts_profile="IPSokolova",
        )
        raise AssertionError("expected CoreQueueError")
    except CoreQueueError:
        pass


def test_drain_creates_and_publishes(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    enqueue_core_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=[{"code": "01000000", "unit_price": "4500", "quantity": "2"}],
        rts_profile="IPSokolova",
    )
    created = MagicMock(
        return_value=DraftResult(
            ok=True,
            rts_draft_id=88001,
            ktru_code=CHAIR_KTRU_CODE,
            fill_report={"source": "core"},
        )
    )
    pub = MagicMock(
        return_value=type("P", (), {"ok": True, "status": "awaiting_signature", "error": "", "reason": ""})()
    )
    monkeypatch.setattr("pp_core_queue.create_pp_draft_from_catalog", created)
    monkeypatch.setattr("pp_core_queue.start_publish_job", pub)
    monkeypatch.setattr("pp_core_queue.publish_enabled", lambda: True)
    monkeypatch.setattr("pp_core_queue.cookie_path_for_profile", lambda *_a, **_k: tmp_path / "c.txt")
    summary = process_pp_core_queue(tmp_path, db, project_root=tmp_path, limit=1)
    assert summary.processed == 1
    assert summary.drafted == 1
    assert summary.publish_prepared == 1
    created.assert_called_once()
    kwargs = created.call_args.kwargs
    assert kwargs["source"] == "core"
    assert kwargs["ktru_code"] == CHAIR_KTRU_CODE
    assert any(row["code"] == SELECT_BACK_MATERIAL for row in kwargs["characteristics"])
    pub.assert_called_once()
    assert pub.call_args.kwargs["source"] == "core"
    assert "core:" in pub.call_args.kwargs["source_key"]


def test_enqueue_again_after_draft_deleted(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    regions = [{"code": "01000000", "unit_price": "4500", "quantity": "2"}]
    first = enqueue_core_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=regions,
        rts_profile="IPSokolova",
    )
    assert first.enqueued == 1
    job = list_pp_core_queue(db, ktru_code=CHAIR_KTRU_CODE)[0]
    update_pp_core_queue(db, int(job["id"]), status="drafted", rts_draft_id=88001)
    save_rts_pp_draft(
        db,
        purchase_number=CATALOG_PURCHASE_NUMBER,
        line_key="core-line-1",
        ktru_code=CHAIR_KTRU_CODE,
        rts_draft_id=88001,
        status="created",
        rts_profile="IPSokolova",
    )
    try:
        enqueue_core_variants(
            db,
            ktru_code=CHAIR_KTRU_CODE,
            variant_nos=[1],
            regions=regions,
            rts_profile="IPSokolova",
        )
        raise AssertionError("expected CoreQueueError while draft exists")
    except CoreQueueError as exc:
        assert "Ни один вариант" in str(exc)
        assert "совпадает" in str(exc)
    assert delete_rts_pp_draft(
        db, CATALOG_PURCHASE_NUMBER, "core-line-1", rts_profile="IPSokolova"
    )
    released = list_pp_core_queue(db, ktru_code=CHAIR_KTRU_CODE)[0]
    assert released["status"] == "released"
    again = enqueue_core_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=regions,
        rts_profile="IPSokolova",
    )
    assert again.enqueued == 1


def test_enqueue_again_when_queue_row_left_without_local_draft(tmp_path: Path):
    """Prod leftover: queue still drafted/done after Черновики ПП were already cleared."""
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    regions = [{"code": "01000000", "unit_price": "4500", "quantity": "2"}]
    first = enqueue_core_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=regions,
        rts_profile="IPSokolova",
    )
    assert first.enqueued == 1
    job = list_pp_core_queue(db, ktru_code=CHAIR_KTRU_CODE)[0]
    update_pp_core_queue(db, int(job["id"]), status="done", rts_draft_id=88002)
    again = enqueue_core_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=regions,
        rts_profile="IPSokolova",
    )
    assert again.enqueued == 1

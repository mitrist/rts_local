from __future__ import annotations

import json
from pathlib import Path

from catalog_pp_draft import CATALOG_PURCHASE_NUMBER
from eis_analytics_db import (
    analytics_db_path,
    list_pp_core_queue,
    list_pp_virtual_queue,
    list_rts_pp_drafts,
    save_rts_pp_draft,
    update_pp_core_queue,
)
from pp_core_catalog import persist_core_snapshot
from pp_core_queue import CoreQueueError, enqueue_core_variants
from pp_virtual_queue import (
    VirtualQueueError,
    draft_virtual_from_notice,
    enqueue_virtual_variants,
    list_drafted_virtual_variants_for_match,
    process_pp_virtual_queue,
)
from virtual_pp_set import sync_virtual_set_from_core
from virtual_pp_store import (
    list_virtual_pp_variants,
    replace_rts_ezt_lot_items,
    upsert_virtual_match_event,
)

ROOT = Path(__file__).resolve().parents[1]
CHAIR_PATH = ROOT / "deploy" / "310111150_03.txt"
CHAIR_KTRU_CODE = "31.01.11.150-00000003"
SELECT_BACK_MATERIAL = "94b59978-4"


def _chair_item() -> dict:
    import json

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


def _sync(db):
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    return sync_virtual_set_from_core(db, CHAIR_KTRU_CODE)


def test_enqueue_virtual_and_drain_does_not_publish(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    _sync(db)
    result = enqueue_virtual_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=[{"code": "01000000", "unit_price": "4500", "quantity": "2"}],
        rts_profile="IPSokolova",
    )
    assert result.enqueued == 1

    def _boom(*_a, **_k):
        raise AssertionError("virtual drain must not use RTS session")

    monkeypatch.setattr("rts_pp_api.validate_cookie_session", _boom)
    monkeypatch.setattr("rts_pp_draft_service.validate_cookie_session", _boom)
    monkeypatch.setattr("rts_pp_draft_service.create_pp_draft_from_catalog", _boom)
    summary = process_pp_virtual_queue(tmp_path, db, project_root=tmp_path, limit=1)
    assert summary.processed == 1
    assert summary.drafted == 1
    job = list_pp_virtual_queue(db, ktru_code=CHAIR_KTRU_CODE)[0]
    assert job["status"] == "drafted"
    assert int(job["rts_draft_id"]) < 0
    drafts = list_rts_pp_drafts(db, rts_profile="IPSokolova")
    assert len(drafts) == 1
    import json

    fill = json.loads(str(drafts[0].get("fill_report_json") or "{}"))
    assert fill["source"] == "virtual_core"
    assert fill["variant_no"] == 1
    assert fill["local"] is True
    live = list_drafted_virtual_variants_for_match(db, CHAIR_KTRU_CODE)
    assert len(live) == 1
    assert int(live[0]["variant_no"]) == 1


def test_virtual_occupancy_blocks_core(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    _sync(db)
    regions = [{"code": "01000000", "unit_price": "4500", "quantity": "2"}]
    enqueue_virtual_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=regions,
        rts_profile="IPSokolova",
    )
    job = list_pp_virtual_queue(db, ktru_code=CHAIR_KTRU_CODE)[0]
    from eis_analytics_db import update_pp_virtual_queue

    update_pp_virtual_queue(db, int(job["id"]), status="drafted", rts_draft_id=99012)
    save_rts_pp_draft(
        db,
        purchase_number=CATALOG_PURCHASE_NUMBER,
        line_key="virtual-occ",
        ktru_code=CHAIR_KTRU_CODE,
        rts_draft_id=99012,
        status="created",
        rts_profile="IPSokolova",
        fill_report_json='{"source":"virtual_core","variant_no":1}',
    )
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
        assert "совпадает" in str(exc)


def test_core_queue_leftover_does_not_block_virtual_enqueue(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    _sync(db)
    regions = [{"code": "01000000", "unit_price": "4500", "quantity": "2"}]
    core = enqueue_core_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=regions,
        rts_profile="IPSokolova",
    )
    assert core.enqueued == 1
    job = list_pp_core_queue(db, ktru_code=CHAIR_KTRU_CODE)[0]
    update_pp_core_queue(db, int(job["id"]), status="drafted", rts_draft_id=88001)
    save_rts_pp_draft(
        db,
        purchase_number=CATALOG_PURCHASE_NUMBER,
        line_key="core-leftover",
        ktru_code=CHAIR_KTRU_CODE,
        rts_draft_id=88001,
        status="created",
        rts_profile="IPSokolova",
        fill_report_json='{"source":"core","variant_no":1}',
    )
    virtual = enqueue_virtual_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=regions,
        rts_profile="IPSokolova",
    )
    assert virtual.enqueued == 1


def test_enqueue_virtual_requires_set(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    try:
        enqueue_virtual_variants(
            db,
            ktru_code=CHAIR_KTRU_CODE,
            variant_nos=[1],
            regions=[{"code": "01000000", "unit_price": "4500", "quantity": "2"}],
            rts_profile="IPSokolova",
        )
        raise AssertionError("expected VirtualQueueError")
    except VirtualQueueError as exc:
        assert "набор" in str(exc).lower()


def _unmatched_notice(
    db,
    set_id: str,
    chars: list[dict],
    pn: str = "0315800000126000099",
    *,
    subject_oktmo: str = "01000000",
    region_raw: str = "",
    unit_price: float = 1300,
) -> str:
    replace_rts_ezt_lot_items(
        db,
        pn,
        [
            {
                "line_index": 0,
                "ktru_code": CHAIR_KTRU_CODE,
                "unit_price": unit_price,
                "quantity": 2,
                "characteristics": chars,
            }
        ],
    )
    upsert_virtual_match_event(
        db,
        {
            "notification_number": pn,
            "line_index": 0,
            "ktru_code": CHAIR_KTRU_CODE,
            "variant_no": None,
            "in_core": None,
            "char_status": "miss",
            "subject_oktmo": subject_oktmo,
            "region_raw": region_raw,
            "unit_price": unit_price,
            "quantity": 2,
            "set_id": set_id,
        },
    )
    return pn


def test_draft_virtual_from_notice_creates_belt_and_keeps_on_sync(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    summary = _sync(db)
    pn = _unmatched_notice(
        db,
        summary["set_id"],
        [{"name": "Вид материала спинки", "value": "Дерево"}],
    )
    monkeypatch.setattr("pp_virtual_queue.resolve_ktru_template", lambda *a, **k: _chair_item())
    result = draft_virtual_from_notice(
        db,
        tmp_path,
        ktru_code=CHAIR_KTRU_CODE,
        notification_number=pn,
        line_index=0,
        rts_profile="IPSokolova",
    )
    assert result["ok"] is True
    assert result["already"] is False
    assert int(result["variant_no"]) == 2
    assert int(result["in_core"]) == 0
    assert int(result["rts_draft_id"]) < 0
    drafts = list_rts_pp_drafts(db, rts_profile="IPSokolova")
    assert len(drafts) == 1
    fill = json.loads(str(drafts[0].get("fill_report_json") or "{}"))
    assert fill["source"] == "virtual_core"
    assert fill["variant_no"] == 2
    live = list_drafted_virtual_variants_for_match(db, CHAIR_KTRU_CODE)
    assert [int(row["variant_no"]) for row in live] == [2]
    again = draft_virtual_from_notice(
        db,
        tmp_path,
        ktru_code=CHAIR_KTRU_CODE,
        notification_number=pn,
        line_index=0,
        rts_profile="IPSokolova",
    )
    assert again["already"] is True
    assert int(again["variant_no"]) == 2
    sync_virtual_set_from_core(db, CHAIR_KTRU_CODE, template=_chair_item())
    nos = {int(row["variant_no"]) for row in list_virtual_pp_variants(db, CHAIR_KTRU_CODE)}
    assert 1 in nos
    assert 2 in nos


def test_draft_from_notice_adds_region_to_existing_variant(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    summary = _sync(db)
    enqueue_virtual_variants(
        db,
        ktru_code=CHAIR_KTRU_CODE,
        variant_nos=[1],
        regions=[{"code": "01000000", "unit_price": "4500", "quantity": "2"}],
        rts_profile="IPSokolova",
    )
    process_pp_virtual_queue(tmp_path, db, project_root=tmp_path, limit=1)
    pn = _unmatched_notice(
        db,
        summary["set_id"],
        [{"name": "Вид материала спинки", "value": "Металл"}],
        pn="0315800000126000088",
        subject_oktmo="03000000",
        region_raw="Краснодарский край",
        unit_price=6300,
    )
    monkeypatch.setattr("pp_virtual_queue.resolve_ktru_template", lambda *a, **k: _chair_item())
    result = draft_virtual_from_notice(
        db,
        tmp_path,
        ktru_code=CHAIR_KTRU_CODE,
        notification_number=pn,
        line_index=0,
        rts_profile="IPSokolova",
    )
    assert result["ok"] is True
    assert result["already"] is True
    assert result["region_added"] is True
    assert int(result["variant_no"]) == 1
    live = list_drafted_virtual_variants_for_match(db, CHAIR_KTRU_CODE)
    assert len(live) == 1
    codes = {str(row.get("code") or "") for row in live[0]["regions"]}
    assert "01000000" in codes
    assert "03000000" in codes
    from virtual_pp_store import list_virtual_match_events

    rows = list_virtual_match_events(db, CHAIR_KTRU_CODE, days=30)
    hit = next(r for r in rows if r["notification_number"] == pn)
    assert hit["matched"] is True
    assert int(hit["variant_no"]) == 1

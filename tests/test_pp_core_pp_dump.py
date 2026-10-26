from __future__ import annotations

import json
from pathlib import Path

from eis_analytics_db import analytics_db_path, list_pp_core_variants, list_pp_monitor_findings_by_ktru
from pp_core_catalog import STUDENT_CHAIR_KTRU_CODE
from pp_core_pp_dump import (
    frequent_regions_from_pp_items,
    import_pp_dump_core,
    iter_pp_dump_items,
)
from pp_core_registry import frequent_subject_regions, price_ladders_for_variants

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "rts_stul_pp_mini.json"
TMPL_PATH = ROOT / "deploy" / "310111150_21.txt"
GROWTH = "1ee0bf77-3"
GROWTH_2 = "b05215bd-5"
GROWTH_5 = "0f45839c-e"
PLYWOOD = "7432f784-1"
PLASTIC = "393996d7-d"


def _template() -> dict:
    raw = json.loads(TMPL_PATH.read_text(encoding="utf-8"))
    item = raw.get("item") if isinstance(raw.get("item"), dict) else raw
    assert item["code"] == STUDENT_CHAIR_KTRU_CODE
    return item


def _has(row: dict, code: str, value_code: str) -> bool:
    return any(ch.get("code") == code and ch.get("value_code") == value_code for ch in row["characteristics"])


def test_iter_pp_dump_items_concatenated_pages():
    rows = iter_pp_dump_items(FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 11
    ids = {str(row.get("offerPriceListItemProtectedId")) for row in rows}
    assert "stul-p1" in ids
    assert "other-stul" in ids


def test_frequent_regions_from_pp_items_min_one():
    items = iter_pp_dump_items(FIXTURE.read_text(encoding="utf-8"))
    chair = [row for row in items if str((row.get("ktru") or {}).get("code")) == STUDENT_CHAIR_KTRU_CODE]
    regions = frequent_regions_from_pp_items(chair, min_regions=1, limit=10)
    codes = {item["code"] for item in regions}
    assert "03000000" in codes
    assert "45000000" in codes


def test_import_pp_dump_core_maps_rts_and_ladder(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    tmpl = _template()
    result = import_pp_dump_core(
        db,
        FIXTURE.read_text(encoding="utf-8"),
        ktru_code=STUDENT_CHAIR_KTRU_CODE,
        template=tmpl,
    )
    assert result["ok"] is True
    assert result["stats"]["used"] == 10
    assert result["stats"]["skipped_ktru"] == 1
    assert result["stats"]["core_n"] == 2
    rows = list_pp_core_variants(db, STUDENT_CHAIR_KTRU_CODE)
    assert len(rows) == 2
    assert all(row["map_status"] == "ready" for row in rows)
    assert all(row["variant_no"] in (1, 2) for row in rows)
    plywood = next(row for row in rows if _has(row, GROWTH, GROWTH_2) and _has(row, "1f5fa9d7-5", PLYWOOD))
    plastic = next(row for row in rows if _has(row, GROWTH, GROWTH_5) and _has(row, "1f5fa9d7-5", PLASTIC))
    assert plywood["n"] == 5
    assert plastic["n"] == 3
    assert plywood["core_meta"]["source"] == "pp"
    ladders = price_ladders_for_variants(db, STUDENT_CHAIR_KTRU_CODE, rows, tmpl)
    plywood_ladder = ladders[plywood["variant_no"]]
    assert plywood_ladder["pp_n"] == 5
    assert plywood_ladder["top5"]["open_slots"] == 0
    assert plywood_ladder["top5"]["need_le"] == 3004.0
    findings = list_pp_monitor_findings_by_ktru(db, STUDENT_CHAIR_KTRU_CODE)
    assert len(findings) == 10
    freq = frequent_subject_regions(db, STUDENT_CHAIR_KTRU_CODE)
    assert freq["source"] == "pp"
    assert freq["selected_n"] >= 1


def test_sync_core_snapshot_template_remaps_stale_value_codes(tmp_path: Path):
    from eis_analytics_db import save_pp_core_snapshot
    from pp_core_catalog import persist_core_snapshot, sync_core_snapshot_template

    db = analytics_db_path(tmp_path)
    tmpl = _template()
    persist_core_snapshot(
        db,
        STUDENT_CHAIR_KTRU_CODE,
        [
            {
                "hash": "h-stale",
                "n": 1,
                "preview": "Ростовая группа=2; Вид материала спинки/сиденья=Фанера",
                "chars": [
                    {
                        "display_label": "Ростовая группа",
                        "characteristic_code": GROWTH,
                        "value_display": "2",
                    },
                    {
                        "display_label": "Вид материала спинки/сиденья",
                        "characteristic_code": "1f5fa9d7-5",
                        "value_display": "Фанера",
                    },
                ],
            }
        ],
        tmpl,
        total_notices=1,
    )
    rows = list_pp_core_variants(db, STUDENT_CHAIR_KTRU_CODE)
    meta = dict(rows[0].get("core_meta") or {})
    meta["template_version"] = 3
    rows[0]["core_meta"] = meta
    rows[0]["characteristics"] = [{"code": GROWTH, "value_code": "abc00a10-4"}]
    save_pp_core_snapshot(db, STUDENT_CHAIR_KTRU_CODE, rows)
    synced = sync_core_snapshot_template(db, STUDENT_CHAIR_KTRU_CODE, tmpl)
    assert _has(synced[0], GROWTH, GROWTH_2)
    assert _has(synced[0], "1f5fa9d7-5", PLYWOOD)
    assert synced[0]["map_status"] == "ready"
    assert synced[0]["core_meta"]["template_version"] == tmpl["version"]


def test_import_soap_pp_dump_keeps_volume(tmp_path: Path):
    from pp_core_catalog import SOAP_KTRU_CODE
    from pp_core_pp_dump import template_from_pp_dump_items

    soap_fixture = Path(__file__).parent / "fixtures" / "rts_zidk_milo_pp_mini.json"
    text = soap_fixture.read_text(encoding="utf-8")
    db = analytics_db_path(tmp_path)
    result = import_pp_dump_core(
        db,
        text,
        ktru_code=SOAP_KTRU_CODE,
        cache_dir=tmp_path / "ktru_rts_cache",
    )
    assert result["ok"] is True
    assert result["stats"]["used"] == 4
    assert result["stats"]["combos"] == 2
    rows = list_pp_core_variants(db, SOAP_KTRU_CODE)
    assert rows
    assert all(row["map_status"] == "ready" for row in rows)
    assert any(_has(row, "b36787f4-f", "2697360") or _has(row, "b36787f4-f", "2697361") for row in rows)
    assert any("Объем тары" in str(row.get("chars_preview") or "") for row in rows)
    tmpl = template_from_pp_dump_items(iter_pp_dump_items(text), ktru_code=SOAP_KTRU_CODE)
    ladders = price_ladders_for_variants(db, SOAP_KTRU_CODE, rows, tmpl)
    vol_codes = {
        str(ch.get("value_code") or "")
        for row in rows
        for ch in row["characteristics"]
        if ch.get("code") == "b36787f4-f"
    }
    assert vol_codes & {"2697360", "2697361"}
    top = rows[0]
    assert ladders[top["variant_no"]]["pp_n"] == 2

from __future__ import annotations

import json
from pathlib import Path

from eis_analytics_db import analytics_db_path, upsert_pp_monitor_finding
from pp_core_catalog import (
    CHAIR_KTRU_CODE,
    demand_key,
    find_selection,
    locked_code_set,
    persist_core_snapshot,
    price_ladder_from_prices,
)
from pp_core_registry import frequent_subject_regions, price_ladders_for_variants
from rts_oktmo_catalog import load_subject_oktmo

ROOT = Path(__file__).resolve().parents[1]
CHAIR_PATH = ROOT / "deploy" / "310111150_03.txt"
SELECT_BACK_MATERIAL = "94b59978-4"
SELECT_BACK_WOOD = "a3d81b08-3"


def _chair_item() -> dict:
    return json.loads(CHAIR_PATH.read_text(encoding="utf-8"))


def _combo(value: str, *, hash_id: str = "h1") -> dict:
    return {
        "hash": hash_id,
        "n": 10,
        "preview": f"Вид материала спинки={value}",
        "chars": [
            {
                "display_label": "Вид материала спинки",
                "characteristic_code": SELECT_BACK_MATERIAL,
                "value_display": value,
            }
        ],
    }


def _find_item(quality: str, places: list[dict]) -> dict:
    return {
        "ktruCharacteristics": [
            {
                "code": SELECT_BACK_MATERIAL,
                "name": "Вид материала спинки",
                "characteristicValues": [{"qualityDescription": quality, "valueCode": None}],
            }
        ],
        "deliveryPlaces": places,
    }


def test_find_selection_wood_and_type_kind():
    tmpl = _chair_item()
    exact = find_selection(_find_item("Дерево", []), tmpl)
    suffix = find_selection(_find_item("Дерево [тип 1] вид 2", []), tmpl)
    assert exact[SELECT_BACK_MATERIAL] == SELECT_BACK_WOOD
    assert suffix[SELECT_BACK_MATERIAL] == SELECT_BACK_WOOD
    locked = locked_code_set(tmpl)
    assert demand_key(exact, locked=locked) == demand_key(suffix, locked=locked)


def test_price_ladder_top5_slots():
    full = price_ladder_from_prices([10, 20, 30, 40, 50, 60])
    assert full["pp_n"] == 6
    assert full["rungs"] == 6
    assert full["top5"]["open_slots"] == 0
    assert full["top5"]["need_le"] == 50
    short = price_ladder_from_prices([10, 20, 30])
    assert short["top5"]["open_slots"] == 2
    assert short["top5"]["need_le"] is None


def test_price_ladders_match_core_variant(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    tmpl = _chair_item()
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_combo("Металл")], tmpl, total_notices=10)
    for i, price in enumerate([1000, 2000, 3000], start=1):
        upsert_pp_monitor_finding(
            db,
            offer_price_list_item_id=f"oid-{i}",
            ktru_code=CHAIR_KTRU_CODE,
            unit_price=float(price),
            find_json=json.dumps(_find_item("Металл", [{"oktmo": {"code": "01000000"}}])),
            trigger_recompute=False,
        )
    from eis_analytics_db import list_pp_core_variants

    rows = list_pp_core_variants(db, CHAIR_KTRU_CODE)
    ladders = price_ladders_for_variants(db, CHAIR_KTRU_CODE, rows, tmpl)
    stats = ladders[1]
    assert stats["pp_n"] == 3
    assert stats["top5"]["open_slots"] == 2
    assert stats["price_ladder"][0]["price"] == 1000


def test_price_ladder_uses_covering_pp_when_exact_missing(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    code = "99.00.00.000-00000001"
    tmpl = {
        "characteristics": [
            {
                "code": "c-a",
                "name": "A",
                "isActual": True,
                "characteristicValues": [{"valueCode": "a1", "qualityDescription": "one"}],
            },
            {
                "code": "c-b",
                "name": "B",
                "isActual": True,
                "characteristicValues": [{"valueCode": "b1", "qualityDescription": "two"}],
            },
        ]
    }
    persist_core_snapshot(
        db,
        code,
        [
            {
                "hash": "h-narrow",
                "n": 3,
                "chars": [{"display_label": "A", "characteristic_code": "c-a", "value_display": "one"}],
            }
        ],
        tmpl,
        total_notices=3,
    )
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="cover-1",
        ktru_code=code,
        unit_price=1500.0,
        find_json=json.dumps(
            {
                "ktruCharacteristics": [
                    {"code": "c-a", "name": "A", "characteristicValues": [{"qualityDescription": "one"}]},
                    {"code": "c-b", "name": "B", "characteristicValues": [{"qualityDescription": "two"}]},
                ],
                "deliveryPlaces": [{"unitPrice": 1500, "oktmo": {"code": "01000000"}}],
            }
        ),
        trigger_recompute=False,
    )
    from eis_analytics_db import list_pp_core_variants

    rows = list_pp_core_variants(db, code)
    ladders = price_ladders_for_variants(db, code, rows, tmpl)
    assert ladders[1]["pp_n"] == 1
    assert ladders[1]["price_ladder"][0]["price"] == 1500.0


def test_frequent_regions_ignore_small_pp(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    subjects = [row.code for row in load_subject_oktmo()[:42]]
    shared = subjects[:41]
    extra = subjects[41]

    def places(codes: list[str]) -> list[dict]:
        return [{"oktmo": {"code": c}} for c in codes]

    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="big-1",
        ktru_code=CHAIR_KTRU_CODE,
        unit_price=2500,
        find_json=json.dumps(_find_item("Металл", places(shared))),
        trigger_recompute=False,
    )
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="big-2",
        ktru_code=CHAIR_KTRU_CODE,
        unit_price=2600,
        find_json=json.dumps(_find_item("Металл", places(shared + [extra]))),
        trigger_recompute=False,
    )
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="small-1",
        ktru_code=CHAIR_KTRU_CODE,
        unit_price=9000,
        find_json=json.dumps(_find_item("Металл", places([extra]))),
        trigger_recompute=False,
    )
    result = frequent_subject_regions(db, CHAIR_KTRU_CODE)
    assert result["big_pp_n"] == 2
    assert result["selected_n"] == 40
    by_code = {row["code"]: row for row in result["items"]}
    assert by_code[shared[0]]["pp_n"] == 2
    assert extra not in by_code
    if "01000000" in shared:
        assert by_code["01000000"]["pp_n"] == 2

from __future__ import annotations

from pathlib import Path

from eis_analytics_db import analytics_db_path, list_pp_core_variants, list_pp_monitor_findings_by_ktru
from pp_core_catalog import persist_core_snapshot
from pp_core_registry import price_ladders_for_variants
from rts_pp_monitor import import_registry_find_dump, iter_json_objects

FIXTURE = Path(__file__).parent / "fixtures" / "rts_shkaf_mini.json"
CABINET = "31.01.12.139-00000001"
FACADE = "cb7e3a45-0"
GLASS = "66d040b5-c"
DRAWERS = "c202afa0-b"


def _cabinet_template() -> dict:
    return {
        "code": CABINET,
        "name": "Шкаф деревянный для документов",
        "characteristics": [
            {
                "code": FACADE,
                "name": "Тип фасада",
                "characteristicValues": [
                    {"qualityDescription": "Закрытый", "valueCode": "facade-closed"},
                    {"qualityDescription": "Распашной", "valueCode": "facade-swing"},
                ],
            },
            {
                "code": GLASS,
                "name": "Наличие остекления",
                "characteristicValues": [
                    {"qualityDescription": "Нет", "valueCode": "glass-no"},
                    {"qualityDescription": "Да", "valueCode": "glass-yes"},
                ],
            },
            {
                "code": DRAWERS,
                "name": "Наличие выдвижных ящиков",
                "characteristicValues": [
                    {"qualityDescription": "Нет", "valueCode": "drawers-no"},
                    {"qualityDescription": "Да", "valueCode": "drawers-yes"},
                ],
            },
        ],
    }


def _closed_combo() -> dict:
    return {
        "hash": "eis-closed",
        "n": 12,
        "preview": "Тип фасада=Закрытый; Наличие остекления=Нет; Наличие выдвижных ящиков=Нет",
        "chars": [
            {"display_label": "Тип фасада", "characteristic_code": FACADE, "value_display": "Закрытый"},
            {"display_label": "Наличие остекления", "characteristic_code": GLASS, "value_display": "Нет"},
            {
                "display_label": "Наличие выдвижных ящиков",
                "characteristic_code": DRAWERS,
                "value_display": "Нет",
            },
        ],
    }


def test_iter_json_objects_concatenated_pages():
    blobs = iter_json_objects(FIXTURE.read_text(encoding="utf-8"))
    assert len(blobs) == 2
    assert blobs[0]["total"] == 67
    assert len(blobs[0]["page"]) == 1
    assert len(blobs[1]["page"]) == 2


def test_import_registry_find_dump_filters_ktru(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    result = import_registry_find_dump(
        db,
        FIXTURE.read_text(encoding="utf-8"),
        ktru_code=CABINET,
        source="dump",
    )
    assert result["ok"] is True
    assert result["blobs"] == 2
    assert result["imported"] == 2
    assert result["filtered"] == 1
    rows = list_pp_monitor_findings_by_ktru(db, CABINET)
    assert len(rows) == 2
    by_id = {row["offer_price_list_item_id"]: row for row in rows}
    assert by_id["Q4x9mRtVnk9x"]["unit_price"] == 19530.0
    assert by_id["Q4x9mRtVnk9x"]["delivery_regions_count"] == 2
    find_item = by_id["Q4x9mRtVnk9x"]["find"]
    assert find_item["ktruCharacteristics"][0]["characteristicValues"][0]["qualityDescription"] == "Закрытый"
    assert by_id["AbCdEfGh1234"]["unit_price"] == 120000.0


def test_import_registry_find_dump_dedupes_repeat(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    text = FIXTURE.read_text(encoding="utf-8")
    first = import_registry_find_dump(db, text, ktru_code=CABINET)
    second = import_registry_find_dump(db, text, ktru_code=CABINET)
    assert first["imported"] == 2
    assert second["imported"] == 2
    assert len(list_pp_monitor_findings_by_ktru(db, CABINET)) == 2


def test_cabinet_core_price_ladder_from_dump(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    tmpl = _cabinet_template()
    persist_core_snapshot(db, CABINET, [_closed_combo()], tmpl, total_notices=12)
    import_registry_find_dump(db, FIXTURE.read_text(encoding="utf-8"), ktru_code=CABINET)
    rows = list_pp_core_variants(db, CABINET)
    ladders = price_ladders_for_variants(db, CABINET, rows, tmpl)
    stats = ladders[1]
    assert stats["pp_n"] == 1
    assert stats["price_ladder"][0]["price"] == 19530.0
    assert stats["top5"]["open_slots"] == 4

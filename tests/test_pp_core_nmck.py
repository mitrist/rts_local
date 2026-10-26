from __future__ import annotations

import json
from pathlib import Path

from eis_analytics_db import analytics_db_path, list_pp_core_variants
from pp_core_nmck import (
    STUDENT_CHAIR_KTRU_CODE,
    import_nmck_core,
    iter_nmck_contracts,
    match_region_to_subject,
)
from pp_core_registry import frequent_subject_regions, price_ladders_for_variants
from rts_oktmo_catalog import load_subject_oktmo

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "nmck_stul_mini.json"
TMPL_PATH = ROOT / "deploy" / "310111150_21.txt"
PLASTIC = "393996d7-d"
PLYWOOD = "7432f784-1"


def _template() -> dict:
    raw = json.loads(TMPL_PATH.read_text(encoding="utf-8"))
    item = raw.get("item") if isinstance(raw.get("item"), dict) else raw
    assert item["code"] == STUDENT_CHAIR_KTRU_CODE
    return item


def test_iter_nmck_contracts_concatenated_pages():
    rows = iter_nmck_contracts(FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 4
    assert {row["uid"] for row in rows} == {"u1", "u2", "u3", "u4"}


def test_match_nizhny_and_moscow_subjects():
    catalog = list(load_subject_oktmo())
    nizhny = match_region_to_subject("Нижегородская область", catalog)
    moscow = match_region_to_subject("Москва", catalog)
    oblast = match_region_to_subject("Московская область", catalog)
    spb = match_region_to_subject("Санкт-Петербург", catalog)
    assert nizhny is not None and "Нижегородск" in nizhny.name
    assert moscow is not None and "Москв" in moscow.name
    assert oblast is not None and moscow.code != oblast.code
    assert spb is not None and "Санкт-Петербург" in spb.name


def _dump(contracts: list[dict]) -> str:
    mid = max(1, len(contracts) // 2)
    return (
        json.dumps({"contracts": contracts[:mid], "total": len(contracts)}, ensure_ascii=False)
        + "\n"
        + json.dumps({"contracts": contracts[mid:], "total": len(contracts)}, ensure_ascii=False)
    )


def _contract(uid: str, price: float, chars: list[dict], region: str = "Нижегородская область") -> dict:
    return {
        "uid": uid,
        "number": uid,
        "regionName": region,
        "contractSignDate": "2026-09-01T00:00:00+03:00",
        "tru": {
            "ktruCode": STUDENT_CHAIR_KTRU_CODE,
            "price": price,
            "characteristics": chars,
        },
    }


def test_import_nmck_core_builds_ladder(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    tmpl = _template()
    plastic = [{"name": "Вид материала спинки/сиденья", "value": "Пластик"}]
    plywood = [
        {"name": "Вид материала спинки/сиденья", "value": "Фанера"},
        {"name": "Регулировка по высоте", "value": "Да"},
        {"name": "Ростовая группа", "value": "5"},
    ]
    ldsp = [{"name": "Вид материала спинки/сиденья", "value": "ЛДСП"}]
    rows_in = (
        [_contract(f"p{i}", 3000 + i, plastic, "Нижегородская область") for i in range(5)]
        + [_contract(f"w{i}", 2800 + i, plywood, "Москва") for i in range(3)]
        + [_contract(f"l{i}", 2500, ldsp, "Санкт-Петербург") for i in range(2)]
        + [_contract("empty", 2000, [{"name": "Цвет", "value": "Синий"}], "Липецкая область")]
    )
    result = import_nmck_core(
        db,
        _dump(rows_in),
        ktru_code=STUDENT_CHAIR_KTRU_CODE,
        template=tmpl,
    )
    assert result["ok"] is True
    assert result["stats"]["used"] == 10
    assert result["stats"]["skipped_empty"] == 1
    rows = list_pp_core_variants(db, STUDENT_CHAIR_KTRU_CODE)
    assert len(rows) == 2
    plastic_row = next(row for row in rows if any(ch.get("value_code") == PLASTIC for ch in row["characteristics"]))
    plywood_row = next(row for row in rows if any(ch.get("value_code") == PLYWOOD for ch in row["characteristics"]))
    assert plastic_row["n"] == 5
    assert plywood_row["n"] == 3
    assert plastic_row["map_status"] == "ready"
    assert plywood_row["map_status"] == "ready"
    ladders = price_ladders_for_variants(db, STUDENT_CHAIR_KTRU_CODE, rows, tmpl)
    plastic_ladder = ladders[plastic_row["variant_no"]]
    assert plastic_ladder["pp_n"] == 5
    prices = {step["price"] for step in plastic_ladder["price_ladder"]}
    assert 3000.0 in prices
    assert 3004.0 in prices
    freq = frequent_subject_regions(db, STUDENT_CHAIR_KTRU_CODE)
    assert freq["source"] == "nmck"
    assert freq["selected_n"] >= 2
    codes = {item["code"] for item in freq["items"]}
    catalog = {row.code: row for row in load_subject_oktmo()}
    assert any("Нижегородск" in catalog[c].name for c in codes if c in catalog)

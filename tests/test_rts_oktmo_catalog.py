from __future__ import annotations

import json
from pathlib import Path

import pytest

from rts_oktmo_catalog import (
    SUBJECT_FEDERAL_DISTRICT,
    OktmoCatalogError,
    federal_district_for,
    federal_districts,
    get_by_code,
    load_subject_oktmo,
)

ROOT = Path(__file__).resolve().parents[1]
REAL_CATALOG = ROOT / "PP_create" / "oktmo_code.txt"


def _write_catalog(path: Path) -> Path:
    path.write_text(
        json.dumps(
            [
                {
                    "code": "01000000",
                    "fullName": "Муниципальные образования Алтайского края",
                    "actual": True,
                    "category": "MunicipalEntitiesOfSubjectsRf",
                },
                {
                    "code": "99000000",
                    "fullName": "Скрытый субъект",
                    "actual": False,
                    "category": "Undefined",
                },
                {
                    "code": "03000000",
                    "fullName": "Муниципальные образования Краснодарского края",
                    "actual": True,
                    "category": "MunicipalEntitiesOfSubjectsRf",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_load_real_oktmo_catalog():
    rows = load_subject_oktmo(REAL_CATALOG)
    assert len(rows) >= 3
    by_code = {row.code: row for row in rows}
    assert by_code["01000000"].name == "Муниципальные образования Алтайского края"
    assert get_by_code("03000000", path=REAL_CATALOG).code == "03000000"
    assert get_by_code("04000000", path=REAL_CATALOG).name.startswith("Муниципальные образования")


def test_real_catalog_codes_are_mapped_to_one_district():
    rows = load_subject_oktmo(REAL_CATALOG)
    codes = [row.code for row in rows]
    assert len(codes) == len(set(codes))
    missing = [code for code in codes if code not in SUBJECT_FEDERAL_DISTRICT]
    assert missing == []
    assert federal_district_for("01000000") == ("sfo", "Сибирский")
    assert federal_district_for("45000000") == ("cfo", "Центральный")
    assert federal_district_for("55000000") == ("other", "Вне округов")
    assert federal_district_for("99999999") == ("other", "Вне округов")
    names = [row["name"] for row in federal_districts()]
    assert names[0] == "Центральный"
    assert names[-1] == "Вне округов"
    by_code = {row.code: row.as_dict() for row in rows}
    assert by_code["45000000"]["federal_district"] == "cfo"
    assert by_code["45000000"]["federal_district_name"] == "Центральный"


def test_skips_actual_false_and_lookup(tmp_path):
    path = _write_catalog(tmp_path / "oktmo.json")
    rows = load_subject_oktmo(path)
    assert [row.code for row in rows] == ["01000000", "03000000"]
    assert get_by_code("01000000", path=path).category == "MunicipalEntitiesOfSubjectsRf"
    with pytest.raises(OktmoCatalogError):
        get_by_code("99000000", path=path)
    with pytest.raises(OktmoCatalogError):
        get_by_code("00000000", path=path)

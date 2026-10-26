from __future__ import annotations

import json
from pathlib import Path

import pytest

from rts_oktmo_catalog import OktmoCatalogError, get_by_code, load_subject_oktmo

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


def test_skips_actual_false_and_lookup(tmp_path):
    path = _write_catalog(tmp_path / "oktmo.json")
    rows = load_subject_oktmo(path)
    assert [row.code for row in rows] == ["01000000", "03000000"]
    assert get_by_code("01000000", path=path).category == "MunicipalEntitiesOfSubjectsRf"
    with pytest.raises(OktmoCatalogError):
        get_by_code("99000000", path=path)
    with pytest.raises(OktmoCatalogError):
        get_by_code("00000000", path=path)

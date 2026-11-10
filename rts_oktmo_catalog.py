"""RTS subject-level OKTMO dictionary (parentCode=null dump)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_OKTMO_CATALOG = ROOT / "PP_create" / "oktmo_code.txt"


class OktmoCatalogError(ValueError):
    """Unknown or empty OKTMO catalog entry."""


@dataclass(frozen=True, slots=True)
class OktmoSubject:
    code: str
    name: str
    category: str
    actual: bool

    def as_dict(self) -> dict[str, Any]:
        district_id, district_name = federal_district_for(self.code)
        return {
            "code": self.code,
            "name": self.name,
            "category": self.category,
            "actual": self.actual,
            "federal_district": district_id,
            "federal_district_name": district_name,
        }


FEDERAL_DISTRICTS: tuple[tuple[str, str], ...] = (
    ("cfo", "Центральный"),
    ("szfo", "Северо-Западный"),
    ("ufo", "Южный"),
    ("skfo", "Северо-Кавказский"),
    ("pfo", "Приволжский"),
    ("urfo", "Уральский"),
    ("sfo", "Сибирский"),
    ("dfo", "Дальневосточный"),
    ("other", "Вне округов"),
)

_DISTRICT_NAMES = dict(FEDERAL_DISTRICTS)

# Subject OKTMO (parentCode=null) → federal district. Unlisted codes fall back to "other".
SUBJECT_FEDERAL_DISTRICT: dict[str, str] = {
    "14000000": "cfo",
    "15000000": "cfo",
    "17000000": "cfo",
    "20000000": "cfo",
    "24000000": "cfo",
    "28000000": "cfo",
    "29000000": "cfo",
    "34000000": "cfo",
    "38000000": "cfo",
    "42000000": "cfo",
    "45000000": "cfo",
    "46000000": "cfo",
    "54000000": "cfo",
    "61000000": "cfo",
    "66000000": "cfo",
    "68000000": "cfo",
    "70000000": "cfo",
    "78000000": "cfo",
    "11000000": "szfo",
    "19000000": "szfo",
    "27000000": "szfo",
    "40000000": "szfo",
    "41000000": "szfo",
    "47000000": "szfo",
    "49000000": "szfo",
    "58000000": "szfo",
    "86000000": "szfo",
    "87000000": "szfo",
    "02000000": "ufo",
    "03000000": "ufo",
    "12000000": "ufo",
    "18000000": "ufo",
    "21000000": "ufo",
    "23000000": "ufo",
    "35000000": "ufo",
    "43000000": "ufo",
    "60000000": "ufo",
    "67000000": "ufo",
    "74000000": "ufo",
    "79000000": "ufo",
    "85000000": "ufo",
    "07000000": "skfo",
    "26000000": "skfo",
    "82000000": "skfo",
    "83000000": "skfo",
    "90000000": "skfo",
    "91000000": "skfo",
    "96000000": "skfo",
    "22000000": "pfo",
    "33000000": "pfo",
    "36000000": "pfo",
    "53000000": "pfo",
    "56000000": "pfo",
    "57000000": "pfo",
    "63000000": "pfo",
    "73000000": "pfo",
    "80000000": "pfo",
    "88000000": "pfo",
    "89000000": "pfo",
    "92000000": "pfo",
    "94000000": "pfo",
    "97000000": "pfo",
    "37000000": "urfo",
    "65000000": "urfo",
    "71000000": "urfo",
    "75000000": "urfo",
    "01000000": "sfo",
    "04000000": "sfo",
    "25000000": "sfo",
    "32000000": "sfo",
    "50000000": "sfo",
    "52000000": "sfo",
    "69000000": "sfo",
    "84000000": "sfo",
    "93000000": "sfo",
    "95000000": "sfo",
    "05000000": "dfo",
    "08000000": "dfo",
    "10000000": "dfo",
    "30000000": "dfo",
    "44000000": "dfo",
    "64000000": "dfo",
    "76000000": "dfo",
    "77000000": "dfo",
    "81000000": "dfo",
    "98000000": "dfo",
    "99000000": "dfo",
    "55000000": "other",
}


def federal_districts() -> list[dict[str, str]]:
    return [{"id": district_id, "name": name} for district_id, name in FEDERAL_DISTRICTS]


def federal_district_for(code: str) -> tuple[str, str]:
    district_id = SUBJECT_FEDERAL_DISTRICT.get(str(code or "").strip(), "other")
    if district_id not in _DISTRICT_NAMES:
        district_id = "other"
    return district_id, _DISTRICT_NAMES[district_id]


def default_oktmo_catalog_path() -> Path:
    raw = os.environ.get("RTS_OKTMO_CATALOG", "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_OKTMO_CATALOG


def _parse_entries(raw: Any) -> list[OktmoSubject]:
    if not isinstance(raw, list):
        raise OktmoCatalogError("Справочник ОКТМО должен быть JSON-массивом.")
    out: list[OktmoSubject] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        if row.get("actual") is False:
            continue
        code = str(row.get("code") or "").strip()
        name = str(row.get("fullName") or row.get("name") or "").strip()
        if not code or not name:
            continue
        if code in seen:
            continue
        seen.add(code)
        out.append(
            OktmoSubject(
                code=code,
                name=name,
                category=str(row.get("category") or "").strip(),
                actual=True,
            )
        )
    out.sort(key=lambda x: (x.name.casefold(), x.code))
    return out


@lru_cache(maxsize=4)
def _load_cached(path_str: str, mtime_ns: int) -> tuple[OktmoSubject, ...]:
    del mtime_ns
    path = Path(path_str)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OktmoCatalogError(f"Файл справочника ОКТМО не найден: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OktmoCatalogError(f"Справочник ОКТМО повреждён: {path}") from exc
    return tuple(_parse_entries(raw))


def load_subject_oktmo(path: Path | None = None) -> list[OktmoSubject]:
    catalog = Path(path) if path is not None else default_oktmo_catalog_path()
    try:
        mtime_ns = catalog.stat().st_mtime_ns
    except OSError as exc:
        raise OktmoCatalogError(f"Файл справочника ОКТМО не найден: {catalog}") from exc
    return list(_load_cached(str(catalog.resolve()), mtime_ns))


def get_by_code(code: str, *, path: Path | None = None) -> OktmoSubject:
    key = str(code or "").strip()
    if not key:
        raise OktmoCatalogError("Код ОКТМО не задан.")
    for item in load_subject_oktmo(path):
        if item.code == key:
            return item
    raise OktmoCatalogError(f"ОКТМО {key} нет в справочнике РТС.")

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
        return {
            "code": self.code,
            "name": self.name,
            "category": self.category,
            "actual": self.actual,
        }


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

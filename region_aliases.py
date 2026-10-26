"""Map RTS auction/search Region text to subject OKTMO for statistics."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pp_core_nmck import match_region_to_subject
from rts_oktmo_catalog import OktmoSubject, load_subject_oktmo

ROOT = Path(__file__).resolve().parent
DEFAULT_ALIASES_PATH = ROOT / "PP_create" / "region_aliases.json"
UNMAPPED = "unmapped"

_MO_PREFIX_RE = re.compile(r"^муниципальные образования\s+", re.IGNORECASE)
_FED_CITY_RE = re.compile(r"^города федерального значения\s+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[«»\"'()]")
_CITY_NOM = {
    "москвы": "Москва",
    "санкт-петербурга": "Санкт-Петербург",
    "севастополя": "Севастополь",
}
EXTRA_RTS_ALIASES = {
    "Кемеровская обл.": "32000000",
    "Респ. Адыгея": "79000000",
    "Респ. Тыва": "93000000",
    "Респ. Хакасия": "95000000",
    "г. Севастополь": "67000000",
    "г. Москва": "45000000",
    "г. Санкт-Петербург": "40000000",
    "Ханты-Мансийский Автономный округ - Югра АО": "71800000",
    "Ханты-Мансийский АО": "71800000",
    "Ямало-Ненецкий АО": "71900000",
    "Чувашия. Чувашская Республика -": "97000000",
    "Чувашская Республика": "97000000",
}


@dataclass(frozen=True, slots=True)
class RegionResolve:
    raw: str
    subject_oktmo: str
    subject_name: str
    source: str

    @property
    def mapped(self) -> bool:
        return bool(self.subject_oktmo) and self.subject_oktmo != UNMAPPED


def norm_alias(value: object) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    text = text.replace(".", " ")
    text = _WS_RE.sub(" ", text).strip()
    return text


def _strip_mo(name: str) -> str:
    text = str(name or "").strip()
    text = _MO_PREFIX_RE.sub("", text, count=1).strip() or text
    text = _FED_CITY_RE.sub("", text, count=1).strip() or text
    return text


def _paren_chunks(text: str) -> list[str]:
    chunks = [text]
    for part in re.findall(r"\(([^)]+)\)", text):
        chunks.append(part.strip())
    chunks[0] = re.sub(r"\s*\([^)]+\)", "", chunks[0]).strip()
    return [c for c in chunks if c]


def _republic_nominative(genitive: str) -> str:
    text = str(genitive or "").strip()
    if text.endswith("еи"):
        return text[:-2] + "ея"
    if text.endswith("ии"):
        return text[:-2] + "ия"
    if text.endswith("ы"):
        return text[:-1] + "а"
    return text


def catalog_alias_strings(full_name: str) -> list[str]:
    """Nominative / RTS-short forms from OKTMO fullName."""
    raw = str(full_name or "").strip()
    if not raw:
        return []
    stripped = _strip_mo(raw)
    out: list[str] = [raw, stripped]
    n = stripped
    city_nom = _CITY_NOM.get(norm_alias(n))
    if city_nom:
        out.extend([city_nom, f"г. {city_nom}", f"г {city_nom}", f"город {city_nom}"])

    m = re.match(r"^(.+?)ского края$", n, re.IGNORECASE)
    if m:
        out.append(f"{m.group(1)}ский край")

    m = re.match(r"^(.+?)ской области\b", n, re.IGNORECASE)
    if m:
        stem = m.group(1)
        out.append(f"{stem}ская область")
        out.append(f"{stem}ская обл.")
        out.append(f"{stem}ская обл")

    m = re.match(r"^Республики\s+(.+)$", n, re.IGNORECASE)
    if m:
        for chunk in _paren_chunks(m.group(1).strip()):
            nom = _republic_nominative(chunk)
            out.append(f"Республика {nom}")
            out.append(f"Респ. {nom}")
            out.append(f"Респ {nom}")
            out.append(nom)

    m = re.match(r"^(.+?)ской Республики\b", n, re.IGNORECASE)
    if m:
        stem = m.group(1)
        out.append(f"{stem}ская Республика")
        out.append(f"Респ. {stem}ская")
        out.append(f"Респ {stem}ская")
        tail = n.split("-", 1)
        if len(tail) == 2:
            extra = _republic_nominative(tail[1].strip())
            if extra:
                out.append(extra)
                out.append(f"Респ. {extra}")

    m = re.match(r"^(.+?)ой Республики$", n, re.IGNORECASE)
    if m and "ской" not in n.lower():
        stem = m.group(1)
        out.append(f"{stem}ая Республика")
        out.append(f"Респ. {stem}ая")

    m = re.match(r"^(.+?)ого автономного округа.*$", n, re.IGNORECASE)
    if m:
        stem = m.group(1)
        out.append(f"{stem}ий автономный округ")
        out.append(f"{stem}ий АО")
        out.append(f"{stem}ий авт. окр.")

    m = re.match(r"^(.+?)ой автономной области$", n, re.IGNORECASE)
    if m:
        stem = m.group(1)
        out.append(f"{stem}ая автономная область")
        out.append(f"{stem}ая АО")

    m = re.match(r"^города\s+(.+)$", n, re.IGNORECASE)
    if m:
        gen = m.group(1).strip()
        nom = _CITY_NOM.get(norm_alias(gen), gen)
        out.append(nom)
        out.append(f"г. {nom}")
        out.append(f"г {nom}")
        out.append(f"город {nom}")

    seen: set[str] = set()
    uniq: list[str] = []
    for item in out:
        key = norm_alias(item)
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def aliases_from_catalog(catalog: list[OktmoSubject] | None = None) -> dict[str, str]:
    subjects = list(catalog) if catalog is not None else list(load_subject_oktmo())
    mapping: dict[str, str] = {}
    for subj in subjects:
        for alias in catalog_alias_strings(subj.name):
            key = norm_alias(alias)
            mapping.setdefault(key, subj.code)
    for alias, code in EXTRA_RTS_ALIASES.items():
        mapping[norm_alias(alias)] = code
    return mapping


def load_alias_file(path: Path | None = None) -> dict[str, str]:
    aliases_path = Path(path) if path is not None else DEFAULT_ALIASES_PATH
    if not aliases_path.is_file():
        return {}
    try:
        raw = json.loads(aliases_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    blob = raw.get("aliases") if isinstance(raw, dict) else raw
    if not isinstance(blob, dict):
        return {}
    out: dict[str, str] = {}
    for alias, code in blob.items():
        key = norm_alias(alias)
        oktmo = str(code or "").strip()
        if key and oktmo:
            out[key] = oktmo
    return out


@lru_cache(maxsize=4)
def _merged_alias_map(path_str: str, mtime_ns: int) -> dict[str, str]:
    del mtime_ns
    merged = aliases_from_catalog()
    merged.update(load_alias_file(Path(path_str) if path_str else DEFAULT_ALIASES_PATH))
    return merged


def alias_map(*, path: Path | None = None) -> dict[str, str]:
    aliases_path = Path(path) if path is not None else DEFAULT_ALIASES_PATH
    try:
        mtime_ns = aliases_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return dict(_merged_alias_map(str(aliases_path), mtime_ns))


def _subject_name(code: str, catalog: list[OktmoSubject]) -> str:
    for subj in catalog:
        if subj.code == code:
            return _strip_mo(subj.name) or subj.name
    return ""


def resolve_region(
    raw: str,
    *,
    catalog: list[OktmoSubject] | None = None,
    aliases: dict[str, str] | None = None,
) -> RegionResolve:
    text = str(raw or "").strip()
    if not text:
        return RegionResolve(raw="", subject_oktmo=UNMAPPED, subject_name="", source="empty")
    subjects = list(catalog) if catalog is not None else list(load_subject_oktmo())
    mapping = aliases if aliases is not None else alias_map()
    key = norm_alias(text)
    code = mapping.get(key, "")
    if code:
        return RegionResolve(
            raw=text,
            subject_oktmo=code,
            subject_name=_subject_name(code, subjects),
            source="alias",
        )
    hit = match_region_to_subject(text, subjects)
    if hit is not None:
        return RegionResolve(
            raw=text,
            subject_oktmo=hit.code,
            subject_name=_strip_mo(hit.name) or hit.name,
            source="fallback",
        )
    return RegionResolve(raw=text, subject_oktmo=UNMAPPED, subject_name="", source=UNMAPPED)


def build_alias_seed(catalog: list[OktmoSubject] | None = None) -> dict[str, Any]:
    subjects = list(catalog) if catalog is not None else list(load_subject_oktmo())
    aliases: dict[str, str] = dict(EXTRA_RTS_ALIASES)
    for subj in subjects:
        for alias in catalog_alias_strings(subj.name):
            aliases.setdefault(alias, subj.code)
    return {
        "comment": "RTS auction/search Region text → subject OKTMO. Overlay on generated catalog forms.",
        "aliases": dict(sorted(aliases.items(), key=lambda kv: (kv[1], kv[0].casefold()))),
    }

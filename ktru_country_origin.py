"""Справочник стран происхождения для черновиков ПП (CSV)."""

from __future__ import annotations

import csv
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rts_preoffers import normalize_text

CSV_DELIMITER = ";"
CSV_HEADER = ("ktru_code", "position_display_name", "country_name", "enabled", "row_id")

COUNTRY_CHOICES: tuple[tuple[str, str], ...] = (
    ("643", "Российская Федерация"),
    ("156", "Китайская Народная Республика"),
)

COUNTRY_NAME_TO_OKSM: dict[str, tuple[str, str]] = {
    name.casefold(): (code, name) for code, name in COUNTRY_CHOICES
}


@dataclass(frozen=True, slots=True)
class CountryOfOrigin:
    code: str
    name: str


DEFAULT_COUNTRY = CountryOfOrigin("643", "Российская Федерация")


@dataclass(slots=True)
class CountryOriginRow:
    row_id: str
    ktru_code: str
    title: str
    country_name: str
    enabled: bool = True

    @property
    def country(self) -> CountryOfOrigin:
        return country_from_name(self.country_name)

    def as_csv_dict(self) -> dict[str, str]:
        return {
            "ktru_code": self.ktru_code,
            "position_display_name": self.title,
            "country_name": self.country_name,
            "enabled": "1" if self.enabled else "0",
            "row_id": self.row_id,
        }


def default_country_origin_path(project_root: Path | None = None) -> Path:
    raw = os.environ.get("RTS_KTRU_COUNTRY_FILE", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (Path.cwd() / p).resolve()
    root = (project_root or Path(__file__).resolve().parent).resolve()
    return root / "КТРУ для ПП" / "Country_of_origin" / "country_origin.csv"


def country_from_name(name: str) -> CountryOfOrigin:
    key = str(name or "").strip().casefold()
    if not key:
        return DEFAULT_COUNTRY
    pair = COUNTRY_NAME_TO_OKSM.get(key)
    if pair:
        return CountryOfOrigin(pair[0], pair[1])
    raise ValueError(f"Неизвестная страна: {name!r}")


def is_known_country_name(name: str) -> bool:
    return str(name or "").strip().casefold() in COUNTRY_NAME_TO_OKSM


def _clean(value: object) -> str:
    return str(value or "").replace("\xa0", " ").strip()


def _parse_enabled(value: object, *, default: bool = True) -> bool:
    raw = _clean(value).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "да")


def _new_row_id() -> str:
    return uuid.uuid4().hex[:12]


def _header_map(fieldnames: list[str] | None) -> dict[str, str | None]:
    if not fieldnames:
        return {}
    out: dict[str, str | None] = {}
    for h in fieldnames:
        key = _clean(h).casefold()
        if key in ("ktru_code", "код ктру", "ктру"):
            out["ktru"] = h
        elif key in ("position_display_name", "наименование", "название предложения", "название"):
            out["title"] = h
        elif key in ("country_name", "столбец1", "страна", "страна происхождения"):
            out["country"] = h
        elif key == "enabled":
            out["enabled"] = h
        elif key == "row_id":
            out["row_id"] = h
    return out


def _parse_row(raw: dict[str, str], header: dict[str, str | None]) -> CountryOriginRow | None:
    title_key = header.get("title")
    country_key = header.get("country")
    if not title_key or not country_key:
        return None
    title = _clean(raw.get(title_key))
    country_name = _clean(raw.get(country_key))
    if not title or not country_name:
        return None
    ktru_key = header.get("ktru")
    ktru_code = _clean(raw.get(ktru_key)) if ktru_key else ""
    enabled_key = header.get("enabled")
    enabled = _parse_enabled(raw.get(enabled_key) if enabled_key else None, default=True)
    row_id_key = header.get("row_id")
    row_id = _clean(raw.get(row_id_key)) if row_id_key else ""
    if not row_id:
        row_id = _new_row_id()
    return CountryOriginRow(
        row_id=row_id,
        ktru_code=ktru_code,
        title=title,
        country_name=country_name,
        enabled=enabled,
    )


def load_country_origin_rows(path: Path) -> list[CountryOriginRow]:
    if not path.is_file():
        return []
    rows: list[CountryOriginRow] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=CSV_DELIMITER)
        header = _header_map(list(reader.fieldnames or []))
        if not header.get("title") or not header.get("country"):
            return rows
        for raw in reader:
            if not isinstance(raw, dict):
                continue
            row = _parse_row(raw, header)
            if row is not None:
                rows.append(row)
    return rows


def save_country_origin_rows(path: Path, rows: list[CountryOriginRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_HEADER), delimiter=CSV_DELIMITER)
        writer.writeheader()
        for row in rows:
            if not is_known_country_name(row.country_name):
                raise ValueError(f"Неизвестная страна: {row.country_name!r}")
            writer.writerow(row.as_csv_dict())
    tmp.replace(path)


def upsert_country_origin_row(
    path: Path,
    *,
    row_id: str = "",
    ktru_code: str = "",
    title: str = "",
    country_name: str = "",
    enabled: bool = True,
) -> CountryOriginRow:
    title_clean = _clean(title)
    country_clean = _clean(country_name)
    if not title_clean:
        raise ValueError("Укажите наименование.")
    if not is_known_country_name(country_clean):
        raise ValueError(f"Неизвестная страна: {country_clean!r}")
    rows = load_country_origin_rows(path)
    rid = _clean(row_id)
    payload = CountryOriginRow(
        row_id=rid or _new_row_id(),
        ktru_code=_clean(ktru_code),
        title=title_clean,
        country_name=country_clean,
        enabled=enabled,
    )
    replaced = False
    out: list[CountryOriginRow] = []
    for row in rows:
        if rid and row.row_id == rid:
            out.append(payload)
            replaced = True
        else:
            out.append(row)
    if not replaced:
        out.append(payload)
    save_country_origin_rows(path, out)
    return payload


def delete_country_origin_row(path: Path, row_id: str) -> bool:
    rid = _clean(row_id)
    if not rid:
        return False
    rows = load_country_origin_rows(path)
    kept = [r for r in rows if r.row_id != rid]
    if len(kept) == len(rows):
        return False
    save_country_origin_rows(path, kept)
    return True


def resolve_country_for_draft(
    *,
    ktru_code: str = "",
    title: str = "",
    rows: list[CountryOriginRow] | None = None,
    path: Path | None = None,
) -> CountryOfOrigin:
    data = rows if rows is not None else load_country_origin_rows(path) if path else []
    enabled = [r for r in data if r.enabled]
    code = _clean(ktru_code)
    if code:
        for row in enabled:
            if row.ktru_code == code:
                return row.country
    title_key = normalize_text(title)
    if title_key:
        for row in enabled:
            if not row.ktru_code and normalize_text(row.title) == title_key:
                return row.country
        for row in enabled:
            if normalize_text(row.title) == title_key:
                return row.country
    return DEFAULT_COUNTRY


def national_mode_groups_for_country(
    country: CountryOfOrigin,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Блоки национального режима ПП 1875: только для происхождения РФ."""
    if country.code == "643":
        return (
            [
                {
                    "countryCode": "643",
                    "nationalModeInfoTypeId": "IndustrialProductsRussianFederation",
                    "nationalModeInfos": [{"registryNumberNotProvided": True}],
                }
            ],
            [
                {
                    "countryCode": "643",
                    "nationalModeInfoTypeId": "SoftwareRussianFederation",
                    "nationalModeInfos": [{"registryNumberNotProvided": True}],
                }
            ],
        )
    return ([], [])


def apply_country_to_draft_item(item: dict[str, Any], country: CountryOfOrigin) -> None:
    item["countriesOfOrigin"] = [{"code": country.code, "name": country.name}]
    national, software = national_mode_groups_for_country(country)
    item["nationalModeInfoGroups"] = national
    item["softwareNationalModeInfoGroups"] = software

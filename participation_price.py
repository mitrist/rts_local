"""Participation economics: CP, CPK, profitability from price_ord.csv."""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rts_pp_api import parse_oktmo_from_delivery_place

_CP_DISCOUNT = 0.97
_PERCENT_RE = re.compile(r"[\d,]+")
NO_PRICE_ORD_ROW_MESSAGE = "Нет данных по накладным расходам для пары КТРУ-Регион"


def economics_unavailable_message(error_reason: str) -> str:
    if error_reason == "no_price_ord_row":
        return NO_PRICE_ORD_ROW_MESSAGE
    if error_reason == "disabled":
        return "н/д"
    return "н/д"


def participation_economics_disabled() -> dict[str, Any]:
    """Экономика отключена (например, без справочника price_ord на VM)."""
    return {
        "available": False,
        "cp": None,
        "cpk": None,
        "zc": None,
        "nr": None,
        "or_amount": None,
        "profitability_pct": None,
        "profitability_text": "н/д",
        "condition": None,
        "profitability_class": profitability_css_class(None),
        "unit_price_for_draft": None,
        "error_reason": "disabled",
        "unavailable_message": "",
    }


def default_price_ord_path() -> Path:
    raw = os.environ.get("RTS_PRICE_ORD_FILE", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else Path.cwd() / p
    return Path(__file__).resolve().parent / "price_order" / "price_ord.csv"


def parse_percent(text: str) -> float | None:
    raw = str(text or "").strip().replace("%", "").replace("\xa0", " ").strip()
    if not raw:
        return None
    raw = raw.replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_money_amount(text: str) -> float | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    cleaned = raw.replace("\xa0", " ").replace(" ", "")
    for suffix in ("RUB", "руб.", "руб", "₽"):
        if cleaned.lower().endswith(suffix.lower()):
            cleaned = cleaned[: -len(suffix)].strip()
    cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_quantity(text: str) -> float | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    cleaned = raw.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def format_money_ru(value: float, *, decimals: int = 2) -> str:
    rounded = round(float(value), decimals)
    whole, frac = f"{rounded:.{decimals}f}".split(".")
    return f"{whole},{frac}"


def format_percent_ru(value: float, *, decimals: int = 1) -> str:
    return f"{round(float(value), decimals):.{decimals}f}".replace(".", ",")


def profitability_condition(profitability_pct: float) -> int:
    if profitability_pct >= 50.0:
        return 1
    if profitability_pct >= 30.0:
        return 2
    return 3


def profitability_css_class(condition: int | None) -> str:
    if condition == 1:
        return "offer-profit--green"
    if condition == 2:
        return "offer-profit--yellow"
    if condition == 3:
        return "offer-profit--red"
    return "offer-profit--na"


def subject_candidates_from_oktmo(oktmo_code: str) -> list[str]:
    digits = re.sub(r"\D", "", str(oktmo_code or ""))
    if not digits:
        return []
    candidates: list[str] = []
    if len(digits) >= 2:
        region = digits[:2]
        candidates.append(f"{region}000000")
        candidates.append(f"{int(region)}000000")
    candidates.append(digits)
    if len(digits) >= 8:
        candidates.append(digits[:8])
    if len(digits) >= 11:
        candidates.append(digits[:11])
    seen: set[str] = set()
    out: list[str] = []
    for item in candidates:
        norm = _normalize_subject(item)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def oktmo_subject_code(delivery_place: str, *, region: str = "") -> str | None:
    parsed = parse_oktmo_from_delivery_place(delivery_place or "")
    if parsed and parsed.get("code"):
        return subject_candidates_from_oktmo(parsed["code"])[0]
    return None


@dataclass
class PriceOrdRow:
    ktru_code: str
    subject_oktmo: str
    municipality: str
    overhead_pct: float
    markup_pct: float


@dataclass
class PriceOrdCatalog:
    by_key: dict[tuple[str, str], PriceOrdRow] = field(default_factory=dict)
    by_ktru: dict[str, list[PriceOrdRow]] = field(default_factory=dict)

    def get(self, ktru_code: str, subject_oktmo: str) -> PriceOrdRow | None:
        key = (str(ktru_code or "").strip(), _normalize_subject(subject_oktmo))
        if key in self.by_key:
            return self.by_key[key]
        alt = (key[0], str(int(key[1])) if key[1].isdigit() else key[1])
        return self.by_key.get(alt)

    def resolve_row(
        self,
        ktru_code: str,
        *,
        delivery_place: str = "",
        region: str = "",
    ) -> PriceOrdRow | None:
        ktru = str(ktru_code or "").strip()
        if not ktru:
            return None
        parsed = parse_oktmo_from_delivery_place(delivery_place or "")
        if parsed and parsed.get("code"):
            for candidate in subject_candidates_from_oktmo(parsed["code"]):
                row = self.get(ktru, candidate)
                if row:
                    return row
        region_norm = str(region or "").strip().lower()
        if region_norm:
            for row in self.by_ktru.get(ktru, []):
                if region_norm in row.municipality.lower():
                    return row
                if region_norm in row.municipality.lower().replace("ё", "е"):
                    return row
        return None


def _normalize_subject(code: str) -> str:
    digits = re.sub(r"\D", "", str(code or ""))
    if not digits:
        return ""
    if len(digits) == 7 and digits.startswith("0"):
        return digits
    try:
        return str(int(digits))
    except ValueError:
        return digits


def load_price_ord_catalog(path: Path) -> PriceOrdCatalog:
    catalog = PriceOrdCatalog()
    if not path.is_file():
        return catalog
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        header = next(reader, None)
        if not header:
            return catalog
        for row in reader:
            if len(row) < 6:
                continue
            ktru = str(row[0] or "").strip()
            subject = _normalize_subject(row[2])
            overhead = parse_percent(row[4])
            markup = parse_percent(row[5])
            if not ktru or not subject or overhead is None or markup is None:
                continue
            entry = PriceOrdRow(
                ktru_code=ktru,
                subject_oktmo=subject,
                municipality=str(row[3] or "").strip(),
                overhead_pct=overhead,
                markup_pct=markup,
            )
            catalog.by_key[(ktru, subject)] = entry
            catalog.by_ktru.setdefault(ktru, []).append(entry)
    return catalog


_catalog_cache: PriceOrdCatalog | None = None
_catalog_mtime: float | None = None
_catalog_path: Path | None = None


def reset_catalog_cache() -> None:
    global _catalog_cache, _catalog_mtime, _catalog_path
    _catalog_cache = None
    _catalog_mtime = None
    _catalog_path = None


def get_price_ord_catalog(path: Path | None = None) -> PriceOrdCatalog:
    global _catalog_cache, _catalog_mtime, _catalog_path
    resolved = (path or default_price_ord_path()).resolve()
    if not resolved.is_file():
        return PriceOrdCatalog()
    mtime = resolved.stat().st_mtime
    if _catalog_cache is not None and _catalog_path == resolved and _catalog_mtime == mtime:
        return _catalog_cache
    _catalog_cache = load_price_ord_catalog(resolved)
    _catalog_mtime = mtime
    _catalog_path = resolved
    return _catalog_cache


def position_sum_amount(
    *,
    price_per_unit_text: str = "",
    quantity_text: str = "",
    line_sum_text: str = "",
) -> float | None:
    unit = parse_money_amount(price_per_unit_text)
    qty = parse_quantity(quantity_text)
    if unit is not None and qty is not None:
        return unit * qty
    return parse_money_amount(line_sum_text)


def compute_participation_economics(
    *,
    ktru_code: str,
    delivery_place: str = "",
    region: str = "",
    price_per_unit_text: str = "",
    quantity_text: str = "",
    line_sum_text: str = "",
    catalog: PriceOrdCatalog | None = None,
) -> dict[str, Any]:
    unavailable = {
        "available": False,
        "cp": None,
        "cpk": None,
        "zc": None,
        "nr": None,
        "or_amount": None,
        "profitability_pct": None,
        "profitability_text": "н/д",
        "condition": None,
        "profitability_class": profitability_css_class(None),
        "unit_price_for_draft": None,
        "error_reason": "",
    }
    cat = catalog if catalog is not None else get_price_ord_catalog()
    row = cat.resolve_row(ktru_code, delivery_place=delivery_place, region=region)
    if row is None:
        message = economics_unavailable_message("no_price_ord_row")
        return {
            **unavailable,
            "error_reason": "no_price_ord_row",
            "unavailable_message": message,
            "profitability_text": message,
        }

    s_amount = position_sum_amount(
        price_per_unit_text=price_per_unit_text,
        quantity_text=quantity_text,
        line_sum_text=line_sum_text,
    )
    qty = parse_quantity(quantity_text)
    if s_amount is None:
        return {**unavailable, "error_reason": "no_position_sum"}
    if qty is None:
        return {**unavailable, "error_reason": "no_quantity"}

    cp = s_amount * _CP_DISCOUNT
    zc = cp / (1.0 + row.markup_pct / 100.0)
    nr = zc * (row.overhead_pct / 100.0)
    or_amount = cp - zc - nr
    profitability_pct = (or_amount / cp * 100.0) if cp else 0.0
    condition = profitability_condition(profitability_pct)
    if condition == 1:
        cpk = cp * _CP_DISCOUNT
    else:
        cpk = cp
    unit_price = cpk / qty

    return {
        "available": True,
        "cp": format_money_ru(cp),
        "cpk": format_money_ru(cpk),
        "zc": format_money_ru(zc),
        "nr": format_money_ru(nr),
        "or_amount": format_money_ru(or_amount),
        "profitability_pct": round(profitability_pct, 4),
        "profitability_text": format_percent_ru(profitability_pct) + "%",
        "condition": condition,
        "profitability_class": profitability_css_class(condition),
        "unit_price_for_draft": format_rts_unit_price_from_float(unit_price),
        "error_reason": "",
    }


def format_rts_unit_price_from_float(value: float) -> str:
    return format_money_ru(value, decimals=2)

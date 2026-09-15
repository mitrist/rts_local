"""Коды orgRegion для getDocsByOrgRegion (субъекты РФ, 2 цифры)."""

from __future__ import annotations

# Субъекты РФ в ЕИС (КЛАДР/ОКАТО, без отдельных кодов упразднённых после 2020).
ALL_EIS_ORG_REGIONS: tuple[str, ...] = (
    "01",
    "02",
    "03",
    "04",
    "05",
    "06",
    "07",
    "08",
    "09",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
    "16",
    "17",
    "18",
    "19",
    "20",
    "21",
    "22",
    "23",
    "24",
    "25",
    "26",
    "27",
    "28",
    "29",
    "30",
    "31",
    "32",
    "33",
    "34",
    "35",
    "36",
    "37",
    "38",
    "39",
    "40",
    "41",
    "42",
    "43",
    "44",
    "45",
    "46",
    "47",
    "48",
    "49",
    "50",
    "51",
    "52",
    "53",
    "54",
    "55",
    "56",
    "57",
    "58",
    "59",
    "60",
    "61",
    "62",
    "63",
    "64",
    "65",
    "66",
    "67",
    "68",
    "69",
    "70",
    "71",
    "72",
    "73",
    "74",
    "75",
    "76",
    "77",
    "78",
    "79",
    "83",
    "86",
    "87",
    "89",
    "91",
    "92",
    "99",
)


def all_eis_org_regions() -> list[str]:
    return list(ALL_EIS_ORG_REGIONS)


def resolve_org_regions(raw: str | None) -> list[str]:
    """Пустой raw — все субъекты РФ; иначе список из CSV."""
    text = (raw or "").strip()
    if not text:
        return all_eis_org_regions()
    seen: set[str] = set()
    out: list[str] = []
    for part in text.split(","):
        item = part.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    if not out:
        raise ValueError("Список регионов пуст.")
    return out

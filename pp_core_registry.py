"""Match competitor registry finds to demand-core variants (prices, frequent regions)."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from eis_analytics_db import list_pp_core_variants, list_pp_monitor_findings_by_ktru
from participation_price import parse_money_amount, subject_candidates_from_oktmo
from pp_core_catalog import (
    demand_key,
    empty_price_stats,
    find_selection_pairs,
    locked_code_set,
    price_ladder_from_prices,
    resolve_ktru_template,
)
from rts_oktmo_catalog import load_subject_oktmo

BIG_REGION_MIN = 41
FREQUENT_REGION_LIMIT = 40


def _float_price(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    parsed = parse_money_amount(raw)
    return parsed if parsed is not None and parsed > 0 else None


def unit_price_from_finding(row: dict[str, Any]) -> float | None:
    price = _float_price(row.get("unit_price"))
    if price is not None:
        return price
    find_item = row.get("find") if isinstance(row.get("find"), dict) else {}
    places = find_item.get("deliveryPlaces")
    if not isinstance(places, list) or not places:
        return None
    first = places[0]
    if not isinstance(first, dict):
        return None
    return _float_price(first.get("unitPrice") or first.get("unit_price"))


def _catalog_code_index(catalog_codes: set[str]) -> dict[str, str]:
    """Map raw and zero-stripped OKTMO strings to the catalog subject code."""
    idx: dict[str, str] = {}
    for code in catalog_codes:
        idx[code] = code
        digits = "".join(ch for ch in code if ch.isdigit())
        if not digits:
            continue
        idx[digits] = code
        try:
            idx[str(int(digits))] = code
        except ValueError:
            continue
    return idx


def subject_codes_from_find(find_item: dict[str, Any] | None, catalog_codes: set[str]) -> set[str]:
    if not isinstance(find_item, dict) or not catalog_codes:
        return set()
    places = find_item.get("deliveryPlaces")
    if not isinstance(places, list):
        return set()
    idx = _catalog_code_index(catalog_codes)
    out: set[str] = set()
    for place in places:
        if not isinstance(place, dict):
            continue
        oktmo = place.get("oktmo")
        raw = ""
        if isinstance(oktmo, dict):
            raw = str(oktmo.get("code") or "").strip()
        elif isinstance(oktmo, str):
            raw = oktmo.strip()
        if not raw:
            continue
        hit = idx.get(raw)
        if hit:
            out.add(hit)
            continue
        for cand in subject_candidates_from_oktmo(raw):
            mapped = idx.get(cand)
            if mapped:
                out.add(mapped)
                break
    return out


def price_ladders_for_variants(
    analytics_db: Path,
    ktru_code: str,
    variants: list[dict[str, Any]],
    template: dict[str, Any] | None,
) -> dict[int, dict[str, Any]]:
    empty = empty_price_stats()
    out: dict[int, dict[str, Any]] = {}
    for row in variants:
        try:
            num = int(row.get("variant_no") or 0)
        except (TypeError, ValueError):
            continue
        if num > 0:
            out[num] = dict(empty)
    if not template:
        return out
    locked = locked_code_set(template)
    groups: dict[frozenset[tuple[str, str]], list[float]] = defaultdict(list)
    for finding in list_pp_monitor_findings_by_ktru(analytics_db, ktru_code):
        find_item = finding.get("find") if isinstance(finding.get("find"), dict) else {}
        key = demand_key(find_selection_pairs(find_item, template), locked=locked)
        if not key:
            continue
        price = unit_price_from_finding(finding)
        if price is None:
            continue
        groups[key].append(price)
    for row in variants:
        try:
            num = int(row.get("variant_no") or 0)
        except (TypeError, ValueError):
            continue
        if num < 1:
            continue
        key = demand_key(row.get("characteristics"), locked=locked)
        meta = row.get("core_meta") if isinstance(row.get("core_meta"), dict) else {}
        nmck = meta.get("nmck_prices") if isinstance(meta.get("nmck_prices"), list) else []
        nmck_prices = []
        for item in nmck:
            parsed = _float_price(item)
            if parsed is not None:
                nmck_prices.append(parsed)
        if nmck_prices:
            out[num] = price_ladder_from_prices(nmck_prices)
            continue
        prices = list(groups.get(key, []))
        if not prices and key:
            for pp_key, pp_prices in groups.items():
                if key <= pp_key:
                    prices.extend(pp_prices)
        out[num] = price_ladder_from_prices(prices)
    return out


def frequent_subject_regions(
    analytics_db: Path,
    ktru_code: str,
    *,
    min_regions: int = BIG_REGION_MIN,
    limit: int = FREQUENT_REGION_LIMIT,
) -> dict[str, Any]:
    catalog = load_subject_oktmo()
    by_code = {row.code: row for row in catalog}
    catalog_codes = set(by_code)
    findings = list_pp_monitor_findings_by_ktru(analytics_db, ktru_code)
    big: list[dict[str, Any]] = []
    for row in findings:
        try:
            regions_n = int(row.get("delivery_regions_count") or 0)
        except (TypeError, ValueError):
            regions_n = 0
        if regions_n < min_regions:
            continue
        find_item = row.get("find") if isinstance(row.get("find"), dict) else {}
        codes = subject_codes_from_find(find_item, catalog_codes)
        if codes:
            big.append({"codes": codes})
    if not big:
        variants = list_pp_core_variants(analytics_db, ktru_code)
        for row in variants:
            meta = row.get("core_meta") if isinstance(row.get("core_meta"), dict) else {}
            items = meta.get("nmck_frequent_regions")
            if isinstance(items, list) and items:
                return {
                    "ok": True,
                    "ktru_code": str(ktru_code or "").strip(),
                    "big_pp_n": int(meta.get("used") or meta.get("notices") or 0),
                    "selected_n": len(items),
                    "items": [dict(it) for it in items if isinstance(it, dict)],
                    "source": str(meta.get("source") or "nmck"),
                }
            break
    total = len(big)
    counts: Counter[str] = Counter()
    for row in big:
        counts.update(row["codes"])
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    take = max(0, int(limit))
    items = []
    for code, n in ranked[:take]:
        subj = by_code.get(code)
        items.append(
            {
                "code": code,
                "name": subj.name if subj else code,
                "pp_n": n,
                "share": round(n / total, 4) if total else 0.0,
            }
        )
    return {
        "ok": True,
        "ktru_code": str(ktru_code or "").strip(),
        "big_pp_n": total,
        "selected_n": len(items),
        "items": items,
    }


def resolve_template_for_core(
    ktru_code: str,
    *,
    cache_dir: Path | None = None,
    template: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return resolve_ktru_template(ktru_code, cache_dir=cache_dir, template=template)

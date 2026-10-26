"""Demand-core + price ladder from RTS NMCK justification contract dumps."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from pp_core_catalog import (
    CoreSnapshotError,
    _demand_map_mod,
    _norm,
    persist_core_snapshot,
    resolve_ktru_template,
)
from rts_oktmo_catalog import OktmoSubject, load_subject_oktmo
from rts_pp_monitor import iter_json_objects

STUDENT_CHAIR_KTRU_CODE = "31.01.11.150-00000021"
_NAME_TRAIL_RE = re.compile(r"[:\d]+$")
_WS_RE = re.compile(r"\s+")
_REGION_NOISE_RE = re.compile(
    r"\b(муниципальные образования|города федерального значения|"
    r"столицы российской федерации|город федерального значения|"
    r"город|города|область|области|край|края|республика|республики|респ)\b",
    re.IGNORECASE,
)


class NmckImportError(ValueError):
    """Cannot parse or import an NMCK contract dump."""


def _fold_char_name(name: str) -> str:
    text = _WS_RE.sub(" ", str(name or "")).strip()
    return _NAME_TRAIL_RE.sub("", text).strip()


def _template_names(template: dict[str, Any] | None) -> set[str]:
    if not isinstance(template, dict):
        return set()
    out: set[str] = set()
    for ch in template.get("characteristics") or []:
        if not isinstance(ch, dict) or ch.get("isActual") is False:
            continue
        name = _norm(ch.get("name"))
        if name:
            out.add(name)
    return out


def _combo_hash(pairs: list[tuple[str, str]]) -> str:
    blob = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def iter_nmck_contracts(text: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for blob in iter_json_objects(text):
        items: list[Any]
        if isinstance(blob, dict) and isinstance(blob.get("contracts"), list):
            items = blob["contracts"]
        elif isinstance(blob, list):
            items = blob
        else:
            continue
        for row in items:
            if not isinstance(row, dict):
                continue
            uid = str(row.get("uid") or row.get("number") or "").strip()
            if uid and uid in seen:
                continue
            if uid:
                seen.add(uid)
            out.append(row)
    return out


def _contract_chars(tru: dict[str, Any], template_names: set[str]) -> list[dict[str, str]]:
    chars: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in tru.get("characteristics") or []:
        if not isinstance(raw, dict):
            continue
        name = _fold_char_name(str(raw.get("name") or ""))
        value = str(raw.get("value") or "").strip()
        if not name or not value:
            continue
        if template_names and _norm(name) not in template_names:
            continue
        key = (_norm(name), _norm(value))
        if key in seen:
            continue
        seen.add(key)
        chars.append({"display_label": name, "value_display": value})
    return chars


def _unit_price(tru: dict[str, Any]) -> float | None:
    raw = tru.get("price")
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value > 0 else None
    return None


def build_nmck_combos(
    contracts: list[dict[str, Any]],
    *,
    ktru_code: str,
    template: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    wanted = str(ktru_code or "").strip()
    names = _template_names(template)
    by_hash: dict[str, dict[str, Any]] = {}
    skipped_empty = 0
    skipped_ktru = 0
    used = 0
    region_counts: Counter[str] = Counter()
    dates: list[str] = []
    for row in contracts:
        tru = row.get("tru") if isinstance(row.get("tru"), dict) else {}
        code = str(tru.get("ktruCode") or tru.get("ktru_code") or "").strip()
        if wanted and code != wanted:
            skipped_ktru += 1
            continue
        chars = _contract_chars(tru, names)
        if not chars:
            skipped_empty += 1
            continue
        pairs = sorted(
            (str(ch.get("display_label") or ""), str(ch.get("value_display") or "")) for ch in chars
        )
        hid = _combo_hash(pairs)
        item = by_hash.setdefault(
            hid,
            {
                "hash": hid,
                "n": 0,
                "chars": chars,
                "preview": "; ".join(f"{a}={b}" for a, b in pairs if a and b) or "—",
                "prices": [],
                "regions": Counter(),
            },
        )
        item["n"] += 1
        used += 1
        price = _unit_price(tru)
        if price is not None:
            item["prices"].append(price)
        region = str(row.get("regionName") or "").strip()
        if region:
            item["regions"][region] += 1
            region_counts[region] += 1
        for key in ("contractSignDate", "contractPublishDate"):
            dt = str(row.get(key) or "")[:10]
            if dt:
                dates.append(dt)
                break
    combos = sorted(by_hash.values(), key=lambda c: (-int(c["n"]), str(c["hash"])))
    period = (min(dates), max(dates)) if dates else ("", "")
    stats = {
        "contracts": used + skipped_empty + skipped_ktru,
        "used": used,
        "skipped_empty": skipped_empty,
        "skipped_ktru": skipped_ktru,
        "combos": len(combos),
        "period": period,
        "region_counts": region_counts,
    }
    return combos, stats


def _region_core(name: str) -> str:
    text = _REGION_NOISE_RE.sub(" ", _norm(name))
    return " ".join(text.split())


def _region_stem(name: str) -> str:
    core = _region_core(name)
    for suf in ("ского", "ская", "ской", "ский", "ское", "ские", "ого", "ому", "ая", "ой", "ий", "ое", "ый"):
        if len(core) > len(suf) + 4 and core.endswith(suf):
            return core[: -len(suf)]
    return core


def _region_similar(left: str, right: str) -> bool:
    a = _region_stem(left)
    b = _region_stem(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    n = min(len(a), len(b))
    return n >= 6 and a[: n - 1] == b[: n - 1]


def match_region_to_subject(region_name: str, catalog: list[OktmoSubject]) -> OktmoSubject | None:
    raw = str(region_name or "").strip()
    if not raw or not catalog:
        return None
    hits: list[OktmoSubject] = []
    for subj in catalog:
        if _region_similar(raw, subj.name):
            hits.append(subj)
    if not hits:
        return None
    hits.sort(key=lambda s: (abs(len(_region_stem(s.name)) - len(_region_stem(raw))), len(s.name)))
    return hits[0]


def frequent_regions_from_counts(
    counts: Counter[str],
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    catalog = list(load_subject_oktmo())
    ranked: list[tuple[OktmoSubject, int]] = []
    seen: set[str] = set()
    for name, n in counts.most_common():
        subj = match_region_to_subject(name, catalog)
        if subj is None or subj.code in seen:
            continue
        seen.add(subj.code)
        ranked.append((subj, int(n)))
    total = sum(n for _s, n in ranked) or 1
    take = max(0, int(limit))
    items: list[dict[str, Any]] = []
    for subj, n in ranked[:take]:
        items.append(
            {
                "code": subj.code,
                "name": subj.name,
                "pp_n": n,
                "share": round(n / total, 4),
            }
        )
    return items


def import_nmck_core(
    analytics_db: Path,
    text: str,
    *,
    ktru_code: str,
    template: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    if not code:
        raise NmckImportError("Укажите код КТРУ.")
    contracts = iter_nmck_contracts(text)
    if not contracts:
        raise NmckImportError("В файле нет контрактов НМЦК.")
    tmpl = resolve_ktru_template(code, cache_dir=cache_dir, template=template)
    if tmpl is None:
        raise CoreSnapshotError(f"Нет шаблона РТС для КТРУ {code}.")
    combos, stats = build_nmck_combos(contracts, ktru_code=code, template=tmpl)
    if not combos:
        raise NmckImportError(f"Нет контрактов с характеристиками шаблона РТС по КТРУ {code}.")
    demand = _demand_map_mod()
    core, total, share = demand.select_core(combos)
    regions = frequent_regions_from_counts(stats["region_counts"])
    meta = {
        "ktru": code,
        "source": "nmck",
        "period": list(stats["period"]),
        "notices": total,
        "contracts": stats["contracts"],
        "used": stats["used"],
        "skipped_empty": stats["skipped_empty"],
        "core_n": len(core),
        "core_share": round(share, 4),
        "core_target": demand.CORE_TARGET,
        "core_max": demand.CORE_MAX,
        "has_rts_template": True,
        "nmck_frequent_regions": regions,
    }
    variants = persist_core_snapshot(
        analytics_db,
        code,
        core,
        tmpl,
        total_notices=total,
        core_meta=meta,
    )
    return {"ok": True, "ktru_code": code, "meta": meta, "variants": variants, "stats": {
        "contracts": stats["contracts"],
        "used": stats["used"],
        "skipped_empty": stats["skipped_empty"],
        "combos": stats["combos"],
        "core_n": len(core),
        "core_share": round(share, 4),
        "regions_n": len(regions),
    }}

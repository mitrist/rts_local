#!/usr/bin/env python3
"""Demand map from eis_history for one KTRU. Reproducible CLI."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eis_history_db import history_connect  # noqa: E402
from participation_price import parse_money_amount  # noqa: E402
from rts_ktru_cache import load_cached_template  # noqa: E402

INN_RE = re.compile(r"ИНН\s+(\d{10,12})")
CORE_TARGET = 0.70
CORE_MAX = 0.80
MID_EXTRA = 0.20


def _norm(s: str) -> str:
    return " ".join(str(s or "").strip().lower().replace("ё", "е").split())


def _money(text: str) -> float | None:
    return parse_money_amount(text)


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.median(vals))


def _mean(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _preview(chars: list[dict[str, Any]], *, limit: int = 8) -> str:
    parts: list[str] = []
    for ch in chars:
        if not isinstance(ch, dict):
            continue
        lab = str(ch.get("display_label") or ch.get("match_key") or ch.get("characteristic_code") or "").strip()
        val = str(ch.get("value_display") or "").strip()
        if lab and val:
            parts.append(f"{lab}={val}")
        elif val:
            parts.append(val)
        if len(parts) >= limit:
            break
    extra = len(chars) - limit if len(chars) > limit else 0
    text = "; ".join(parts) if parts else "—"
    if extra > 0:
        text += f"; +{extra}"
    return text


def _inn(customer_text: str) -> str:
    m = INN_RE.search(str(customer_text or ""))
    return m.group(1) if m else ""


def _name(customer_text: str) -> str:
    first = str(customer_text or "").splitlines()[0].strip() if customer_text else ""
    return first[:80]


def load_rows(db: Path, ktru: str) -> list[dict[str, Any]]:
    with history_connect(db) as conn:
        rows = conn.execute(
            """
            SELECT
              l.line_key,
              l.purchase_number,
              l.ktru_code,
              l.position_display_name,
              l.price_per_unit_text,
              l.line_sum_text,
              l.notice_total_text,
              l.customer_text,
              l.characteristics_count,
              l.characteristics_hash,
              l.characteristics_json,
              n.org_region,
              COALESCE(NULLIF(n.publish_dt_utc, ''), n.detected_at_utc) AS observed_at_utc
            FROM ktru_lines l
            JOIN notice_events n ON n.purchase_key = l.purchase_key
            WHERE l.ktru_code = ?
            """,
            (ktru,),
        ).fetchall()
    return [dict(r) for r in rows]


def build_combos(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_hash: dict[str, dict[str, Any]] = {}
    for row in rows:
        h = str(row.get("characteristics_hash") or "")
        pn = str(row.get("purchase_number") or "").strip()
        if not h or not pn:
            continue
        item = by_hash.setdefault(
            h,
            {
                "hash": h,
                "purchases": {},
                "chars_json": str(row.get("characteristics_json") or "[]"),
                "chars_count": int(row.get("characteristics_count") or 0),
                "name": str(row.get("position_display_name") or ""),
            },
        )
        if int(row.get("characteristics_count") or 0) > item["chars_count"]:
            item["chars_count"] = int(row.get("characteristics_count") or 0)
            item["chars_json"] = str(row.get("characteristics_json") or "[]")
        purch = item["purchases"].setdefault(
            pn,
            {
                "nmck": None,
                "unit": None,
                "line_sum": None,
                "inn": "",
                "customer": "",
                "region": "",
                "date": "",
            },
        )
        nmck = _money(str(row.get("notice_total_text") or ""))
        if nmck is not None:
            purch["nmck"] = nmck
        unit = _money(str(row.get("price_per_unit_text") or ""))
        if unit is not None:
            purch["unit"] = unit
        line_sum = _money(str(row.get("line_sum_text") or ""))
        if line_sum is not None:
            purch["line_sum"] = line_sum
        inn = _inn(str(row.get("customer_text") or ""))
        if inn:
            purch["inn"] = inn
        name = _name(str(row.get("customer_text") or ""))
        if name and not purch["customer"]:
            purch["customer"] = name
        reg = str(row.get("org_region") or "").strip()
        if reg:
            purch["region"] = reg
        dt = str(row.get("observed_at_utc") or "")
        if dt > str(purch.get("date") or ""):
            purch["date"] = dt

    combos: list[dict[str, Any]] = []
    for h, item in by_hash.items():
        purchases = list(item["purchases"].values())
        n = len(item["purchases"])
        nmcks = [p["nmck"] for p in purchases if p["nmck"] is not None]
        units = [p["unit"] for p in purchases if p["unit"] is not None]
        inns = {p["inn"] for p in purchases if p["inn"]}
        regions = Counter(p["region"] for p in purchases if p["region"])
        try:
            chars = json.loads(item["chars_json"])
        except json.JSONDecodeError:
            chars = []
        if not isinstance(chars, list):
            chars = []
        qty_est: list[float] = []
        for p in purchases:
            if p["line_sum"] and p["unit"] and p["unit"] > 0:
                qty_est.append(p["line_sum"] / p["unit"])
        combos.append(
            {
                "hash": h,
                "n": n,
                "nmck_mean": _mean(nmcks),
                "nmck_median": _median(nmcks),
                "nmck_min": min(nmcks) if nmcks else None,
                "nmck_max": max(nmcks) if nmcks else None,
                "unit_mean": _mean(units),
                "customers": len(inns),
                "inns": sorted(inns),
                "customer_names": sorted({p["customer"] for p in purchases if p["customer"]})[:8],
                "regions": [{"code": c, "n": n} for c, n in regions.most_common(8)],
                "qty_mean": _mean(qty_est),
                "chars": chars,
                "preview": _preview(chars),
                "nmck_n": len(nmcks),
            }
        )
    combos.sort(key=lambda x: (-int(x["n"]), str(x["hash"])))
    return combos


def select_core(
    combos: list[dict[str, Any]],
    *,
    target: float = CORE_TARGET,
    core_max: float = CORE_MAX,
) -> tuple[list[dict[str, Any]], int, float]:
    """Greedy core: add combos until coverage would exceed core_max after target."""
    total = sum(int(c["n"]) for c in combos)
    acc = 0
    core: list[dict[str, Any]] = []
    for c in combos:
        prev = acc / total if total else 0
        acc += int(c["n"])
        if prev < target and (acc / total) <= core_max + 0.01:
            core.append(c)
            continue
        break
    core_n = sum(int(c["n"]) for c in core)
    share = core_n / total if total else 0.0
    return core, total, share


def slim_combo(c: dict[str, Any], *, include_inns: bool = False) -> dict[str, Any]:
    out = {
        "hash": c["hash"],
        "n": c["n"],
        "nmck_mean": c["nmck_mean"],
        "nmck_median": c["nmck_median"],
        "nmck_min": c["nmck_min"],
        "nmck_max": c["nmck_max"],
        "unit_mean": c["unit_mean"],
        "customers": c["customers"],
        "customer_names": c["customer_names"],
        "regions": c["regions"],
        "qty_mean": c["qty_mean"],
        "preview": c["preview"],
        "nmck_n": c["nmck_n"],
        "char_count": len(c.get("chars") or []),
    }
    if include_inns:
        out["inns"] = c.get("inns") or []
    return out


def variability(core: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # frequency of char label -> value among core notices
    label_values: dict[str, Counter[str]] = defaultdict(Counter)
    label_notices: dict[str, int] = defaultdict(int)
    total_core = sum(int(c["n"]) for c in core) or 1
    for c in core:
        seen_labels: set[str] = set()
        for ch in c["chars"]:
            if not isinstance(ch, dict):
                continue
            lab = str(ch.get("display_label") or ch.get("match_key") or "").strip()
            val = str(ch.get("value_display") or "").strip()
            if not lab:
                continue
            label_values[lab][val or "—"] += int(c["n"])
            if lab not in seen_labels:
                label_notices[lab] += int(c["n"])
                seen_labels.add(lab)
    out: list[dict[str, Any]] = []
    for lab, ctr in sorted(label_values.items(), key=lambda kv: -sum(kv[1].values())):
        nvals = len([v for v in ctr if v and v != "—"])
        top = ctr.most_common(6)
        mode_n = top[0][1] if top else 0
        out.append(
            {
                "label": lab,
                "kind": "фиксирована" if nvals <= 1 else "варьируется",
                "distinct": nvals,
                "coverage_pct": round(100.0 * label_notices[lab] / total_core, 1),
                "values": [{"value": v, "n": n, "pct": round(100.0 * n / total_core, 1)} for v, n in top],
                "mode_pct": round(100.0 * mode_n / total_core, 1),
            }
        )
    return out


def rts_map(core: list[dict[str, Any]], template: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not template:
        return []
    by_code = {}
    by_name = {}
    for ch in template.get("characteristics") or []:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or "").strip()
        name = _norm(str(ch.get("name") or ""))
        if code:
            by_code[code] = ch
        if name:
            by_name[name] = ch
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for c in core:
        for ch in c["chars"]:
            if not isinstance(ch, dict):
                continue
            lab = str(ch.get("display_label") or "").strip()
            code = str(ch.get("characteristic_code") or "").strip()
            val = str(ch.get("value_display") or "").strip()
            key = (lab or code, val)
            if key in seen or not (lab or code):
                continue
            seen.add(key)
            tmpl = by_code.get(code) if code else None
            if tmpl is None:
                tmpl = by_name.get(_norm(lab))
            if tmpl is None:
                rows.append(
                    {
                        "eis_char": lab or code,
                        "eis_value": val,
                        "rts_field": "",
                        "rts_value": "",
                        "transform": "нет поля",
                        "status": "разрыв",
                    }
                )
                continue
            options = [cv for cv in (tmpl.get("characteristicValues") or []) if isinstance(cv, dict)]
            nv = _norm(val)
            hit = None
            for opt in options:
                qd = str(opt.get("qualityDescription") or "").strip()
                conc = str(opt.get("concreteValue") or "").strip()
                if nv and (_norm(qd) == nv or _norm(conc) == nv):
                    hit = opt
                    break
            if hit is None and nv:
                for opt in options:
                    qd = _norm(str(opt.get("qualityDescription") or ""))
                    if qd and (nv in qd or qd in nv):
                        hit = opt
                        break
            rts_name = str(tmpl.get("name") or "")
            if hit:
                rts_val = str(hit.get("qualityDescription") or hit.get("concreteValue") or hit.get("id") or "")
                exact = _norm(rts_val) == nv
                rows.append(
                    {
                        "eis_char": lab or code,
                        "eis_value": val,
                        "rts_field": f"{tmpl.get('code')}|{rts_name}",
                        "rts_value": rts_val,
                        "transform": "exact" if exact else "substring",
                        "status": "готово" if exact else "требует валидации",
                    }
                )
            elif not options:
                rows.append(
                    {
                        "eis_char": lab or code,
                        "eis_value": val,
                        "rts_field": f"{tmpl.get('code')}|{rts_name}",
                        "rts_value": val,
                        "transform": "free-form",
                        "status": "требует валидации",
                    }
                )
            else:
                rows.append(
                    {
                        "eis_char": lab or code,
                        "eis_value": val,
                        "rts_field": f"{tmpl.get('code')}|{rts_name}",
                        "rts_value": "",
                        "transform": "нет значения",
                        "status": "разрыв",
                    }
                )
    return rows


def fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v - round(v)) < 0.005:
        return f"{int(round(v)):,}".replace(",", " ")
    return f"{v:,.2f}".replace(",", " ")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--ktru", required=True)
    p.add_argument("--cache-dir", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--matrix-limit", type=int, default=40)
    args = p.parse_args()
    rows = load_rows(args.db, args.ktru)
    if not rows:
        print(json.dumps({"ok": False, "error": "no_rows"}, ensure_ascii=False))
        return 2
    combos = build_combos(rows)
    core, total, core_share = select_core(combos)
    acc = 0
    mid: list[dict[str, Any]] = []
    tail: list[dict[str, Any]] = []
    core_hashes = {str(c.get("hash") or "") for c in core}
    for c in combos:
        if str(c.get("hash") or "") in core_hashes:
            acc += int(c["n"])
            continue
        prev = acc / total if total else 0
        acc += int(c["n"])
        if prev < CORE_TARGET + MID_EXTRA:
            mid.append(c)
            continue
        tail.append(c)
    core_n = sum(int(c["n"]) for c in core)
    var = variability(core)
    cache_dir = args.cache_dir
    tmpl = load_cached_template(cache_dir, args.ktru, ttl_sec=0) if cache_dir else None
    mapping = rts_map(core, tmpl) if tmpl else None
    dates = [str(r.get("observed_at_utc") or "") for r in rows if r.get("observed_at_utc")]
    period = (min(dates)[:10], max(dates)[:10]) if dates else ("", "")
    payload = {
        "ktru": args.ktru,
        "period": period,
        "notices": total,
        "lines": len(rows),
        "unique_combos": len(combos),
        "core_n": len(core),
        "core_share": round(core_share, 4),
        "mid_n": len(mid),
        "tail_n": len(tail),
        "nmck_missing": sum(1 for c in combos if c["nmck_n"] == 0),
        "has_rts_template": bool(tmpl),
        "has_oktmo": False,
        "has_delivery_days": False,
        "has_quantity": False,
        "has_schedules": False,
        "core": [slim_combo(c, include_inns=True) for c in core],
        "mid_count": len(mid),
        "tail_count": len(tail),
        "variability": var,
        "mapping": mapping,
        "mapping_counts": None
        if mapping is None
        else {
            "rows": len(mapping),
            "ready": sum(1 for r in mapping if r["status"] == "готово"),
            "validate": sum(1 for r in mapping if r["status"] == "требует валидации"),
            "gap": sum(1 for r in mapping if r["status"] == "разрыв"),
        },
        "matrix": [slim_combo(c) for c in combos[: max(1, int(args.matrix_limit))]],
        "matrix_truncated": len(combos) > int(args.matrix_limit),
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Compare EIS HTML notice characteristics with RTS creation-data snapshot."""

from __future__ import annotations

import re
from typing import Any

from characteristic_aliases import label_alias_keys, labels_are_aliases
from notice_to_rts_pp import (
    ApplyResult,
    _norm_label,
    build_notice_ktru_characteristics,
    find_template_characteristic,
    group_notice_characteristics,
    render_characteristic_value,
)

_WS_RE = re.compile(r"\s+")


def _norm_value_token(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip()).lower().replace("ё", "е")


def _value_tokens(values: list[str]) -> set[str]:
    return {_norm_value_token(v) for v in values if _norm_value_token(v)}


def _render_creation_value(cv: dict[str, Any]) -> str:
    """Render creation-data characteristic value (may use dictionaryId field names)."""
    if not isinstance(cv, dict):
        return ""
    # creation-data uses ktruCharacteristicValues with qualityDescription / min/max / concreteValue
    text = render_characteristic_value(cv, include_id=False)
    if text:
        return text
    for key in ("qualityDescription", "concreteValue"):
        raw = str(cv.get(key) or "").strip()
        if raw:
            return raw
    return ""


def normalize_creation_characteristics(creation_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten creation-data ktruCharacteristics to comparable rows."""
    raw = creation_data.get("ktruCharacteristics") if isinstance(creation_data, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for ch in raw:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or "").strip()
        name = str(ch.get("name") or "").strip()
        values_raw = ch.get("ktruCharacteristicValues")
        if not isinstance(values_raw, list):
            values_raw = ch.get("characteristicValues") or []
        values: list[str] = []
        ids: list[Any] = []
        if isinstance(values_raw, list):
            for cv in values_raw:
                if not isinstance(cv, dict):
                    continue
                rendered = _render_creation_value(cv)
                if rendered:
                    values.append(rendered)
                vid = cv.get("dictionaryId")
                if vid is None:
                    vid = cv.get("id")
                if vid is not None:
                    ids.append(vid)
        required = ch.get("isRequired")
        out.append(
            {
                "code": code,
                "name": name,
                "values": values,
                "value_ids": ids,
                "is_required": bool(required) if required is not None else False,
                "dictionary_id": ch.get("dictionaryId") or ch.get("id"),
            }
        )
    return out


def _index_creation_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_code: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("code") or "").strip()
        name_key = _norm_label(str(row.get("name") or ""))
        if code:
            by_code[code] = row
        if name_key:
            by_name[name_key] = row
    return by_code, by_name


def _find_creation_row(
    *,
    html_code: str,
    html_label: str,
    by_code: dict[str, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Return (row, match_kind) where match_kind is code|name|alias|none."""
    code = str(html_code or "").strip()
    if code and code in by_code:
        return by_code[code], "code"
    label_key = _norm_label(html_label)
    if label_key and label_key in by_name:
        return by_name[label_key], "name"
    if label_key:
        alias_keys = label_alias_keys(html_label)
        for name_key, row in by_name.items():
            if name_key in alias_keys or labels_are_aliases(html_label, str(row.get("name") or "")):
                return row, "alias"
    return None, "none"


def _values_equal(html_values: list[str], rts_values: list[str]) -> bool:
    left = _value_tokens(html_values)
    right = _value_tokens(rts_values)
    if not left and not right:
        return True
    if left == right:
        return True
    # subset either way often OK for multi-select / display truncation
    if left and right and (left <= right or right <= left):
        return True
    return False


def compare_html_to_creation_data(
    html_chars: list[dict[str, Any]],
    creation_data: dict[str, Any],
    *,
    ktru_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare HTML notice chars with RTS creation-data.

    Returns ``{rows, summary}`` where each row has status/create_risk from the plan taxonomy.
    """
    creation_rows = normalize_creation_characteristics(creation_data)
    by_code, by_name = _index_creation_rows(creation_rows)
    used_creation: set[str] = set()
    rows: list[dict[str, Any]] = []

    # Simulate create-from-HTML risks when template is available.
    apply: ApplyResult | None = None
    unmatched_notice_keys: set[str] = set()
    value_unmapped_codes: set[str] = set()
    unfilled_required_names: set[str] = set()
    if ktru_item:
        _mapped, apply = build_notice_ktru_characteristics(ktru_item, html_chars)
        for item in apply.unmatched_notice:
            label = str(item.get("label") or "").strip()
            code = str(item.get("code") or "").strip()
            if code:
                unmatched_notice_keys.add(f"code:{code}")
            if label:
                unmatched_notice_keys.add(f"name:{_norm_label(label)}")
        if apply.value_unmatched > 0:
            # Mark groups that failed value resolve via re-check against template.
            for group in group_notice_characteristics(html_chars):
                template = find_template_characteristic(ktru_item, group.as_notice_dict())
                if not template:
                    continue
                # Reuse build path counts indirectly: if template found but values empty after build.
                code = str(template.get("code") or "").strip()
                built = next(
                    (
                        ch
                        for ch in _mapped
                        if isinstance(ch, dict) and str(ch.get("code") or "").strip() == code
                    ),
                    None,
                )
                vals = (built or {}).get("characteristicValues") if isinstance(built, dict) else None
                has_id = any(
                    isinstance(v, dict) and v.get("id") is not None for v in (vals or [])
                )
                if group.values and not has_id:
                    if code:
                        value_unmapped_codes.add(code)
                    value_unmapped_codes.add(f"name:{_norm_label(group.display_label)}")
        unfilled_required_names = {_norm_label(x) for x in apply.unfilled_required}

    for group in group_notice_characteristics(html_chars):
        html_code = str(group.characteristic_code or "").strip()
        html_label = str(group.display_label or "").strip() or html_code or "—"
        html_values = list(group.values)
        rts_row, match_kind = _find_creation_row(
            html_code=html_code,
            html_label=html_label,
            by_code=by_code,
            by_name=by_name,
        )
        create_risk = ""
        risk_keys = set()
        if html_code:
            risk_keys.add(f"code:{html_code}")
        risk_keys.add(f"name:{_norm_label(html_label)}")
        if risk_keys & unmatched_notice_keys:
            create_risk = "create_risk_unmatched_notice"
        rts_code = ""
        rts_name = ""
        rts_values: list[str] = []
        if rts_row is None:
            status = "rts_missing"
            detail = "характеристика HTML отсутствует в creation-data"
            if not create_risk and ktru_item:
                create_risk = create_risk or "create_risk_rts_missing"
        else:
            rts_code = str(rts_row.get("code") or "").strip()
            rts_name = str(rts_row.get("name") or "").strip()
            rts_values = list(rts_row.get("values") or [])
            used_key = rts_code or f"name:{_norm_label(rts_name)}"
            used_creation.add(used_key)
            if match_kind == "code":
                if _values_equal(html_values, rts_values):
                    status = "ok"
                    detail = ""
                else:
                    status = "value_mismatch"
                    detail = "значения HTML и creation-data расходятся"
            elif match_kind in ("name", "alias"):
                if html_code and rts_code and html_code != rts_code:
                    status = "matched_by_name_only"
                    detail = f"code HTML≠RTS ({html_code} vs {rts_code})"
                elif _values_equal(html_values, rts_values):
                    status = "matched_by_name_only"
                    detail = f"join by {match_kind}"
                else:
                    status = "value_mismatch"
                    detail = f"join by {match_kind}, значения расходятся"
            else:
                status = "rts_missing"
                detail = "не сопоставлено"
            if rts_code in value_unmapped_codes or f"name:{_norm_label(html_label)}" in value_unmapped_codes:
                if status in ("ok", "matched_by_name_only"):
                    status = "value_unmapped"
                    detail = "HTML-значение не резолвится в dictionary option шаблона"
                create_risk = create_risk or "create_risk_value_unmapped"
            elif status == "value_mismatch":
                create_risk = create_risk or "create_risk_value_mismatch"

        rows.append(
            {
                "html_code": html_code,
                "html_label": html_label,
                "html_values": html_values,
                "rts_code": rts_code,
                "rts_name": rts_name,
                "rts_values": rts_values,
                "status": status,
                "detail": detail,
                "create_risk": create_risk,
                "match_kind": match_kind,
            }
        )

    for crow in creation_rows:
        code = str(crow.get("code") or "").strip()
        name = str(crow.get("name") or "").strip()
        key = code or f"name:{_norm_label(name)}"
        if key in used_creation:
            continue
        if code and code in used_creation:
            continue
        create_risk = ""
        status = "html_missing"
        detail = "есть в creation-data, нет в HTML"
        if crow.get("is_required"):
            status = "required_html_gap"
            detail = "обязательная характеристика creation-data отсутствует в HTML"
            create_risk = "create_risk_required_html_gap"
        elif _norm_label(name) in unfilled_required_names:
            status = "required_html_gap"
            detail = "обязательная по шаблону не заполнена из HTML"
            create_risk = "create_risk_required_html_gap"
        rows.append(
            {
                "html_code": "",
                "html_label": "",
                "html_values": [],
                "rts_code": code,
                "rts_name": name,
                "rts_values": list(crow.get("values") or []),
                "status": status,
                "detail": detail,
                "create_risk": create_risk,
                "match_kind": "none",
            }
        )

    summary = summarize_diff_rows(rows)
    if apply is not None:
        summary["apply"] = apply.as_dict()
    return {"rows": rows, "summary": summary}


def summarize_diff_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    create_risk_notices = 0
    risk_flags = 0
    for row in rows:
        st = str(row.get("status") or "ok")
        by_status[st] = by_status.get(st, 0) + 1
        if str(row.get("create_risk") or "").strip():
            risk_flags += 1
    if risk_flags:
        create_risk_notices = 1
    return {
        "total_rows": len(rows),
        "by_status": by_status,
        "create_risk_row_count": risk_flags,
        "has_create_risk": bool(create_risk_notices),
    }


def aggregate_pattern_summary(
    all_rows: list[dict[str, Any]],
    *,
    notices_with_risk: int,
    notices_total: int,
) -> list[dict[str, Any]]:
    """Build CSV-oriented pattern rows: status counts + samples."""
    buckets: dict[str, dict[str, Any]] = {}
    for row in all_rows:
        status = str(row.get("status") or "ok")
        b = buckets.setdefault(
            status,
            {
                "status": status,
                "count": 0,
                "sample_purchase": str(row.get("purchase_number") or ""),
                "sample_html_label": str(row.get("html_label") or row.get("rts_name") or ""),
                "sample_detail": str(row.get("detail") or ""),
            },
        )
        b["count"] += 1
        if not b["sample_purchase"] and row.get("purchase_number"):
            b["sample_purchase"] = str(row.get("purchase_number") or "")
            b["sample_html_label"] = str(row.get("html_label") or row.get("rts_name") or "")
            b["sample_detail"] = str(row.get("detail") or "")
    total = sum(int(b["count"]) for b in buckets.values()) or 1
    out: list[dict[str, Any]] = []
    for status, b in sorted(buckets.items(), key=lambda kv: (-int(kv[1]["count"]), kv[0])):
        out.append(
            {
                "status": status,
                "count": int(b["count"]),
                "share": round(int(b["count"]) / total, 4),
                "sample_purchase": b["sample_purchase"],
                "sample_html_label": b["sample_html_label"],
                "sample_detail": b["sample_detail"],
            }
        )
    out.append(
        {
            "status": "_notices_with_create_risk",
            "count": int(notices_with_risk),
            "share": round(notices_with_risk / max(1, notices_total), 4),
            "sample_purchase": "",
            "sample_html_label": "",
            "sample_detail": f"notices_total={notices_total}",
        }
    )
    return out

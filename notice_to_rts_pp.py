"""Map EIS notice characteristics to RTS preliminary offer draft fields."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from characteristic_aliases import label_alias_keys, labels_are_aliases
from rts_pp_api import (
    _characteristic_value_for_api,
    _fix_characteristic_value_ids,
    _map_create_characteristic,
    _map_characteristic_value,
    _numeric_option_contains,
    _numeric_option_exact_match,
    _resolve_value_option,
    _search_char_options,
    _set_characteristic_value,
)

_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
_RATIO_VALUE_RE = re.compile(r"\d+(?:[.,]\d+)?\s*:\s*\d+")
_NUMERIC_VALUE_HINT = re.compile(
    r"≥|≤|>=|<=|не\s+менее|не\s+более|greaterorequal|greater\s*or\s*equal|lessorequal|\bмин\s+\d|\bмакс\s+\d",
    re.IGNORECASE,
)
_PLACEHOLDER_LABEL = "(характеристика без имени)"


def _norm_label(value: str) -> str:
    return _WS_RE.sub(" ", (value or "").strip()).lower()


def _notice_code(notice_char: dict[str, Any]) -> str:
    code = str(notice_char.get("characteristic_code") or "").strip()
    if code:
        return code
    mk = str(notice_char.get("match_key") or "").strip()
    if mk and not mk.startswith("name:"):
        return mk
    return ""


def _notice_name_key(notice_char: dict[str, Any]) -> str:
    mk = str(notice_char.get("match_key") or "").strip()
    if mk.startswith("name:"):
        return mk[5:].strip()
    label = str(notice_char.get("display_label") or "").strip()
    return _norm_label(label)


@dataclass
class GroupedNoticeChar:
    display_label: str
    characteristic_code: str | None
    match_key: str
    values: list[str]

    def as_notice_dict(self) -> dict[str, Any]:
        return {
            "display_label": self.display_label,
            "characteristic_code": self.characteristic_code,
            "match_key": self.match_key,
            "value_display": self.values[0] if self.values else "",
        }


def _is_real_label(label: str) -> bool:
    text = (label or "").strip()
    return bool(text) and text != _PLACEHOLDER_LABEL


def _group_key(notice_char: dict[str, Any]) -> str:
    code = _notice_code(notice_char)
    if code:
        return f"code:{code}"
    label = _notice_name_key(notice_char)
    if _is_real_label(str(notice_char.get("display_label") or "")) and label:
        return f"name:{label}"
    mk = str(notice_char.get("match_key") or "").strip()
    if mk and mk != "name:":
        return mk
    return ""


def split_value_display(value_display: str) -> list[str]:
    """Split XML/HTML multi-value strings; keep quantitative values intact."""
    raw = _WS_RE.sub(" ", (value_display or "").strip())
    if not raw or raw == "—":
        return []
    if _NUMERIC_VALUE_HINT.search(raw):
        return [raw]
    parts: list[str] = []
    for chunk in raw.split(" | "):
        for piece in chunk.split(";"):
            part = piece.strip()
            if part and part not in parts:
                parts.append(part)
    return parts or [raw]


def group_notice_characteristics(notice_chars: list[dict[str, Any]]) -> list[GroupedNoticeChar]:
    groups: dict[str, GroupedNoticeChar] = {}
    order: list[str] = []
    last_key = ""

    for raw in notice_chars:
        if not isinstance(raw, dict):
            continue
        key = _group_key(raw)
        if not key and last_key:
            key = last_key
        if not key:
            continue
        last_key = key

        label = str(raw.get("display_label") or "").strip()
        code = _notice_code(raw) or None
        mk = str(raw.get("match_key") or "").strip()
        if not mk or mk == "name:":
            mk = f"name:{_norm_label(label)}" if _is_real_label(label) else ""

        if key not in groups:
            groups[key] = GroupedNoticeChar(
                display_label=label if _is_real_label(label) else "",
                characteristic_code=code,
                match_key=mk,
                values=[],
            )
            order.append(key)
        else:
            group = groups[key]
            if _is_real_label(label):
                group.display_label = label
            if code and not group.characteristic_code:
                group.characteristic_code = code
            if mk and mk != "name:":
                group.match_key = mk

        for part in split_value_display(str(raw.get("value_display") or "")):
            if part not in groups[key].values:
                groups[key].values.append(part)

    return [groups[k] for k in order if groups[k].values]


def _allows_multiple_values(template_char: dict[str, Any]) -> bool:
    kind = template_char.get("characteristicKind") if isinstance(template_char.get("characteristicKind"), dict) else {}
    choice = (
        template_char.get("characteristicChoiceType")
        if isinstance(template_char.get("characteristicChoiceType"), dict)
        else {}
    )
    kind_name = str(kind.get("name") or "")
    choice_name = str(choice.get("name") or "")
    return "нескольких значений" in kind_name or "Множественный" in choice_name


def _lookup_by_name_aliases(by_name: dict[str, dict[str, Any]], label: str) -> dict[str, Any] | None:
    keys = label_alias_keys(label)
    if not keys:
        return None
    for name_key, row in by_name.items():
        if name_key in keys or label_alias_keys(str(row.get("name") or "")) & keys:
            return row
    return None


def find_template_characteristic(
    ktru_item: dict[str, Any],
    notice_char: dict[str, Any],
) -> dict[str, Any] | None:
    code = _notice_code(notice_char)
    name_key = _notice_name_key(notice_char)
    by_code: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for ch in ktru_item.get("characteristics") or []:
        if not isinstance(ch, dict):
            continue
        c = str(ch.get("code") or "").strip()
        if c:
            by_code[c] = ch
        nm = _norm_label(str(ch.get("name") or ""))
        if nm:
            by_name[nm] = ch
    if code and code in by_code:
        return by_code[code]
    if name_key and name_key in by_name:
        return by_name[name_key]
    if name_key:
        alias_hit = _lookup_by_name_aliases(by_name, name_key)
        if alias_hit is not None:
            return alias_hit
    return None


@dataclass
class ApplyResult:
    matched: int = 0
    unmatched_notice: list[dict[str, str]] = field(default_factory=list)
    unmatched_find: list[dict[str, str]] = field(default_factory=list)
    unfilled_required: list[str] = field(default_factory=list)
    value_match_exact: int = 0
    value_match_fuzzy: int = 0
    value_unmatched: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "unmatched_notice": self.unmatched_notice,
            "unmatched_find": self.unmatched_find,
            "unfilled_required": self.unfilled_required,
            "value_match_exact": self.value_match_exact,
            "value_match_fuzzy": self.value_match_fuzzy,
            "value_unmatched": self.value_unmatched,
        }

    @property
    def fill_summary(self) -> str:
        total = self.matched + len(self.unmatched_notice)
        if total == 0:
            return "0/0"
        return f"{self.matched}/{total}"


def _normalize_num(text: str) -> str:
    m = _NUM_RE.search(text.replace("\xa0", " "))
    if not m:
        return ""
    return m.group(1).replace(".", ",")


def parse_notice_value(value_display: str) -> dict[str, Any]:
    """Parse EIS value_display into RTS value fields."""
    raw = _WS_RE.sub(" ", (value_display or "").strip())
    low = raw.lower()
    result: dict[str, Any] = {
        "qualityDescription": None,
        "concreteValue": None,
        "minValue": None,
        "maxValue": None,
        "minMathNotation": None,
        "maxMathNotation": None,
    }
    if not raw or raw == "—":
        return result

    min_match = re.search(r"не\s+менее\s+(\d+(?:[.,]\d+)?)", low)
    max_match = re.search(r"не\s+более\s+(\d+(?:[.,]\d+)?)", low)
    if min_match:
        result["minValue"] = min_match.group(1).replace(".", ",")
        result["minMathNotation"] = "ge"
    if max_match:
        result["maxValue"] = max_match.group(1).replace(".", ",")
        result["maxMathNotation"] = "le"

    if result["minValue"] or result["maxValue"]:
        return result

    min_xml = re.search(r"\bмин\s+(\d+(?:[.,]\d+)?)", low)
    max_xml = re.search(r"\bмакс\s+(\d+(?:[.,]\d+)?)", low)
    if min_xml:
        result["minValue"] = min_xml.group(1).replace(".", ",")
        if re.search(r"greaterorequal|greater\s*or\s*equal", low):
            result["minMathNotation"] = "ge"
        elif re.search(r"\bgreater\b", low):
            result["minMathNotation"] = "gt"
        else:
            result["minMathNotation"] = "ge"
    if max_xml:
        result["maxValue"] = max_xml.group(1).replace(".", ",")
        if re.search(r"lessorequal|less\s*or\s*equal", low):
            result["maxMathNotation"] = "le"
        elif re.search(r"\bless\b", low):
            result["maxMathNotation"] = "lt"
        else:
            result["maxMathNotation"] = "le"
    if result["minValue"] or result["maxValue"]:
        return result

    if re.search(r"greaterorequal|greater\s*or\s*equal", low):
        m = re.search(r"(?:greaterorequal|greater\s*or\s*equal)\D*(\d+(?:[.,]\d+)?)", low)
        if m:
            result["minValue"] = m.group(1).replace(".", ",")
            result["minMathNotation"] = "ge"
    if re.search(r"lessorequal|less\s*or\s*equal", low):
        m = re.search(r"(?:lessorequal|less\s*or\s*equal)\D*(\d+(?:[.,]\d+)?)", low)
        if m:
            result["maxValue"] = m.group(1).replace(".", ",")
            result["maxMathNotation"] = "le"
    if result["minValue"] or result["maxValue"]:
        return result

    if _RATIO_VALUE_RE.search(raw):
        result["qualityDescription"] = raw
        return result

    if re.search(r"≥|>=", raw):
        nums = _NUM_RE.findall(raw)
        if nums:
            result["minValue"] = nums[0].replace(".", ",")
            result["minMathNotation"] = "ge"
    if re.search(r"≤|<=", raw):
        nums = _NUM_RE.findall(raw)
        if nums:
            result["maxValue"] = nums[-1].replace(".", ",")
            result["maxMathNotation"] = "le"
    strict_lt = re.search(r"(?<![≤=])<(?!=)", raw)
    if strict_lt:
        nums = _NUM_RE.findall(raw[strict_lt.start() :])
        if nums:
            result["maxValue"] = nums[0].replace(".", ",")
            result["maxMathNotation"] = "lt"
    strict_gt = re.search(r"(?<![≥=])>(?!=)", raw)
    if strict_gt:
        nums = _NUM_RE.findall(raw[strict_gt.start() :])
        if nums:
            result["minValue"] = nums[0].replace(".", ",")
            result["minMathNotation"] = "gt"
    if result["minValue"] or result["maxValue"]:
        return result

    nums = _NUM_RE.findall(raw)
    if len(nums) == 1 and any(x in low for x in ("шт", "штук", "кг", "мм", "см", "м ")):
        result["concreteValue"] = nums[0].replace(".", ",")
        return result
    if len(nums) >= 2 and ("-" in raw or "до" in low):
        result["minValue"] = nums[0].replace(".", ",")
        result["maxValue"] = nums[1].replace(".", ",")
        result["minMathNotation"] = "ge"
        result["maxMathNotation"] = "le"
        return result

    result["qualityDescription"] = raw
    return result


def _fuzzy_match_value(parsed: dict[str, Any], options: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    if not options:
        return None, "missing"

    qd = str(parsed.get("qualityDescription") or "").strip()
    if qd:
        qlow = _norm_label(qd)
        for opt in options:
            oq = _norm_label(str(opt.get("qualityDescription") or ""))
            if oq and oq == qlow:
                return opt, "exact"
        for opt in options:
            oq = str(opt.get("qualityDescription") or "").strip()
            if oq and (qd in oq or oq in qd):
                return opt, "fuzzy"

    if parsed.get("minValue") or parsed.get("maxValue") or parsed.get("concreteValue"):
        for opt in options:
            if _numeric_option_exact_match(parsed, opt):
                return opt, "exact"
        for opt in options:
            if _numeric_option_contains(parsed, opt):
                return opt, "fuzzy"
        matched = _resolve_value_option(parsed, options)
        if matched is not None:
            return matched, "fuzzy"
        return None, "missing"

    return None, "missing"


def _build_rts_value_single(
    template_char: dict[str, Any],
    notice_char: dict[str, Any],
    result: ApplyResult,
) -> list[dict[str, Any]]:
    parsed = parse_notice_value(str(notice_char.get("value_display") or ""))
    options = [cv for cv in (template_char.get("characteristicValues") or []) if isinstance(cv, dict)]
    chosen, match_kind = _fuzzy_match_value(parsed, options)

    if match_kind == "exact":
        result.value_match_exact += 1
    elif match_kind == "fuzzy":
        result.value_match_fuzzy += 1
    else:
        result.value_unmatched += 1

    base = {
        "qualityDescription": parsed.get("qualityDescription"),
        "concreteValue": parsed.get("concreteValue"),
        "minValue": parsed.get("minValue"),
        "maxValue": parsed.get("maxValue"),
        "minMathNotation": parsed.get("minMathNotation"),
        "maxMathNotation": parsed.get("maxMathNotation"),
        "characteristicId": template_char.get("id"),
        "characteristicCode": template_char.get("code"),
        "characteristicName": template_char.get("name"),
        "okeiCode": "",
        "okeiFullName": None,
        "valueFormatId": None,
    }

    if chosen:
        mapped = _map_characteristic_value(chosen)
        if mapped.get("id") is not None:
            base["id"] = mapped["id"]
        if not base["qualityDescription"] and mapped.get("qualityDescription"):
            base["qualityDescription"] = mapped.get("qualityDescription")
        if not base["concreteValue"] and mapped.get("concreteValue"):
            base["concreteValue"] = mapped.get("concreteValue")
        if not base["minValue"] and mapped.get("minValue"):
            base["minValue"] = mapped.get("minValue")
        if not base["maxValue"] and mapped.get("maxValue"):
            base["maxValue"] = mapped.get("maxValue")
        if not base["minMathNotation"] and mapped.get("minMathNotation"):
            base["minMathNotation"] = mapped.get("minMathNotation")
        if not base["maxMathNotation"] and mapped.get("maxMathNotation"):
            base["maxMathNotation"] = mapped.get("maxMathNotation")
        base["okeiCode"] = mapped.get("okeiCode") or ""
        base["okeiFullName"] = mapped.get("okeiFullName")
        base["valueFormatId"] = mapped.get("valueFormatId")

    if not any(
        base.get(k)
        for k in ("qualityDescription", "concreteValue", "minValue", "maxValue")
    ):
        return []

    return [_characteristic_value_for_api(base, options=options, characteristic=template_char)]


def _build_rts_values(
    template_char: dict[str, Any],
    grouped: GroupedNoticeChar,
    result: ApplyResult,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[object] = set()
    for value_display in grouped.values:
        notice_char = {**grouped.as_notice_dict(), "value_display": value_display}
        for mapped in _build_rts_value_single(template_char, notice_char, result):
            token: object = mapped.get("id")
            if token is None:
                token = (
                    mapped.get("qualityDescription"),
                    mapped.get("minValue"),
                    mapped.get("maxValue"),
                    mapped.get("concreteValue"),
                )
            if token in seen:
                continue
            seen.add(token)
            out.append(mapped)
    if not _allows_multiple_values(template_char) and len(out) > 1:
        return out[:1]
    return out


def build_notice_ktru_characteristics(
    ktru_item: dict[str, Any],
    notice_chars: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], ApplyResult]:
    result = ApplyResult()
    by_template_code: dict[str, dict[str, Any]] = {}

    for group in group_notice_characteristics(notice_chars):
        template = find_template_characteristic(ktru_item, group.as_notice_dict())
        if not template:
            for value_display in group.values:
                result.unmatched_notice.append(
                    {
                        "label": group.display_label,
                        "value": value_display,
                        "code": group.characteristic_code or "",
                    }
                )
            continue

        code = str(template.get("code") or "").strip()
        values = _build_rts_values(template, group, result)
        if code in by_template_code:
            existing = by_template_code[code].setdefault("characteristicValues", [])
            present = {row.get("id") for row in existing if isinstance(row, dict)}
            for row in values:
                if row.get("id") not in present:
                    existing.append(row)
                    present.add(row.get("id"))
            continue

        mapped = _map_create_characteristic(template)
        if values:
            mapped["characteristicValues"] = values
        by_template_code[code] = mapped
        result.matched += 1

    included_codes = set(by_template_code)
    out = list(by_template_code.values())

    for template in ktru_item.get("characteristics") or []:
        if not isinstance(template, dict) or not template.get("isRequired"):
            continue
        code = str(template.get("code") or "").strip()
        if code and code not in included_codes:
            out.append(_map_create_characteristic(template))
            result.unfilled_required.append(str(template.get("name") or code))

    return out, result


def build_notice_create_payload(
    ktru_item: dict[str, Any],
    notice_chars: list[dict[str, Any]],
    *,
    unit_from_line: str | None = None,
) -> tuple[dict[str, Any], ApplyResult]:
    """Create payload: full KTRU template characteristics + notice values overlaid."""
    from rts_pp_api import build_create_payload

    notice_mapped, apply_result = build_notice_ktru_characteristics(ktru_item, notice_chars)
    by_code = {
        str(ch.get("code") or "").strip(): ch
        for ch in notice_mapped
        if isinstance(ch, dict) and str(ch.get("code") or "").strip()
    }
    payload = build_create_payload(ktru_item, unit_from_line=unit_from_line)
    for ch in payload.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or "").strip()
        overlay = by_code.get(code)
        if overlay and overlay.get("characteristicValues"):
            ch["characteristicValues"] = overlay["characteristicValues"]
    return payload, apply_result


def ensure_item_ktru_fields(item: dict[str, Any], ktru_item: dict[str, Any]) -> None:
    """RTS edit UI expects ktru/name on the saved draft item."""
    code = str(ktru_item.get("code") or "").strip()
    name = str(ktru_item.get("name") or "").strip()
    if name and not str(item.get("name") or "").strip():
        item["name"] = name
    ktru = item.get("ktru") if isinstance(item.get("ktru"), dict) else {}
    if str(ktru.get("code") or "").strip():
        return
    item["ktru"] = {
        "code": code,
        "name": name or code,
        "okpd2": ktru_item.get("okpd2"),
        "version": ktru_item.get("version"),
    }


def apply_notice_to_draft_item(
    item: dict[str, Any],
    notice_chars: list[dict[str, Any]],
    ktru_item: dict[str, Any],
) -> ApplyResult:
    """Merge notice values into GET draft item by characteristic code/name."""
    result = ApplyResult()
    draft_chars = item.get("ktruCharacteristics") or []
    if not isinstance(draft_chars, list):
        draft_chars = []

    draft_by_code = {
        str(ch.get("code") or "").strip(): ch
        for ch in draft_chars
        if isinstance(ch, dict) and ch.get("code")
    }

    built, build_result = build_notice_ktru_characteristics(ktru_item, notice_chars)
    result.unmatched_notice = build_result.unmatched_notice
    result.unfilled_required = build_result.unfilled_required
    result.value_match_exact = build_result.value_match_exact
    result.value_match_fuzzy = build_result.value_match_fuzzy
    result.value_unmatched = build_result.value_unmatched

    for mapped in built:
        code = str(mapped.get("code") or "").strip()
        if not code:
            continue
        if code in draft_by_code:
            target = draft_by_code[code]
            if mapped.get("characteristicValues"):
                target["characteristicValues"] = mapped["characteristicValues"]
                result.matched += 1
        else:
            draft_chars.append(mapped)
            result.matched += 1

    item["ktruCharacteristics"] = draft_chars
    return result


def fill_remaining_required_from_template(
    item: dict[str, Any],
    ktru_item: dict[str, Any],
    result: ApplyResult,
) -> None:
    for ch in item.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict) or not ch.get("isRequired") or ch.get("characteristicValues"):
            continue
        options = _search_char_options(ktru_item, str(ch.get("code") or ""))
        if options:
            _set_characteristic_value(ch, options[0])
            name = str(ch.get("name") or ch.get("code") or "")
            if name not in result.unfilled_required:
                result.unfilled_required.append(name)


def render_characteristic_value(cv: dict[str, Any], *, include_id: bool = False) -> str:
    """Human-readable single entry from draft characteristicValues."""
    qd = str(cv.get("qualityDescription") or "").strip()
    if qd:
        text = qd
    elif cv.get("minValue") or cv.get("maxValue"):
        parts: list[str] = []
        if cv.get("minValue"):
            notation = str(cv.get("minMathNotation") or "")
            prefix = "≥" if notation == "ge" else ("≤" if notation == "le" else "")
            parts.append(f"{prefix}{cv.get('minValue')}")
        if cv.get("maxValue"):
            notation = str(cv.get("maxMathNotation") or "")
            prefix = "≤" if notation == "le" else ""
            parts.append(f"{prefix}{cv.get('maxValue')}")
        text = " ".join(parts)
    elif cv.get("concreteValue"):
        text = str(cv.get("concreteValue") or "").strip()
    else:
        text = ""
    if include_id and cv.get("id") is not None:
        suffix = f"(id={cv['id']})"
        text = f"{text} {suffix}".strip() if text else suffix
    return text


def render_characteristic_values(
    values: list[dict[str, Any]],
    *,
    include_id: bool = False,
) -> list[str]:
    out: list[str] = []
    for cv in values:
        if not isinstance(cv, dict):
            continue
        rendered = render_characteristic_value(cv, include_id=include_id)
        if rendered:
            out.append(rendered)
    return out


def _draft_char_maps(draft_item: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_code: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for ch in draft_item.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or "").strip()
        if code:
            by_code[code] = ch
        nm = _norm_label(str(ch.get("name") or ""))
        if nm:
            by_name[nm] = ch
    return by_code, by_name


def _find_draft_characteristic(
    draft_item: dict[str, Any],
    group: GroupedNoticeChar,
    *,
    ktru_item: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    by_code, by_name = _draft_char_maps(draft_item)
    code = str(group.characteristic_code or "").strip()
    if code and code in by_code:
        return by_code[code]
    label_key = _norm_label(group.display_label)
    if label_key and label_key in by_name:
        return by_name[label_key]
    if label_key:
        alias_hit = _lookup_by_name_aliases(by_name, label_key)
        if alias_hit is not None:
            return alias_hit
    if ktru_item:
        template = find_template_characteristic(ktru_item, group.as_notice_dict())
        if template:
            tc = str(template.get("code") or "").strip()
            if tc in by_code:
                return by_code[tc]
    return None


def _value_signature(cv: dict[str, Any]) -> object:
    if cv.get("id") is not None:
        return ("id", cv.get("id"))
    if cv.get("qualityDescription"):
        return ("qd", _norm_label(str(cv.get("qualityDescription") or "")))
    if cv.get("minValue") or cv.get("maxValue") or cv.get("concreteValue"):
        return (
            "num",
            cv.get("minValue"),
            cv.get("maxValue"),
            cv.get("concreteValue"),
            cv.get("minMathNotation"),
            cv.get("maxMathNotation"),
        )
    return ("raw", str(cv))


def _expected_value_signatures(
    group: GroupedNoticeChar,
    template_char: dict[str, Any],
) -> set[object]:
    dummy = ApplyResult()
    built = _build_rts_values(template_char, group, dummy)
    return {_value_signature(v) for v in built}


def _draft_value_signatures(draft_char: dict[str, Any]) -> set[object]:
    return {
        _value_signature(v)
        for v in (draft_char.get("characteristicValues") or [])
        if isinstance(v, dict)
    }


def _compare_value_sets(
    *,
    notice_values: list[str],
    draft_values: list[str],
    expected: set[object],
    actual: set[object],
) -> tuple[str, str]:
    if expected == actual:
        return "ok", ""
    if len(expected) != len(actual):
        return "count_mismatch", f"ожидалось {len(expected)}, в черновике {len(actual)}"
    if not expected and not actual and notice_values and draft_values:
        notice_norm = {_norm_label(v) for v in notice_values}
        draft_norm = {_norm_label(v) for v in draft_values}
        if notice_norm == draft_norm:
            return "ok", ""
    return "value_mismatch", "разный состав значений"


def compare_notice_to_draft(
    notice_chars: list[dict[str, Any]],
    draft_item: dict[str, Any],
    *,
    ktru_item: dict[str, Any] | None = None,
    include_id_in_render: bool = False,
) -> dict[str, Any]:
    """Compare enriched notice characteristics with final RTS draft GET item."""
    rows: list[dict[str, Any]] = []
    compared_draft_codes: set[str] = set()

    for group in group_notice_characteristics(notice_chars):
        notice_values = list(group.values)
        template = find_template_characteristic(ktru_item, group.as_notice_dict()) if ktru_item else None
        draft_char = _find_draft_characteristic(draft_item, group, ktru_item=ktru_item)
        label = (
            group.display_label
            or (str(template.get("name") or "") if template else "")
            or str(group.characteristic_code or "")
            or "—"
        )
        rts_code = str(group.characteristic_code or "").strip() or None
        rts_name = str(template.get("name") or "") if template else None

        if draft_char is None:
            rows.append(
                {
                    "notice_label": label,
                    "notice_code": rts_code,
                    "notice_values": notice_values,
                    "rts_code": rts_code or (str(template.get("code") or "") if template else None),
                    "rts_name": rts_name,
                    "draft_values": [],
                    "status": "unmatched_notice",
                    "detail": "характеристика не найдена в черновике",
                }
            )
            continue

        code = str(draft_char.get("code") or "").strip()
        if code:
            compared_draft_codes.add(code)
            rts_code = rts_code or code
        if not rts_name:
            rts_name = str(draft_char.get("name") or "") or None

        draft_vals_raw = [
            v for v in (draft_char.get("characteristicValues") or []) if isinstance(v, dict)
        ]
        draft_values = render_characteristic_values(
            draft_vals_raw,
            include_id=include_id_in_render,
        )

        if not draft_vals_raw:
            rows.append(
                {
                    "notice_label": label,
                    "notice_code": rts_code,
                    "notice_values": notice_values,
                    "rts_code": rts_code,
                    "rts_name": rts_name,
                    "draft_values": [],
                    "status": "missing_in_draft",
                    "detail": "пустые characteristicValues в черновике",
                }
            )
            continue

        if template:
            expected = _expected_value_signatures(group, template)
            actual = _draft_value_signatures(draft_char)
        else:
            expected = {_norm_label(v) for v in notice_values}
            actual = {_norm_label(v) for v in draft_values}

        status, detail = _compare_value_sets(
            notice_values=notice_values,
            draft_values=draft_values,
            expected=expected,
            actual=actual,
        )
        rows.append(
            {
                "notice_label": label,
                "notice_code": rts_code,
                "notice_values": notice_values,
                "rts_code": rts_code,
                "rts_name": rts_name,
                "draft_values": draft_values,
                "status": status,
                "detail": detail,
            }
        )

    for ch in draft_item.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or "").strip()
        if not code or code in compared_draft_codes:
            continue
        draft_name = str(ch.get("name") or "")
        if any(
            labels_are_aliases(draft_name, str(row.get("notice_label") or ""))
            for row in rows
            if str(row.get("status") or "") == "ok"
        ):
            continue
        draft_vals_raw = [v for v in (ch.get("characteristicValues") or []) if isinstance(v, dict)]
        if not draft_vals_raw:
            continue
        rows.append(
            {
                "notice_label": "",
                "notice_code": None,
                "notice_values": [],
                "rts_code": code,
                "rts_name": str(ch.get("name") or "") or None,
                "draft_values": render_characteristic_values(
                    draft_vals_raw,
                    include_id=include_id_in_render,
                ),
                "status": "auto_filled",
                "detail": "заполнено автоматически",
            }
        )

    ok_count = sum(1 for row in rows if row.get("status") == "ok")
    issues = len(rows) - ok_count
    return {"total": len(rows), "ok": ok_count, "issues": issues, "rows": rows}


def compare_notice_to_template(
    notice_chars: list[dict[str, Any]],
    ktru_item: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rows for CLI/UI compare table."""
    rows: list[dict[str, Any]] = []
    for notice_char in notice_chars:
        if not isinstance(notice_char, dict):
            continue
        template = find_template_characteristic(ktru_item, notice_char)
        parsed = parse_notice_value(str(notice_char.get("value_display") or ""))
        row = {
            "notice_label": notice_char.get("display_label"),
            "notice_code": _notice_code(notice_char),
            "notice_value": notice_char.get("value_display"),
            "rts_name": None,
            "rts_code": None,
            "char_match": "missing",
            "value_match": "n/a",
        }
        if template:
            row["rts_name"] = template.get("name")
            row["rts_code"] = template.get("code")
            row["char_match"] = "code" if _notice_code(notice_char) else "name"
            options = [cv for cv in (template.get("characteristicValues") or []) if isinstance(cv, dict)]
            _, vm = _fuzzy_match_value(parsed, options)
            row["value_match"] = vm
        rows.append(row)
    return rows

"""Group competitor PP registry rows into stacks by KTRU characteristics fingerprint."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from monitor_find_to_draft import COMPETITOR_DRAFT_PRICE_FACTOR
from notice_to_rts_pp import _norm_label, _value_signature
from participation_price import format_rts_unit_price_from_float, parse_money_amount

EMPTY_HASH = "empty"


def _json_dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def find_characteristics_fingerprint(find_item: dict[str, Any]) -> tuple[str, str]:
    """Digest + canonical JSON from find ktruCharacteristics only (no delivery)."""
    pairs: list[dict[str, Any]] = []
    raw_chars = find_item.get("ktruCharacteristics")
    if not isinstance(raw_chars, list):
        raw_chars = []
    for ch in raw_chars:
        if not isinstance(ch, dict):
            continue
        key_raw = str(ch.get("code") or ch.get("characteristicCode") or "").strip()
        if not key_raw:
            key_raw = str(ch.get("name") or ch.get("characteristicName") or "").strip()
        key = _norm_label(key_raw)
        raw_vals = ch.get("characteristicValues")
        if not isinstance(raw_vals, list):
            raw_vals = []
        sigs = sorted(
            (_value_signature(v) for v in raw_vals if isinstance(v, dict)),
            key=lambda x: repr(x),
        )
        if key or sigs:
            pairs.append({"key": key, "values": sigs})
    pairs.sort(key=lambda x: (x["key"], repr(x["values"])))
    if not pairs:
        return EMPTY_HASH, _json_dumps([])
    canonical = _json_dumps(pairs)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return digest, canonical


def stack_key(ktru_code: str, characteristics_hash: str) -> str:
    code = str(ktru_code or "").strip()
    chash = str(characteristics_hash or "").strip()
    return f"{code}:{chash}"


def parse_stack_key(raw: str) -> tuple[str, str] | None:
    text = str(raw or "").strip()
    if ":" not in text:
        return None
    code, chash = text.split(":", 1)
    code = code.strip()
    chash = chash.strip()
    if not code or not chash or chash == EMPTY_HASH:
        return None
    return code, chash


def our_price_from_competitor(competitor_price: float) -> float:
    return float(competitor_price) * COMPETITOR_DRAFT_PRICE_FACTOR


def prices_match(actual: float | None, expected: float | None, *, epsilon: float = 0.01) -> bool:
    if actual is None or expected is None:
        return False
    try:
        a = float(actual)
        e = float(expected)
    except (TypeError, ValueError):
        return False
    if abs(a - e) <= epsilon:
        return True
    formatted = parse_money_amount(format_rts_unit_price_from_float(e))
    if formatted is not None and abs(a - formatted) <= epsilon:
        return True
    return False


def _member_price(member: dict[str, Any]) -> float | None:
    raw = member.get("unit_price")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _exact_rts_match(actual: float, competitor_price: float) -> bool:
    expected = our_price_from_competitor(competitor_price)
    formatted = parse_money_amount(format_rts_unit_price_from_float(expected))
    if formatted is None:
        return False
    return abs(actual - formatted) < 0.001


def mark_our_pp(
    members: list[dict[str, Any]],
    *,
    draft_prices: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Annotate members with is_our_pp using competitor price × 0.95 rule."""
    draft_prices = draft_prices or {}
    out: list[dict[str, Any]] = []
    for member in members:
        row = dict(member)
        row["is_our_pp"] = False
        out.append(row)

    candidates: list[int] = []
    for idx, member in enumerate(out):
        price_m = _member_price(member)
        if price_m is None:
            continue
        oid = str(member.get("offer_price_list_item_id") or "").strip()
        matched = False
        for jdx, other in enumerate(out):
            if idx == jdx:
                continue
            price_o = _member_price(other)
            if price_o is None:
                continue
            if prices_match(price_m, our_price_from_competitor(price_o)):
                matched = True
                break
            other_oid = str(other.get("offer_price_list_item_id") or "").strip()
            draft_price = draft_prices.get(other_oid)
            if draft_price is not None and prices_match(price_m, draft_price):
                matched = True
                break
        if matched:
            candidates.append(idx)

    if len(candidates) == 1:
        out[candidates[0]]["is_our_pp"] = True
        return out

    if len(candidates) > 1:
        exact: list[int] = []
        for idx in candidates:
            price_m = _member_price(out[idx])
            if price_m is None:
                continue
            for jdx, other in enumerate(out):
                if idx == jdx:
                    continue
                price_o = _member_price(other)
                if price_o is not None and _exact_rts_match(price_m, price_o):
                    exact.append(idx)
                    break
        if len(exact) == 1:
            out[exact[0]]["is_our_pp"] = True
    return out


def stack_has_our_pp(members: list[dict[str, Any]], *, draft_prices: dict[str, float] | None = None) -> bool:
    marked = mark_our_pp(members, draft_prices=draft_prices)
    return any(bool(m.get("is_our_pp")) for m in marked)

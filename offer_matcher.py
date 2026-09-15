"""Match customer KTRU requirements against RTS pre-offers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from rts_preoffers import RtsPreoffer, normalize_text

MatchStatus = Literal["match", "missing", "mismatch"]


@dataclass(frozen=True, slots=True)
class OfferMatchRow:
    match_key: str
    label: str
    customer_value: str
    offer_value: str
    status: MatchStatus

    def as_dict(self) -> dict[str, str]:
        return {
            "match_key": self.match_key,
            "label": self.label,
            "customer_value": self.customer_value,
            "offer_value": self.offer_value,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class OfferMatchResult:
    offer_id: str
    title: str
    rts_url: str
    sku: str
    ktru_code: str
    passes: bool
    matched: int
    required: int
    missing: int
    mismatched: int
    rows: list[OfferMatchRow] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "title": self.title,
            "rts_url": self.rts_url,
            "sku": self.sku,
            "ktru_code": self.ktru_code,
            "passes": self.passes,
            "matched": self.matched,
            "required": self.required,
            "missing": self.missing,
            "mismatched": self.mismatched,
            "rows": [r.as_dict() for r in self.rows],
        }


def _customer_match_key(raw: dict[str, Any]) -> str:
    mk = str(raw.get("match_key") or "").strip()
    label = str(raw.get("display_label") or raw.get("label") or "").strip()
    if not mk and label:
        mk = f"name:{normalize_text(label)}"
    return normalize_text(mk)


def _customer_label(raw: dict[str, Any]) -> str:
    return str(raw.get("display_label") or raw.get("label") or "(без имени)").strip()


def _customer_value(raw: dict[str, Any]) -> str:
    return str(raw.get("value_display") or raw.get("display_value") or raw.get("value") or "").strip()


def match_offer(customer_characteristics: list[dict[str, Any]], offer: RtsPreoffer) -> OfferMatchResult:
    offer_chars = {c.match_key: c for c in offer.characteristics}
    rows: list[OfferMatchRow] = []
    matched = 0
    missing = 0
    mismatched = 0
    for raw in customer_characteristics:
        if not isinstance(raw, dict):
            continue
        mk = _customer_match_key(raw)
        if not mk:
            continue
        label = _customer_label(raw)
        customer_value = _customer_value(raw)
        offer_ch = offer_chars.get(mk)
        if offer_ch is None:
            missing += 1
            rows.append(OfferMatchRow(mk, label, customer_value or "—", "—", "missing"))
            continue
        offer_value = offer_ch.value
        if normalize_text(customer_value) == normalize_text(offer_value) and (customer_value or offer_value):
            matched += 1
            status: MatchStatus = "match"
        else:
            mismatched += 1
            status = "mismatch"
        rows.append(OfferMatchRow(mk, label, customer_value or "—", offer_value or "—", status))
    required = len(rows)
    return OfferMatchResult(
        offer_id=offer.offer_id,
        title=offer.title,
        rts_url=offer.rts_url,
        sku=offer.sku,
        ktru_code=offer.ktru_code,
        passes=required > 0 and matched == required,
        matched=matched,
        required=required,
        missing=missing,
        mismatched=mismatched,
        rows=rows,
    )


def rank_offer_matches(
    customer_characteristics: list[dict[str, Any]],
    offers: list[RtsPreoffer],
) -> list[OfferMatchResult]:
    results = [match_offer(customer_characteristics, offer) for offer in offers]
    results.sort(key=lambda r: (not r.passes, -r.matched, r.missing + r.mismatched, r.title.lower()))
    return results

"""Find RTS PP predecessors before an EIS-linked anchor PP (variant key: hash + EIS region)."""

from __future__ import annotations


import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eis_analytics_db import connect, get_pp_monitor_finding
from monitor_find_to_draft import competitor_unit_price_amount
from rts_pp_api import parse_oktmo_from_delivery_place

_REGION_PREFIX_RE = re.compile(
    r"^(г|г\.|город|гор\.|пгт|с|с\.|пос\.|поселок|посёлок|р-н|район|обл|обл\.|область|респ|респ\.|республика)\s+",
    re.IGNORECASE,
)


def _norm_region_text(raw: str) -> str:
    text = str(raw or "").replace("\xa0", " ").strip().lower()
    text = text.replace("ё", "е")
    text = _REGION_PREFIX_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _notice_region_context(db_path: Path, purchase_number: str) -> dict[str, str]:
    pn = str(purchase_number or "").strip()
    if not pn or not db_path.is_file():
        return {"region": "", "delivery_place": "", "oktmo_code": ""}
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT region, delivery_place
            FROM notice_html_pages
            WHERE purchase_number = ? AND ok = 1
            """,
            (pn,),
        ).fetchone()
    if not row:
        return {"region": "", "delivery_place": "", "oktmo_code": ""}
    region = str(row[0] or "").strip()
    delivery_place = str(row[1] or "").strip()
    parsed = parse_oktmo_from_delivery_place(delivery_place)
    oktmo_code = str(parsed.get("code") or "").strip() if parsed else ""
    return {
        "region": region,
        "delivery_place": delivery_place,
        "oktmo_code": oktmo_code,
    }


def notice_region_matches_pp_place(
    notice_ctx: dict[str, str],
    place: dict[str, Any],
) -> tuple[bool, str]:
    oktmo = place.get("oktmo") if isinstance(place.get("oktmo"), dict) else {}
    code = str(oktmo.get("code") or "").strip()
    name = str(oktmo.get("name") or "").strip()
    notice_code = str(notice_ctx.get("oktmo_code") or "").strip()
    if notice_code and code and notice_code == code:
        return True, name or code
    needles = [
        _norm_region_text(notice_ctx.get("region") or ""),
        _norm_region_text(notice_ctx.get("delivery_place") or ""),
    ]
    hay = _norm_region_text(name)
    if not hay:
        return False, ""
    for needle in needles:
        if not needle:
            continue
        if needle in hay or hay in needle:
            return True, name or code
    return False, ""


def _candidate_regions_from_find(find_item: dict[str, Any]) -> list[dict[str, Any]]:
    places = find_item.get("deliveryPlaces")
    if not isinstance(places, list):
        return []
    out: list[dict[str, Any]] = []
    for place in places:
        if isinstance(place, dict):
            out.append(place)
    return out


def _parse_link_dt(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        elif "+" not in text:
            text = text + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _delta_days_label(anchor_raw: str, predecessor_raw: str) -> str:
    a = _parse_link_dt(anchor_raw)
    b = _parse_link_dt(predecessor_raw)
    if a is None or b is None:
        return "—"
    days = (a - b).total_seconds() / 86400.0
    rounded = round(days, 1)
    if rounded <= 0:
        return "0 дн."
    return f"−{rounded:g} дн."


@dataclass
class PrehistoryRow:
    offer_price_list_item_id: str
    link_date: str
    unit_price: float | None
    matched_region_label: str
    trade_mark: str = ""
    card_url: str = ""
    link_date_display_msk: str = ""
    delta_to_anchor_label: str = "—"

    def as_dict(self) -> dict[str, Any]:
        return {
            "offer_price_list_item_id": self.offer_price_list_item_id,
            "link_date": self.link_date,
            "unit_price": self.unit_price,
            "matched_region_label": self.matched_region_label,
            "trade_mark": self.trade_mark,
            "card_url": self.card_url,
            "link_date_display_msk": self.link_date_display_msk,
            "delta_to_anchor_label": self.delta_to_anchor_label,
            "pp_offer_url": (
                f"https://offers.rts-tender.ru/customer/offers/offer-item/{self.offer_price_list_item_id}"
                if self.offer_price_list_item_id
                else ""
            ),
        }


@dataclass
class PrehistoryResult:
    has_prehistory: bool = False
    predecessor_count: int = 0
    predecessors: list[PrehistoryRow] = field(default_factory=list)
    notice_region: str = ""
    notice_delivery_place: str = ""
    anchor_unit_price: float | None = None
    anchor_link_date: str = ""
    characteristics_hash: str = ""
    skipped_no_find: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "has_prehistory": self.has_prehistory,
            "predecessor_count": self.predecessor_count,
            "predecessors": [p.as_dict() for p in self.predecessors],
            "notice_region": self.notice_region,
            "notice_delivery_place": self.notice_delivery_place,
            "anchor_unit_price": self.anchor_unit_price,
            "anchor_link_date": self.anchor_link_date,
            "characteristics_hash": self.characteristics_hash,
            "skipped_no_find": self.skipped_no_find,
            "error": self.error,
        }


def find_predecessors(
    db_path: Path,
    match: dict[str, Any],
    *,
    load_finding: Any = get_pp_monitor_finding,
) -> PrehistoryResult:
    from eis_analytics_db import _display_msk

    purchase_number = str(match.get("purchase_number") or "").strip()
    anchor_oid = str(match.get("offer_price_list_item_id") or "").strip()
    ktru_code = str(match.get("ktru_code") or "").strip()
    anchor_link = str(match.get("pp_link_date") or "").strip()

    result = PrehistoryResult(anchor_link_date=anchor_link)
    if not anchor_oid or not ktru_code or not db_path.is_file():
        result.error = "missing_anchor"
        return result

    notice_ctx = _notice_region_context(db_path, purchase_number)
    result.notice_region = notice_ctx.get("region") or ""
    result.notice_delivery_place = notice_ctx.get("delivery_place") or ""

    anchor_row = load_finding(db_path, anchor_oid)
    anchor_find = anchor_row.get("find") if anchor_row and isinstance(anchor_row.get("find"), dict) else {}

    chash = ""
    if anchor_row:
        chash = str(anchor_row.get("characteristics_hash") or "").strip()
    if not chash:
        sk = str(match.get("stack_key") or "").strip()
        if ":" in sk:
            chash = sk.split(":", 1)[1].strip()
    if not chash:
        with connect(db_path) as conn:
            reg = conn.execute(
                "SELECT characteristics_hash, unit_price, link_date FROM rts_pp_monitor_registry WHERE offer_price_list_item_id = ?",
                (anchor_oid,),
            ).fetchone()
        if reg:
            chash = str(reg[0] or "").strip()
            if not anchor_link:
                anchor_link = str(reg[2] or "").strip()
                result.anchor_link_date = anchor_link
    result.characteristics_hash = chash
    if not chash or chash == "empty":
        result.error = "missing_characteristics_hash"
        return result

    anchor_price = competitor_unit_price_amount(
        anchor_find,
        fallback_unit_price=(anchor_row or {}).get("unit_price"),
    )
    if anchor_price is None and anchor_row:
        try:
            anchor_price = float(anchor_row.get("unit_price"))
        except (TypeError, ValueError):
            anchor_price = None
    result.anchor_unit_price = anchor_price

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.offer_price_list_item_id, r.trade_mark, r.link_date, r.unit_price, r.card_url,
                   CASE WHEN f.offer_price_list_item_id IS NOT NULL THEN 1 ELSE 0 END AS has_find
            FROM rts_pp_monitor_registry r
            LEFT JOIN rts_pp_monitor_findings f ON f.offer_price_list_item_id = r.offer_price_list_item_id
            WHERE r.ktru_code = ?
              AND r.characteristics_hash = ?
              AND r.offer_price_list_item_id != ?
              AND r.link_date < ?
            ORDER BY r.link_date DESC
            """,
            (ktru_code, chash, anchor_oid, anchor_link or "9999-12-31T23:59:59Z"),
        ).fetchall()

    predecessors: list[PrehistoryRow] = []
    for row in rows:
        d = dict(row)
        oid = str(d.get("offer_price_list_item_id") or "").strip()
        if not oid:
            continue
        try:
            unit_price = float(d.get("unit_price")) if d.get("unit_price") is not None else None
        except (TypeError, ValueError):
            unit_price = None
        finding = load_finding(db_path, oid)
        find_item = finding.get("find") if finding and isinstance(finding.get("find"), dict) else {}
        if not find_item:
            result.skipped_no_find += 1
            continue
        matched_region = ""
        region_ok = False
        for place in _candidate_regions_from_find(find_item):
            ok, label = notice_region_matches_pp_place(notice_ctx, place)
            if ok:
                region_ok = True
                matched_region = label
                break
        if not region_ok:
            continue
        predecessors.append(
            PrehistoryRow(
                offer_price_list_item_id=oid,
                link_date=str(d.get("link_date") or ""),
                unit_price=unit_price,
                matched_region_label=matched_region,
                trade_mark=str(d.get("trade_mark") or ""),
                card_url=str(d.get("card_url") or ""),
                link_date_display_msk=_display_msk(d.get("link_date")),
                delta_to_anchor_label=_delta_days_label(anchor_link, str(d.get("link_date") or "")),
            )
        )

    predecessors.sort(key=lambda p: (p.link_date, p.offer_price_list_item_id), reverse=True)
    result.predecessors = predecessors
    result.predecessor_count = len(predecessors)
    result.has_prehistory = bool(predecessors)
    return result


def enrich_matches_with_prehistory(
    db_path: Path,
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for match in matches:
        row = dict(match)
        ph = find_predecessors(db_path, row)
        row["prehistory_count"] = ph.predecessor_count
        row["has_prehistory"] = ph.has_prehistory
        out.append(row)
    return out

"""Parse EIS notice supplier-results HTML (winner offer total)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from bs4 import BeautifulSoup, Tag

from eis_changelog import MSK, dt_to_msk_clean
from participation_price import parse_money_amount

_WS_RE = re.compile(r"\s+")
_EVENT_DT_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}(?::\d{2})?)")
_MSK_OFFSET_RE = re.compile(r"МСК\s*([+-]?\d+)?", re.IGNORECASE)
_WINNER_RANK_RE = re.compile(r"^\s*1\s*-\s*победитель\s*$", re.IGNORECASE)


def _clean_text(value: object) -> str:
    return _WS_RE.sub(" ", str(value or "").replace("\xa0", " ").strip())


def _timezone_from_eis_label(label: str, title: str = "") -> timezone:
    text = _clean_text(label).upper().replace(" ", "")
    m = _MSK_OFFSET_RE.search(text)
    if m:
        raw = m.group(1)
        delta = int(raw) if raw not in (None, "", "+", "-") else 0
        hours = 3 + delta
        return timezone(timedelta(hours=hours), f"UTC{hours:+d}")
    title_l = _clean_text(title).lower()
    if "екатеринбург" in title_l:
        return timezone(timedelta(hours=5), "UTC+5")
    return MSK


def _parse_formed_datetime(raw: str, tz: timezone) -> datetime | None:
    text = _clean_text(raw)
    m = _EVENT_DT_RE.search(text)
    if not m:
        return None
    token = m.group(1)
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(token, fmt)
            return dt.replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class SupplierWinnerResult:
    participant_id: str
    offer_total: float
    rank_label: str
    formed_at_utc: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "offer_total": self.offer_total,
            "rank_label": self.rank_label,
            "formed_at_utc": self.formed_at_utc,
        }


def _participant_table(root: Tag) -> Tag | None:
    div = root.find("div", id=re.compile(r"^supplier-def-result-participant-table-"))
    if isinstance(div, Tag):
        table = div.find("table")
        if isinstance(table, Tag):
            return table
    for table in root.find_all("table", class_=re.compile(r"tableBlock")):
        if not isinstance(table, Tag):
            continue
        thead = table.find("thead")
        if not isinstance(thead, Tag):
            continue
        header_text = _clean_text(thead.get_text(" ", strip=True)).lower()
        if "предложения участников" in header_text:
            return table
    return None


def _parse_formed_at_utc(root: Tag) -> str | None:
    for section in root.find_all("section", class_=re.compile(r"blockInfo__section")):
        if not isinstance(section, Tag):
            continue
        title = section.find("span", class_="section__title")
        info = section.find("span", class_="section__info")
        if not isinstance(title, Tag) or not isinstance(info, Tag):
            continue
        if "формирования результатов определения поставщика" not in _clean_text(title.get_text()).lower():
            continue
        tz = MSK
        span = info.find("span", class_="timeZoneName")
        if isinstance(span, Tag):
            tz = _timezone_from_eis_label(span.get_text(" ", strip=True), str(span.get("title") or ""))
            span.decompose()
        dt = _parse_formed_datetime(info.get_text(" ", strip=True), tz)
        return _utc_iso(dt) if dt is not None else None
    return None


def parse_supplier_results_html(html: str) -> SupplierWinnerResult | None:
    """Return winner row from supplier-results page, or None if not found."""
    root = BeautifulSoup(html or "", "html.parser")
    table = _participant_table(root)
    if not isinstance(table, Tag):
        return None
    tbody = table.find("tbody")
    if not isinstance(tbody, Tag):
        return None
    formed_at_utc = _parse_formed_at_utc(root)
    winner: SupplierWinnerResult | None = None
    fallback: SupplierWinnerResult | None = None
    for tr in tbody.find_all("tr", recursive=False):
        if not isinstance(tr, Tag):
            continue
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        participant_id = _clean_text(cells[0].get_text(" ", strip=True))
        rank_label = _clean_text(cells[1].get_text(" ", strip=True))
        offer_text = _clean_text(cells[2].get_text(" ", strip=True))
        offer_total = parse_money_amount(offer_text)
        if not participant_id or offer_total is None:
            continue
        row = SupplierWinnerResult(
            participant_id=participant_id,
            offer_total=float(offer_total),
            rank_label=rank_label,
            formed_at_utc=formed_at_utc,
        )
        if _WINNER_RANK_RE.match(rank_label):
            winner = row
            break
        if fallback is None:
            fallback = row
    return winner or fallback


def parse_supplier_results_display(html: str) -> dict[str, Any]:
    winner = parse_supplier_results_html(html)
    if winner is None:
        return {"ok": False, "winner": None}
    out = winner.as_dict()
    out["offer_total_display"] = offer_text_display(winner.offer_total)
    out["formed_at_display_msk"] = ""
    if winner.formed_at_utc:
        from eis_pp_link import parse_utc_datetime

        dt = parse_utc_datetime(winner.formed_at_utc)
        if dt is not None:
            out["formed_at_display_msk"] = dt_to_msk_clean(dt)
    return {"ok": True, "winner": out}


def offer_text_display(value: float) -> str:
    whole, frac = f"{float(value):.2f}".split(".")
    return f"{whole},{frac}"

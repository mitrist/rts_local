"""
Разбор ответа API ЛК РТС:
  POST https://app.rts-tender.ru/supplier/lk/api/lot-items/search/paged
  Тело запроса (JSON): {"TradeId": <int>, "PageNumber": 1, "PageLength": 15}

В документной позиции (LotItems[]): код КТРУ в ClassificationCode / ClassificationCodeForDisplay,
наименование — Name / ClassificationName, количество/цены — см. Quantity*, PricePerItem, TotalSum.
Характеристики КТРУ — блок Characteristics[]; значения могут быть в качественном описании или в числовых полях.
У РТС в JSON опечатка: CharactericticValues (без одной «s»).

Пример сохранённого ответа: responce_item.json
"""

from __future__ import annotations

import html as html_std
import json
import re
from dataclasses import dataclass, field
from typing import Any

import requests

DEFAULT_ORIGIN = "https://app.rts-tender.ru"
LOT_ITEMS_SEARCH_PAGED_PATH = "/supplier/lk/api/lot-items/search/paged"
DEFAULT_LOT_ITEMS_PAGE_LENGTH = 50
DEFAULT_APP_REFERER = "App504.aspx"


@dataclass
class LotCharacteristicValue:
    """Одно допустимое/выбранное значение характеристики лота."""

    quality_description: str | None = None
    concrete_value: str | None = None
    okei_name: str | None = None
    min_value: str | None = None
    max_value: str | None = None


@dataclass
class LotCharacteristic:
    """Характеристика позиции (по КТРУ / закупке)."""

    name: str | None = None
    order_label: str | None = None
    type_id: int | None = None
    values: list[LotCharacteristicValue] = field(default_factory=list)


@dataclass
class LotItemRecord:
    """Позиция лота после разбора API."""

    id: int | None = None
    sid: int | None = None
    name: str | None = None
    classification_name: str | None = None
    classification_code: str | None = None
    classification_code_display: str | None = None
    classification_type: int | None = None
    ktru_version_number: int | None = None
    okei_name: str | None = None
    quantity_text: str | None = None
    price_amount: str | None = None
    total_amount: str | None = None
    characteristics: list[LotCharacteristic] = field(default_factory=list)


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_char_values(blob: dict[str, Any]) -> LotCharacteristicValue:
    return LotCharacteristicValue(
        quality_description=_str_or_none(blob.get("QualityDescription")),
        concrete_value=_str_or_none(blob.get("ConcreteValue")),
        okei_name=_str_or_none(blob.get("OkeiName")),
        min_value=_str_or_none(blob.get("MinValue")),
        max_value=_str_or_none(blob.get("MaxValue")),
    )


def _parse_characteristic(ch: dict[str, Any]) -> LotCharacteristic | None:
    if not isinstance(ch, dict):
        return None
    raw_vals = ch.get("CharactericticValues") or ch.get("CharacteristicValues") or []
    if not isinstance(raw_vals, list):
        raw_vals = []
    vals: list[LotCharacteristicValue] = []
    for rv in raw_vals:
        if isinstance(rv, dict):
            vals.append(_parse_char_values(rv))
    return LotCharacteristic(
        name=_str_or_none(ch.get("Name")),
        order_label=_str_or_none(ch.get("OrderNumberString")),
        type_id=_int_or_none(ch.get("KtruCharacteristicTypeId")),
        values=vals,
    )


def _parse_lot_item(item: dict[str, Any]) -> LotItemRecord | None:
    if not isinstance(item, dict):
        return None
    chars_raw = item.get("Characteristics") or []
    chars: list[LotCharacteristic] = []
    if isinstance(chars_raw, list):
        for c in chars_raw:
            if isinstance(c, dict):
                pc = _parse_characteristic(c)
                if pc:
                    chars.append(pc)

    ppt = item.get("PricePerItem") or {}
    total = item.get("TotalSum") or {}
    price_am = ppt.get("AmountInString") or ppt.get("Amount") if isinstance(ppt, dict) else None
    total_am = total.get("AmountInString") or total.get("Amount") if isinstance(total, dict) else None

    return LotItemRecord(
        id=_int_or_none(item.get("ID") or item.get("Id")),
        sid=_int_or_none(item.get("SId")),
        name=_str_or_none(item.get("Name")),
        classification_name=_str_or_none(item.get("ClassificationName")),
        classification_code=_str_or_none(item.get("ClassificationCode")),
        classification_code_display=_str_or_none(item.get("ClassificationCodeForDisplay")),
        classification_type=_int_or_none(item.get("ClassificationType")),
        ktru_version_number=_int_or_none(item.get("KtruVersionNumber")),
        okei_name=_str_or_none(item.get("OkeiName")),
        quantity_text=_str_or_none(item.get("QuantityTextValue") or item.get("Quantity")),
        price_amount=_str_or_none(price_am),
        total_amount=_str_or_none(total_am),
        characteristics=chars,
    )


def parse_lot_items_search_paged_payload(payload: dict[str, Any]) -> list[LotItemRecord]:
    """
    Разбирает тело ответа lot-items/search/paged (верхний уровень: LotItems, TotalRows, …).
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("LotItems")
    if not isinstance(raw, list):
        return []
    out: list[LotItemRecord] = []
    for x in raw:
        if isinstance(x, dict):
            rec = _parse_lot_item(x)
            if rec:
                out.append(rec)
        # развёртка подпозиций из ChildLotItems (если понадобится — рекурсия)
        if isinstance(x, dict):
            nested = x.get("ChildLotItems")
            if isinstance(nested, list):
                for y in nested:
                    if isinstance(y, dict):
                        r2 = _parse_lot_item(y)
                        if r2:
                            out.append(r2)
    return out


def _value_line(v: LotCharacteristicValue) -> str:
    if v.quality_description:
        return v.quality_description
    parts: list[str] = []
    if v.min_value or v.max_value:
        parts.append(f"{v.min_value or ''} … {v.max_value or ''}".strip())
    if v.concrete_value:
        parts.append(v.concrete_value)
    u = "; ".join(p for p in parts if p)
    if not u:
        return "—"
    if v.okei_name:
        return f"{u} ({v.okei_name})"
    return u


def format_lot_item_record_plain(it: LotItemRecord, *, with_header: bool = True) -> str:
    """Текст для лога или уведомления (русские подписи)."""
    lines: list[str] = []
    hdr = []
    if it.name:
        hdr.append(it.name)
    if it.classification_code:
        hdr.append(f"КТРУ {it.classification_code}")
    if hdr and with_header:
        lines.append(": ".join(hdr) if len(hdr) > 1 else hdr[0])
    if it.classification_code_display and it.classification_code_display != it.classification_code:
        lines.append(f"Код для отображения: {it.classification_code_display}")
    if it.classification_name and it.classification_name != it.name:
        lines.append(f"Классификация (наименование): {it.classification_name}")
    if it.okei_name or it.quantity_text:
        q = ", ".join(p for p in (it.quantity_text, it.okei_name) if p)
        if q:
            lines.append(f"Количество: {q}")
    if it.price_amount:
        lines.append(f"Цена за единицу: {it.price_amount}")
    if it.total_amount:
        lines.append(f"НМЦ всего по позиции: {it.total_amount}")
    if it.characteristics:
        lines.append("Характеристики:")
        for ch in it.characteristics:
            nm = ch.name or "—"
            ordn = ch.order_label or ""
            pref = f"  {ordn} {nm}".rstrip()
            vals = "; ".join(_value_line(v) for v in ch.values if v) or "—"
            lines.append(f"{pref}: {vals}")
    return "\n".join(lines) if lines else "(пустая позиция)"


def format_lot_items_response_plain(payload: dict[str, Any]) -> str:
    items = parse_lot_items_search_paged_payload(payload)
    if not items:
        total = payload.get("TotalRows") if isinstance(payload, dict) else None
        return f"(позиций нет в разборе, TotalRows={total!r})"
    blocks = [format_lot_item_record_plain(it) for it in items]
    return "\n\n---\n\n".join(blocks)


def extract_trade_view_urls(html: str) -> list[str]:
    """Ссылки на карточку закупки app — …/supplier/auction/Trade/View.aspx?Id=…"""
    if not html:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for rx in (
        r'(?i)(https://app\.rts-tender\.ru/supplier/auction/Trade/View\.aspx\?[^\s"<>\'`]+)',
        r'(?i)(?:href|src)\s*=\s*"([^"]*?/supplier/auction/Trade/View\.aspx\?[^"]+)"',
        r"(?i)(?:href|src)\s*=\s*'([^']*?/supplier/auction/Trade/View\.aspx\?[^']+)'",
    ):
        for m in re.finditer(rx, html):
            u = html_std.unescape(m.group(1).replace("&amp;", "&").strip())
            if u.startswith("/"):
                u = DEFAULT_ORIGIN + u
            if "/Trade/View.aspx" in u and u not in seen:
                seen.add(u)
                out.append(u)
    return out


def discover_trade_numeric_id_from_html(page_html: str) -> int | None:
    """
    Вытащить числовой TradeId из HTML/скриптов страницы Trade/View или загрузчиков SPA.
    """
    if not page_html:
        return None
    for pat in (
        r'"TradeId"\s*:\s*(\d{1,12})',
        r"'TradeId'\s*:\s*(\d{1,12})",
        r"(?i)TradeId\s*=\s*(\d{1,12})\b",
        r'(?i)"tradeId"\s*:\s*(\d{1,12})',
        r"(?i)data-trade-id\s*=\s*[\"'](\d{1,12})[\"']",
    ):
        m = re.search(pat, page_html)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def discover_trade_numeric_id_from_auction_view(
    session: requests.Session,
    trade_view_absolute_url: str,
    *,
    timeout: float,
) -> int | None:
    """GET Trade/View.aspx и поиск числового TradeId в теле страницы."""
    url = trade_view_absolute_url.strip()
    if not url.startswith("http"):
        url = DEFAULT_ORIGIN.rstrip("/") + "/" + url.lstrip("/")
    r = session.get(
        url,
        headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.9,*/*;q=0.8"},
        timeout=timeout,
    )
    if r.status_code != 200:
        return None
    return discover_trade_numeric_id_from_html(r.text)


def fetch_lot_items_search_paged(
    session: requests.Session,
    trade_numeric_id: int,
    *,
    page_number: int = 1,
    page_length: int = DEFAULT_LOT_ITEMS_PAGE_LENGTH,
    timeout: float = 60.0,
    referer_page: str = DEFAULT_APP_REFERER,
) -> dict[str, Any]:
    """POST …/lot-items/search/paged — нужна та же Cookie, что для ЛК поставщика."""
    origin = str(session.headers.get("Origin") or DEFAULT_ORIGIN).rstrip("/") or DEFAULT_ORIGIN
    api_url = origin + LOT_ITEMS_SEARCH_PAGED_PATH
    body = {"TradeId": int(trade_numeric_id), "PageNumber": page_number, "PageLength": page_length}
    r = session.post(
        api_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": origin,
            "Referer": f"{origin}/supplier/lk/{referer_page}",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=timeout,
    )
    if r.status_code != 200:
        text = (r.text or "")[:400].lower()
        if "anti-ddos" in text or "<html" in text:
            raise RuntimeError(f"lot-items анти-DDoS HTTP {r.status_code} ({origin})")
        raise RuntimeError(f"lot-items HTTP {r.status_code}: {(r.text or '')[:400]}")
    try:
        parsed = json.loads(r.text)
    except json.JSONDecodeError as ex:
        raise RuntimeError(f"lot-items ответ не JSON: {ex}") from ex
    if not isinstance(parsed, dict):
        raise RuntimeError("lot-items ответ не объект JSON")
    return parsed


def resolve_trade_and_fetch_first_lot_page(
    session: requests.Session,
    *,
    html_from_message_body: str,
    timeout: float,
    page_length: int = DEFAULT_LOT_ITEMS_PAGE_LENGTH,
) -> tuple[int | None, dict[str, Any] | None, str]:
    """
    Из HTML письма → первая ссылка Trade/View → числовой TradeId → POST lot-items/page 1.
    Возвращает (trade_id, json_response_or_none, текст_ошибки_или_пусто).
    """
    urls = extract_trade_view_urls(html_from_message_body)
    if not urls:
        return None, None, "(нет ссылок Trade/View.aspx в тексте сообщения)"
    last_err = ""
    for u in urls:
        tid = discover_trade_numeric_id_from_auction_view(session, u, timeout=timeout)
        if tid is None:
            last_err = f"(не нашли числовой TradeId на странице {u[:120]}…)"
            continue
        try:
            payload = fetch_lot_items_search_paged(
                session, tid, page_number=1, page_length=page_length, timeout=timeout
            )
            return tid, payload, ""
        except RuntimeError as ex:
            last_err = str(ex)
            continue
    return None, None, last_err or "(lot-items недоступен)"

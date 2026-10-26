"""RSS polling for public EIS extended search results."""

from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from eis_analytics_db import record_rss_ktru_hints, record_rss_poll_run

DEFAULT_RSS_URL = (
    "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html?"
    "morphology=on&pageNumber=1&sortDirection=false&recordsPerPage=_10&"
    "showLotsInfoHidden=false&sortBy=UPDATE_DATE&fz44=on&"
    "placingWayList=EZT20%2CEZTP20&selectedLaws=FZ44&currencyIdGeneral=-1&"
    "oktmoIdsWithNested=on&oktmoIds=19000000&oktmoIdsCodes=19000000&"
    "gws=%D0%92%D1%8B%D0%B1%D0%B5%D1%80%D0%B8%D1%82%D0%B5+%D1%82%D0%B8%D0%BF+%D0%B7%D0%B0%D0%BA%D1%83%D0%BF%D0%BA%D0%B8"
)
DEFAULT_QUERY_LABEL = "ezt20_35_oktmo_19000000"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; rts-tender-rss/1.0)"


@dataclass
class EisRssConfig:
    rss_url: str = DEFAULT_RSS_URL
    timeout: float = 30.0
    query_label: str = DEFAULT_QUERY_LABEL
    user_agent: str = DEFAULT_USER_AGENT


@dataclass
class EisRssItem:
    reg_number: str
    title: str = ""
    link: str = ""
    author: str = ""
    pub_date_utc: str = ""
    description_html: str = ""
    procedure_name: str = ""
    customer_name: str = ""
    price_text: str = ""
    stage_text: str = ""
    ikz: str = ""
    etp_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "reg_number": self.reg_number,
            "title": self.title,
            "link": self.link,
            "author": self.author,
            "pub_date_utc": self.pub_date_utc,
            "description_html": self.description_html,
            "procedure_name": self.procedure_name,
            "customer_name": self.customer_name,
            "price_text": self.price_text,
            "stage_text": self.stage_text,
            "ikz": self.ikz,
            "etp_name": self.etp_name,
        }


@dataclass
class EisRssPollResult:
    ok: bool
    total_items: int = 0
    new_items: int = 0
    errors: list[str] = field(default_factory=list)
    items: list[EisRssItem] = field(default_factory=list)
    run_id: int | None = None


_REG_RE = re.compile(r"regNumber=(\d+)|№\s*(\d{10,})")
_TAG_RE = re.compile(r"<[^>]+>")
_CACHE_BUSTER_PARAM = "_rts_ts"


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _inner_xml(el: ET.Element | None) -> str:
    if el is None:
        return ""
    parts = [el.text or ""]
    for child in list(el):
        parts.append(ET.tostring(child, encoding="unicode", method="html"))
    return "".join(parts).strip()


def _rss_xml_body(raw: str) -> str:
    idx = raw.find("<rss")
    body = raw[idx:] if idx >= 0 else raw
    return re.sub(r"&(?!#?\w+;)", "&amp;", body)


def parse_rss_datetime(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        return _utc_iso(parsedate_to_datetime(s))
    except (TypeError, ValueError, IndexError, OverflowError):
        return ""


def extract_reg_number(title: str, link: str, description_html: str) -> str:
    for text in (link, title, description_html):
        m = _REG_RE.search(text or "")
        if not m:
            continue
        return (m.group(1) or m.group(2) or "").strip()
    return ""


def _description_plain(description_html: str) -> str:
    normalized = re.sub(r"<br\s*/?>", "\n", description_html or "", flags=re.I)
    without_tags = _TAG_RE.sub("", normalized)
    return html.unescape(without_tags).replace("\xa0", " ")


def _field_after_label(description_html: str, label: str) -> str:
    plain = _description_plain(description_html)
    marker = f"{label}:"
    idx = plain.find(marker)
    if idx < 0:
        return ""
    rest = plain[idx + len(marker) :].strip()
    lines = [x.strip() for x in rest.splitlines() if x.strip()]
    value = lines[0] if lines else ""
    if label == "Начальная цена контракта":
        value = value.split("Валюта:", 1)[0].strip()
    return value


def _procedure_name(description_html: str) -> str:
    plain = _description_plain(description_html)
    marker = "Найденный результат:"
    idx = plain.find(marker)
    if idx < 0:
        return ""
    rest = plain[idx + len(marker) :].strip()
    line = next((x.strip() for x in rest.splitlines() if x.strip()), "")
    return re.sub(r"\s+№.*$", "", line).strip()


def rss_url_with_cache_buster(url: str, *, ts: int | None = None) -> str:
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != _CACHE_BUSTER_PARAM]
    stamp = str(ts if ts is not None else int(datetime.now(timezone.utc).timestamp()))
    pairs.append((_CACHE_BUSTER_PARAM, stamp))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, doseq=True), parts.fragment))


def rss_url_for_ktru(base_url: str, ktru_code: str, *, records_per_page: str = "_50") -> str:
    """Same EIS RSS search, restricted to one KTRU classifier code (no HTML fetch)."""
    code = str(ktru_code or "").strip()
    parts = urlsplit(str(base_url or "").strip() or DEFAULT_RSS_URL)
    drop = {
        _CACHE_BUSTER_PARAM,
        "kos",
        "ktruIdsCodes",
        "ktruIds",
        "searchString",
        "recordsPerPage",
    }
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in drop]
    if code:
        pairs.append(("kos", "on"))
        pairs.append(("ktruIdsCodes", code))
    page_size = str(records_per_page or "_50").strip() or "_50"
    if not page_size.startswith("_"):
        page_size = f"_{page_size}"
    pairs.append(("recordsPerPage", page_size))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, doseq=True), parts.fragment))


def parse_rss_items(xml_text: str) -> list[EisRssItem]:
    body = _rss_xml_body(xml_text)
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return _parse_rss_items_loose(body)
    items: list[EisRssItem] = []
    for item in root.findall(".//item"):
        title = _text(item.find("title"))
        link = _text(item.find("link"))
        description = _inner_xml(item.find("description"))
        author = _text(item.find("author"))
        pub_date = parse_rss_datetime(_text(item.find("pubDate")))
        reg = extract_reg_number(title, link, description)
        if not reg:
            continue
        items.append(
            EisRssItem(
                reg_number=reg,
                title=title,
                link=link,
                author=author,
                pub_date_utc=pub_date,
                description_html=description,
                procedure_name=_procedure_name(description),
                customer_name=_field_after_label(description, "Наименование Заказчика"),
                price_text=_field_after_label(description, "Начальная цена контракта"),
                stage_text=_field_after_label(description, "Этап размещения"),
                ikz=_field_after_label(description, "Идентификационный код закупки (ИКЗ)"),
                etp_name=(
                    _field_after_label(description, "Площадка")
                    or _field_after_label(description, "Электронная площадка")
                    or _field_after_label(description, "Наименование электронной площадки")
                ),
            )
        )
    return items


def _tag_value(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, flags=re.I | re.S)
    return html.unescape(m.group(1).strip()) if m else ""


def _parse_rss_items_loose(xml_text: str) -> list[EisRssItem]:
    items: list[EisRssItem] = []
    for m in re.finditer(r"<item\b[^>]*>(.*?)</item>", xml_text, flags=re.I | re.S):
        block = m.group(1)
        title = _description_plain(_tag_value(block, "title")).strip()
        link = _description_plain(_tag_value(block, "link")).strip()
        description = _tag_value(block, "description")
        author = _description_plain(_tag_value(block, "author")).strip()
        pub_date = parse_rss_datetime(_description_plain(_tag_value(block, "pubDate")).strip())
        reg = extract_reg_number(title, link, description)
        if not reg:
            continue
        items.append(
            EisRssItem(
                reg_number=reg,
                title=title,
                link=link,
                author=author,
                pub_date_utc=pub_date,
                description_html=description,
                procedure_name=_procedure_name(description),
                customer_name=_field_after_label(description, "Наименование Заказчика"),
                price_text=_field_after_label(description, "Начальная цена контракта"),
                stage_text=_field_after_label(description, "Этап размещения"),
                ikz=_field_after_label(description, "Идентификационный код закупки (ИКЗ)"),
                etp_name=(
                    _field_after_label(description, "Площадка")
                    or _field_after_label(description, "Электронная площадка")
                    or _field_after_label(description, "Наименование электронной площадки")
                ),
            )
        )
    return items


def rss_requests_verify() -> bool:
    """Verify TLS cert when fetching RSS.

    Set EIS_RSS_VERIFY_SSL=0 to disable. When HTTPS_PROXY/HTTP_PROXY is set and the
    env var is unset, verification is disabled (typical VPS → tinyproxy → zakupki path).
    """
    raw = os.environ.get("EIS_RSS_VERIFY_SSL", "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    if os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"):
        return False
    return True


def fetch_rss_items(cfg: EisRssConfig) -> list[EisRssItem]:
    resp = requests.get(
        rss_url_with_cache_buster(cfg.rss_url),
        timeout=cfg.timeout,
        verify=rss_requests_verify(),
        headers={
            "User-Agent": cfg.user_agent,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return parse_rss_items(resp.text)


def run_eis_rss_poll(
    cfg: EisRssConfig,
    db_path: Path,
    *,
    update_query_label: bool = True,
) -> EisRssPollResult:
    started = datetime.now(timezone.utc).replace(microsecond=0)
    errors: list[str] = []
    items: list[EisRssItem] = []
    try:
        items = fetch_rss_items(cfg)
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
    finished = datetime.now(timezone.utc).replace(microsecond=0)
    run_id, new_count = record_rss_poll_run(
        db_path,
        [x.as_dict() for x in items],
        query_label=cfg.query_label,
        rss_url=cfg.rss_url,
        started_at_utc=started,
        finished_at_utc=finished,
        ok=not errors,
        errors=errors,
        update_query_label=update_query_label,
    )
    return EisRssPollResult(
        ok=not errors,
        total_items=len(items),
        new_items=new_count,
        errors=errors,
        items=items,
        run_id=run_id,
    )


def run_eis_rss_catalog_ktru_poll(cfg: EisRssConfig, db_path: Path, ktru_code: str) -> EisRssPollResult:
    code = str(ktru_code or "").strip()
    labeled = EisRssConfig(
        rss_url=rss_url_for_ktru(cfg.rss_url, code),
        timeout=cfg.timeout,
        query_label=f"catalog_ktru:{code}",
        user_agent=cfg.user_agent,
    )
    res = run_eis_rss_poll(labeled, db_path, update_query_label=False)
    if res.ok and code:
        record_rss_ktru_hints(db_path, [x.reg_number for x in res.items], code)
    return res

"""Fetch public EIS notice common-info HTML pages."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

USER_AGENT = "Mozilla/5.0 (compatible; rts-tender-eis-html/1.0)"

NOTICE_COMMON_INFO_URL = (
    "https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html"
)
_REG_IN_URL_RE = re.compile(r"regNumber=(\d+)", re.I)
_HOST = "zakupki.gov.ru"


def normalize_eis_notice_link(link: str, *, reg_number: str = "") -> str:
    """Fix mangled EIS notice URLs (e.g. duplicated host / missing ``:`` after https).

    Prefer a canonical common-info URL when ``reg_number`` is known.
    """
    raw = str(link or "").strip()
    pn = str(reg_number or "").strip()
    if not pn:
        m = _REG_IN_URL_RE.search(raw)
        pn = m.group(1) if m else ""
    if pn:
        return f"{NOTICE_COMMON_INFO_URL}?regNumber={pn}"

    fixed = raw.replace("https//", "https://").replace("http//", "http://")
    # Collapse accidental double host: https://zakupki.gov.ruhttps://zakupki.gov.ru/...
    while fixed.lower().count(_HOST) > 1:
        idx = fixed.lower().rfind(_HOST)
        tail = fixed[idx + len(_HOST) :]
        if not tail.startswith("/"):
            tail = "/" + tail.lstrip("/")
        fixed = f"https://{_HOST}{tail}"
    if fixed.startswith("//"):
        return "https:" + fixed
    if fixed.startswith("/"):
        return f"https://{_HOST}{fixed}"
    if fixed and not fixed.lower().startswith(("http://", "https://")):
        return f"https://{_HOST}/{fixed.lstrip('/')}"
    return fixed


def _cache_busted_url(url: str) -> str:
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_rts_ts"]
    pairs.append(("_rts_ts", str(int(datetime.now(timezone.utc).timestamp()))))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, doseq=True), parts.fragment))


def fetch_notice_common_info_html(url: str, *, timeout: float = 30.0) -> tuple[str, int]:
    normalized = normalize_eis_notice_link(url)
    resp = requests.get(
        _cache_busted_url(normalized),
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://zakupki.gov.ru/",
        },
    )
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text, int(resp.status_code)

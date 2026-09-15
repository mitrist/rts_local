"""Fetch public EIS notice common-info HTML pages."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

USER_AGENT = "Mozilla/5.0 (compatible; rts-tender-eis-html/1.0)"


def _cache_busted_url(url: str) -> str:
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_rts_ts"]
    pairs.append(("_rts_ts", str(int(datetime.now(timezone.utc).timestamp()))))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, doseq=True), parts.fragment))


def fetch_notice_common_info_html(url: str, *, timeout: float = 30.0) -> tuple[str, int]:
    resp = requests.get(
        _cache_busted_url(url),
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

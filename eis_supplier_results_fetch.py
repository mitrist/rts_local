"""Fetch public EIS notice supplier-results HTML pages."""

from __future__ import annotations

from eis_notice_html_fetch import USER_AGENT, _cache_busted_url
from eis_rss_poll import rss_requests_verify

import requests


def notice_supplier_results_url(purchase_number: str) -> str:
    pn = str(purchase_number or "").strip()
    return (
        "https://zakupki.gov.ru/epz/order/notice/ezt20/view/supplier-results.html"
        f"?regNumber={pn}"
    )


def fetch_notice_supplier_results_html(url: str, *, timeout: float = 30.0) -> tuple[str, int]:
    resp = requests.get(
        _cache_busted_url(url),
        timeout=timeout,
        verify=rss_requests_verify(),
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

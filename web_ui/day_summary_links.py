"""Deep links на «Подбор предложений» с автoоткрытием модалки сравнения."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from eis_analytics_db import parse_publish_datetime_utc

DEFAULT_PUBLIC_BASE_URL = "https://tender.nacpro-web-service.ru"


def web_public_base_url() -> str:
    for name in ("RTS_WEB_PUBLIC_BASE_URL", "RTS_PUBLIC_BASE_URL"):
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw.rstrip("/")
    return DEFAULT_PUBLIC_BASE_URL.rstrip("/")


def summary_date_from_row(row: dict[str, Any]) -> date:
    """Календарный день (MSK) для фильтра «Итоги дня»."""
    from eis_changelog import MSK

    for key in ("pub_date_utc", "fetched_at_utc", "parsed_at_utc"):
        raw = str(row.get(key) or "").strip()
        if not raw:
            continue
        dt = parse_publish_datetime_utc(raw)
        if dt is not None:
            return dt.astimezone(MSK).date()
    return datetime.now(MSK).date()


def build_offer_matching_compare_url(
    *,
    purchase_number: str,
    line_key: str,
) -> str:
    pn = (purchase_number or "").strip()
    lk = (line_key or "").strip()
    if not pn or not lk:
        return ""
    query = urlencode(
        {
            "purchase": pn,
            "line_key": lk,
            "open_compare": "1",
        }
    )
    return f"{web_public_base_url()}/offer-matching?{query}"


def build_day_summary_compare_url(
    *,
    purchase_number: str,
    line_key: str,
    summary_date: date | None = None,
) -> str:
    """Обратная совместимость: ссылки ведут на /offer-matching."""
    del summary_date
    return build_offer_matching_compare_url(
        purchase_number=purchase_number,
        line_key=line_key,
    )


def build_pp_monitor_sample_url(offer_price_list_item_id: str) -> str:
    oid = str(offer_price_list_item_id or "").strip()
    if not oid:
        return ""
    return f"{web_public_base_url()}/pp-monitor-samples/{oid}"

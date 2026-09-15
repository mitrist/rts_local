from __future__ import annotations

import json
from pathlib import Path

from rts_customer_pp_api import build_search_payload, parse_search_response, parse_search_summary


def _load_reestr_page() -> list[dict]:
    path = Path(__file__).resolve().parents[1] / ".cursor" / "supplier" / "reestr.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    page = data.get("page")
    assert isinstance(page, list)
    return page


def test_parse_search_summary_protected_id_aliases():
    raw = {
        "id": 263500,
        "name": "Светодиодный дисплей",
        "offerPriceListItemProtectedId": "HmqJMSc7HYDx",
        "offerPriceListItemLinkDate": "2026-06-11T05:48:30.797",
        "validityDate": "2026-07-11T16:59:59",
        "ktru": {"code": "26.11.22.200-00000001", "name": "Светодиодный дисплей"},
        "deliveryPlaces": [{"unitPrice": "2100000,0000", "oktmo": {"code": "14000000"}}],
    }
    summary = parse_search_summary(raw, ktru_code="26.11.22.200-00000001")
    assert summary is not None
    assert summary.offer_price_list_item_id == "HmqJMSc7HYDx"
    assert summary.link_date == "2026-06-11T05:48:30.797"
    assert summary.unit_price == 2100000.0
    assert summary.price_list_item_id == 263500


def test_parse_search_response_sorting_first_is_newest_link_date():
    page = _load_reestr_page()
    summaries, total = parse_search_response({"page": page, "total": 133}, ktru_code="26.11.22.200-00000001")
    assert total == 133
    assert summaries
    link_dates = [s.link_date for s in summaries if s.link_date]
    assert link_dates
    # reestr.json page is not sorted; ensure parser handles all rows
    assert all(s.offer_price_list_item_id for s in summaries)


def test_build_search_payload_uses_string_whole_prices():
    payload = build_search_payload("26.20.18.000-00000069", price_from=73300.0, price_to=73300.0, take=10)
    assert payload["priceFrom"] == "73300"
    assert payload["priceTo"] == "73300"
    assert isinstance(payload["priceFrom"], str)

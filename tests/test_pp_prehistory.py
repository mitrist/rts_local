from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eis_analytics_db import (
    connect,
    init_db,
    record_notice_html_page,
    upsert_pp_eis_match,
    upsert_pp_monitor_finding,
    upsert_pp_monitor_registry_row,
)
from pp_prehistory import find_predecessors, notice_region_matches_pp_place
from pp_stack import find_characteristics_fingerprint


def _seed_finding(
    db: Path,
    *,
    oid: str,
    ktru: str,
    link_date: str,
    unit_price: float,
    oktmo_code: str,
    oktmo_name: str,
    chars: list[dict] | None = None,
) -> str:
    find_item = {
        "offerPriceListItemId": oid,
        "createDateTime": link_date,
        "tradeMark": "TM",
        "ktru": {"code": ktru, "name": "Test"},
        "ktruCharacteristics": chars or [{"code": "c1", "name": "X", "characteristicValues": [{"value": "1"}]}],
        "deliveryPlaces": [
            {
                "oktmo": {"code": oktmo_code, "name": oktmo_name},
                "unitPrice": f"{unit_price:.4f}".replace(".", ","),
            }
        ],
    }
    find_json = json.dumps(find_item, ensure_ascii=False)
    chash, _ = find_characteristics_fingerprint(find_item)
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id=oid,
        ktru_code=ktru,
        name="Test",
        trade_mark="TM",
        link_date=link_date,
        unit_price=unit_price,
        card_url=f"https://offers.rts-tender.ru/customer/offers/offer-item/{oid}",
    )
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id=oid,
        find_json=find_json,
        ktru_code=ktru,
        trade_mark="TM",
        link_date=link_date,
        unit_price=unit_price,
        source="test",
        trigger_recompute=False,
    )
    return chash


def test_notice_region_matches_by_oktmo_code():
    notice_ctx = {"region": "Республика Марий Эл", "delivery_place": "88701000001 г Йошкар-Ола", "oktmo_code": "88701000001"}
    place = {"oktmo": {"code": "88701000001", "name": "г Йошкар-Ола"}}
    ok, label = notice_region_matches_pp_place(notice_ctx, place)
    assert ok is True
    assert "Йошкар" in label


def test_notice_region_matches_by_substring():
    notice_ctx = {"region": "Республика Марий Эл", "delivery_place": "Йошкар-Ола", "oktmo_code": ""}
    place = {"oktmo": {"code": "88701000001", "name": "г Йошкар-Ола"}}
    ok, _ = notice_region_matches_pp_place(notice_ctx, place)
    assert ok is True


def test_find_predecessors_filters_hash_price_region_time(tmp_path: Path):
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    ktru = "26.20.13.000-00000002"
    record_notice_html_page(
        db,
        purchase_number="PN-1",
        source_url="https://example",
        html_text="<html></html>",
        lines=[],
        fetched_at_utc=datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc),
        ok=True,
        page_context={
            "region": "Республика Марий Эл",
            "delivery_place": "г Йошкар-Ола",
            "contract_start_date": "",
            "contract_execution_term": "",
            "delivery_days": "",
        },
    )
    chars = [{"code": "c1", "name": "X", "characteristicValues": [{"value": "1"}]}]
    chash = _seed_finding(
        db,
        oid="old1",
        ktru=ktru,
        link_date="2026-07-01T10:00:00Z",
        unit_price=50000.0,
        oktmo_code="88701000001",
        oktmo_name="г Йошкар-Ола",
        chars=chars,
    )
    _seed_finding(
        db,
        oid="old_wrong_price",
        ktru=ktru,
        link_date="2026-07-02T10:00:00Z",
        unit_price=51000.0,
        oktmo_code="88701000001",
        oktmo_name="г Йошкар-Ола",
        chars=chars,
    )
    _seed_finding(
        db,
        oid="anchor",
        ktru=ktru,
        link_date="2026-07-08T10:00:00Z",
        unit_price=50000.0,
        oktmo_code="88701000001",
        oktmo_name="г Йошкар-Ола",
        chars=chars,
    )
    upsert_pp_eis_match(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        offer_price_list_item_id="anchor",
        ktru_code=ktru,
        stack_key=f"{ktru}:{chash}",
        matched_count=1,
        total_compared=1,
        is_exact_match=True,
        pp_link_date="2026-07-08T10:00:00Z",
    )
    match = {
        "purchase_number": "PN-1",
        "line_key": "lk1",
        "offer_price_list_item_id": "anchor",
        "ktru_code": ktru,
        "stack_key": f"{ktru}:{chash}",
        "pp_link_date": "2026-07-08T10:00:00Z",
    }
    result = find_predecessors(db, match)
    assert result.has_prehistory is True
    assert result.predecessor_count == 2
    ids = {p.offer_price_list_item_id for p in result.predecessors}
    assert ids == {"old1", "old_wrong_price"}

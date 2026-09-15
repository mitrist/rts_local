from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from eis_analytics_db import (
    ANALYTICS_SCHEMA,
    connect,
    count_pp_monitor_registry,
    display_compare_datetime_msk,
    display_rts_datetime_msk,
    get_pp_monitor_finding,
    init_db,
    list_known_pp_monitor_ids,
    list_pp_monitor_findings,
    list_pp_monitor_journal_with_findings,
    list_pp_monitor_recent,
    list_pp_monitor_registry,
    list_pp_monitor_registry_ktru_codes,
    list_pp_monitor_registry_names,
    record_pp_monitor_notification,
    upsert_pp_monitor_finding,
    upsert_pp_monitor_registry_row,
)
from rts_pp_monitor_settings import default_settings, parse_settings, registry_highlight_since


def test_pp_monitor_schema_v14(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('rts_pp_monitor_findings', 'rts_pp_monitor_registry')"
            ).fetchall()
        }
        finding_cols = {
            str(r[1]) for r in conn.execute("PRAGMA table_info(rts_pp_monitor_findings)").fetchall()
        }
        registry_cols = {
            str(r[1]) for r in conn.execute("PRAGMA table_info(rts_pp_monitor_registry)").fetchall()
        }
    assert int(row[0]) >= ANALYTICS_SCHEMA
    assert ANALYTICS_SCHEMA == 19
    assert "rts_pp_monitor_findings" in tables
    assert "rts_pp_monitor_registry" in tables
    assert "characteristics_hash" in finding_cols
    assert "characteristics_hash" in registry_cols


def test_pp_monitor_registry_upsert(tmp_path: Path):
    from eis_analytics_db import get_pp_monitor_registry_row

    db = tmp_path / "analytics.sqlite3"
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="reg1",
        ktru_code="26.20.13.000-00000002",
        name="Панель",
        link_date="2026-07-01T10:00:00",
        unit_price=100000.0,
    )
    row = get_pp_monitor_registry_row(db, "reg1")
    assert row is not None
    assert row["name"] == "Панель"
    assert row["unit_price"] == 100000.0


def test_pp_monitor_registry_list_and_ktru_filter(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="a1",
        ktru_code="26.20.13.000-00000002",
        name="A",
        link_date="2026-07-02T10:00:00",
        price_list_item_id=100,
        unit_price=100000.0,
    )
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="b1",
        ktru_code="31.02.10.000-00000004",
        name="B",
        link_date="2026-07-01T10:00:00",
        price_list_item_id=200,
        unit_price=50000.0,
    )
    codes = list_pp_monitor_registry_ktru_codes(db)
    assert codes == ["26.20.13.000-00000002", "31.02.10.000-00000004"]
    all_rows = list_pp_monitor_registry(db, limit=10)
    assert len(all_rows) == 2
    assert all_rows[0]["offer_price_list_item_id"] == "a1"
    assert all_rows[0]["link_date_display_msk"]
    filtered = list_pp_monitor_registry(db, ktru_code="31.02.10.000-00000004")
    assert len(filtered) == 1
    assert filtered[0]["name"] == "B"
    assert count_pp_monitor_registry(db) == 2
    assert count_pp_monitor_registry(db, name_query="пан") == 0
    by_name = list_pp_monitor_registry(db, name_query="A")
    assert len(by_name) == 1
    assert by_name[0]["has_find"] is False
    names = list_pp_monitor_registry_names(db)
    assert names == ["A", "B"]
    by_names = list_pp_monitor_registry(db, name_values=["B"])
    assert len(by_names) == 1
    assert by_names[0]["offer_price_list_item_id"] == "b1"
    by_multi = list_pp_monitor_registry(db, name_values=["A", "B"])
    assert len(by_multi) == 2
    by_price = list_pp_monitor_registry(db, unit_price_min=80000.0)
    assert len(by_price) == 1
    assert by_price[0]["offer_price_list_item_id"] == "a1"
    by_range = list_pp_monitor_registry(db, unit_price_min=40000.0, unit_price_max=60000.0)
    assert len(by_range) == 1
    assert by_range[0]["offer_price_list_item_id"] == "b1"
    assert count_pp_monitor_registry(db, unit_price_max=70000.0) == 1


def test_pp_monitor_registry_highlight_since(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="old1",
        ktru_code="26.20.13.000-00000002",
        name="Old",
        seen_at_utc=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
    )
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="new1",
        ktru_code="26.20.13.000-00000002",
        name="New",
        seen_at_utc=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
    )
    rows = list_pp_monitor_registry(db, highlight_since_utc="2026-07-06T11:00:00Z")
    by_id = {r["offer_price_list_item_id"]: r for r in rows}
    assert by_id["old1"]["is_latest_pp_new"] is False
    assert by_id["new1"]["is_latest_pp_new"] is True


def test_registry_highlight_since_settings():
    assert registry_highlight_since({}) is None
    assert registry_highlight_since({"last_new_poll_items_new": 0}) is None
    assert (
        registry_highlight_since(
            {
                "last_new_poll_items_new": 2,
                "last_new_poll_started_at_utc": "2026-07-06T10:00:00Z",
            }
        )
        == "2026-07-06T10:00:00Z"
    )


def test_pp_monitor_notification_upsert(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    record_pp_monitor_notification(
        db,
        offer_price_list_item_id="abc123",
        ktru_code="26.20.13.000-00000002",
        channel="telegram_rts_pp_monitor",
        status="seeded",
        card_url="https://example.test/item/abc123",
    )
    assert "abc123" in list_known_pp_monitor_ids(db)
    record_pp_monitor_notification(
        db,
        offer_price_list_item_id="abc123",
        ktru_code="26.20.13.000-00000002",
        channel="telegram_rts_pp_monitor",
        status="sent",
        unit_price=100000.0,
    )
    rows = list_pp_monitor_recent(db, limit=5)
    assert rows[0]["status"] == "sent"
    assert rows[0]["unit_price"] == 100000.0


def test_pp_monitor_journal_dedupes_channels(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    for channel in ("telegram_rts_pp_monitor", "max_rts_pp_monitor"):
        record_pp_monitor_notification(
            db,
            offer_price_list_item_id="dup1",
            ktru_code="31.02.10.000-00000004",
            channel=channel,
            status="seeded",
            unit_price=5060.0,
        )
    rows = list_pp_monitor_recent(db, limit=10)
    assert len(rows) == 1
    assert rows[0]["offer_price_list_item_id"] == "dup1"


def test_pp_monitor_finding_upsert_and_list(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    find_payload = {"offerPriceListItemId": "oid1", "ktruCharacteristics": [{"name": "A", "characteristicValues": []}]}
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="oid1",
        find_json=json.dumps(find_payload),
        ktru_code="26.20.13.000-00000002",
        trade_mark="TM",
        unit_price=99000.0,
        source="notify",
    )
    row = get_pp_monitor_finding(db, "oid1")
    assert row is not None
    assert row["find"]["offerPriceListItemId"] == "oid1"
    assert row["fetched_display_msk"]
    listed = list_pp_monitor_findings(db, limit=10)
    assert len(listed) == 1
    assert listed[0]["ktru_code"] == "26.20.13.000-00000002"

    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="oid1",
        find_json=json.dumps({**find_payload, "tradeMark": "updated"}),
        ktru_code="26.20.13.000-00000002",
        source="admin_fetch",
    )
    row2 = get_pp_monitor_finding(db, "oid1")
    assert row2 is not None
    assert row2["source"] == "admin_fetch"
    assert row2["find"]["tradeMark"] == "updated"


def test_pp_monitor_finding_row_enrichment(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="enriched",
        find_json=json.dumps(
            {
                "ktru": {"code": "26.20.13.000-00000002", "name": "Панель"},
                "offerPriceListItemLinkDate": "2026-07-01T08:00:00",
                "validityDate": "0001-01-01T00:00:00",
                "offer": {"validityDate": "2026-08-02T19:59:59"},
                "deliveryPlaces": [
                    {"oktmo": {"code": "11000000"}},
                    {"oktmo": {"code": "12000000"}},
                ],
            }
        ),
        ktru_code="26.20.13.000-00000002",
        link_date="2026-07-01T08:00:00",
    )
    row = get_pp_monitor_finding(db, "enriched")
    assert row is not None
    assert row["ktru_name"] == "Панель"
    assert row["added_display_msk"] == display_compare_datetime_msk("2026-07-01T08:00:00")
    assert row["validity_display_msk"] == display_compare_datetime_msk("2026-08-02T19:59:59")
    assert row["delivery_regions_count"] == 2


def test_pp_monitor_journal_with_findings_flag(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    record_pp_monitor_notification(
        db,
        offer_price_list_item_id="with_find",
        ktru_code="26.20.13.000-00000002",
        channel="telegram_rts_pp_monitor",
        status="sent",
        unit_price=100000.0,
    )
    record_pp_monitor_notification(
        db,
        offer_price_list_item_id="no_find",
        ktru_code="26.20.13.000-00000002",
        channel="telegram_rts_pp_monitor",
        status="seeded",
        unit_price=50000.0,
    )
    record_pp_monitor_notification(
        db,
        offer_price_list_item_id="other_ktru",
        ktru_code="31.02.10.000-00000004",
        channel="telegram_rts_pp_monitor",
        status="sent",
        unit_price=100000.0,
    )
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="with_find",
        find_json=json.dumps(
            {
                "name": "Светодиодный дисплей",
                "createDateTime": "2026-07-02T12:17:02",
                "validityDate": "2026-08-02T19:59:59",
                "offerPriceListItemLinkDate": "2026-07-02T12:17:05",
                "deliveryPlaces": [{}, {}, {}],
                "ktruCharacteristics": [],
            }
        ),
        ktru_code="26.20.13.000-00000002",
    )
    settings = parse_settings(
        {
            **default_settings(),
            "ktru_codes": ["26.20.13.000-00000002"],
            "filters": {"price_min": 90000.0, "price_max": None, "region_ids": [], "skip_expired": True},
        }
    )
    rows = list_pp_monitor_journal_with_findings(db, limit=10, settings=settings)
    ids = {r["offer_price_list_item_id"] for r in rows}
    assert ids == {"with_find"}
    row = rows[0]
    assert row["has_find"] is True
    assert row["create_display_msk"] == display_rts_datetime_msk("2026-07-02T12:17:02")
    assert row["validity_display_msk"] == display_rts_datetime_msk("2026-08-02T19:59:59")
    assert row["link_date_display_msk"] == display_rts_datetime_msk("2026-07-02T12:17:05")
    assert row["create_sort_key"] == "2026-07-02T12:17:02"
    assert row["item_name"] == "Светодиодный дисплей"
    assert row["delivery_places_count"] == 3

    all_rows = list_pp_monitor_journal_with_findings(db, limit=10)
    assert len(all_rows) == 3
    by_id = {r["offer_price_list_item_id"]: r for r in all_rows}
    assert by_id["with_find"]["has_find"] is True
    assert by_id["no_find"]["has_find"] is False


def test_pp_monitor_finding_sets_characteristics_hash(tmp_path: Path):
    from eis_analytics_db import get_pp_monitor_registry_row

    db = tmp_path / "analytics.sqlite3"
    find_json = json.dumps(
        {
            "ktruCharacteristics": [
                {"code": "x", "characteristicValues": [{"concreteValue": "1"}]},
            ]
        }
    )
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="h1",
        ktru_code="26.20.13.000-00000002",
        link_date="2026-07-01T10:00:00",
    )
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="h1",
        find_json=find_json,
        ktru_code="26.20.13.000-00000002",
    )
    finding = get_pp_monitor_finding(db, "h1")
    reg = get_pp_monitor_registry_row(db, "h1")
    assert finding is not None
    assert reg is not None
    assert finding.get("characteristics_hash")
    assert reg.get("characteristics_hash") == finding.get("characteristics_hash")


def _seed_registry_stack_pair(db: Path) -> None:
    ktru = "26.20.13.000-00000002"
    find_base = {
        "ktru": {"code": ktru, "name": "Панель"},
        "ktruCharacteristics": [
            {
                "code": "char-1",
                "characteristicValues": [{"qualityDescription": "В помещении"}],
            }
        ],
    }
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="comp1",
        ktru_code=ktru,
        name="Competitor",
        link_date="2026-06-29T10:00:00",
        unit_price=100000.0,
    )
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="our1",
        ktru_code=ktru,
        name="Our PP",
        link_date="2026-06-30T10:00:00",
        unit_price=95000.0,
    )
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="comp1",
        find_json=json.dumps({**find_base, "offerPriceListItemId": "comp1"}),
        ktru_code=ktru,
        unit_price=100000.0,
    )
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="our1",
        find_json=json.dumps(
            {
                **find_base,
                "offerPriceListItemId": "our1",
                "deliveryPlaces": [{"oktmo": {"code": "99000000"}}],
            }
        ),
        ktru_code=ktru,
        unit_price=95000.0,
    )
    from eis_analytics_db import backfill_pp_characteristics_hashes

    backfill_pp_characteristics_hashes(db)


def test_build_pp_monitor_registry_groups(tmp_path: Path):
    from eis_analytics_db import (
        _build_pp_monitor_registry_groups,
        count_pp_monitor_registry_groups,
        list_pp_monitor_registry_groups,
    )

    db = tmp_path / "analytics.sqlite3"
    _seed_registry_stack_pair(db)
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="solo1",
        ktru_code="26.20.13.000-00000002",
        name="Solo no hash",
        link_date="2026-07-01T10:00:00",
    )
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="solo2",
        ktru_code="26.20.13.000-00000002",
        name="Solo other hash",
        link_date="2026-07-02T10:00:00",
    )
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id="solo2",
        find_json=json.dumps(
            {
                "ktru": {"code": "26.20.13.000-00000002"},
                "ktruCharacteristics": [
                    {"code": "other", "characteristicValues": [{"concreteValue": "99"}]},
                ],
            }
        ),
        ktru_code="26.20.13.000-00000002",
    )
    from eis_analytics_db import backfill_pp_characteristics_hashes

    backfill_pp_characteristics_hashes(db)

    groups = _build_pp_monitor_registry_groups(db)
    stacks = [g for g in groups if g["is_stack"]]
    assert len(stacks) == 1
    stack = stacks[0]
    assert stack["member_count"] == 2
    assert stack["primary"]["offer_price_list_item_id"] == "comp1"
    assert stack["children"][0]["offer_price_list_item_id"] == "our1"
    assert stack["has_our_pp"] is True

    singles = [g for g in groups if not g["is_stack"]]
    assert len(singles) == 2
    solo_ids = {g["primary"]["offer_price_list_item_id"] for g in singles}
    assert solo_ids == {"solo1", "solo2"}

    assert count_pp_monitor_registry_groups(db) == len(groups)
    assert len(groups) == 3
    assert len(list_pp_monitor_registry_groups(db, limit=2)) == 2

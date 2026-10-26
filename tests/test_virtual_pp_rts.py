from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from eis_analytics_db import analytics_db_path, record_notice_html_page
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from pp_core_catalog import find_selection, find_selection_pairs, persist_core_snapshot
from region_aliases import resolve_region
from rts_lot_items import parse_lot_items_search_paged_payload
from rts_pp_application_api import (
    PURCHASE_METHOD_TYPE_EZT,
    build_auction_search_payload,
    parse_auction_search_response,
)
from virtual_pp_match import compare_virtual_notice_characteristics, match_eis_html_line, match_lot_item
from virtual_pp_poll import ingest_ezt_lk_dump, parse_publication_date_string, poll_virtual_pp
from virtual_pp_set import pick_belt_from_leftovers, sync_virtual_set_from_core
from virtual_pp_store import (
    get_virtual_pp_variant,
    list_virtual_match_events,
    list_virtual_pp_variants,
    parse_winner_unit_price,
    save_virtual_match_winner_unit_price,
    upsert_rts_ezt_notice,
    upsert_virtual_match_event,
    virtual_match_stats,
)

ROOT = Path(__file__).resolve().parents[1]
SEARCH_DUMP = ROOT / "PP_create" / "RTS_monitor.txt"
LOT_DUMP = ROOT / "PP_create" / "RTS_monitor_response.txt"
STUDENT_TMPL = ROOT / "deploy" / "310111150_21.txt"
CABINET = "31.01.12.139-00000001"
STUDENT = "31.01.11.150-00000021"
FACADE = "cb7e3a45-0"
GLASS = "66d040b5-c"
DRAWERS = "c202afa0-b"
HEIGHT = "1ee0bf77-3"
HEIGHT_3 = "aed887b0-2"
HEIGHT_4 = "9548848f-d"
HEIGHT_5 = "0f45839c-e"
HEIGHT_6 = "f82a9ac4-0"
HEIGHT_7 = "897c058a-2"
MATERIAL = "1f5fa9d7-5"
PLASTIC = "393996d7-d"


def _json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    out: list[dict] = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def _cabinet_template() -> dict:
    return {
        "code": CABINET,
        "name": "Шкаф деревянный для документов",
        "characteristics": [
            {
                "code": FACADE,
                "name": "Тип фасада",
                "characteristicValues": [
                    {"qualityDescription": "Закрытый", "valueCode": "facade-closed"},
                    {"qualityDescription": "Распашной", "valueCode": "facade-swing"},
                ],
            },
            {
                "code": GLASS,
                "name": "Наличие остекления",
                "characteristicValues": [
                    {"qualityDescription": "Нет", "valueCode": "glass-no"},
                    {"qualityDescription": "Да", "valueCode": "glass-yes"},
                ],
            },
            {
                "code": DRAWERS,
                "name": "Наличие выдвижных ящиков",
                "characteristicValues": [
                    {"qualityDescription": "Нет", "valueCode": "drawers-no"},
                    {"qualityDescription": "Да", "valueCode": "drawers-yes"},
                ],
            },
        ],
    }


def _closed_combo() -> dict:
    return {
        "hash": "eis-closed",
        "n": 12,
        "preview": "Тип фасада=Закрытый; Наличие остекления=Нет; Наличие выдвижных ящиков=Нет",
        "chars": [
            {"display_label": "Тип фасада", "characteristic_code": FACADE, "value_display": "Закрытый"},
            {"display_label": "Наличие остекления", "characteristic_code": GLASS, "value_display": "Нет"},
            {
                "display_label": "Наличие выдвижных ящиков",
                "characteristic_code": DRAWERS,
                "value_display": "Нет",
            },
        ],
    }


def test_krasnodar_alias_to_subject_oktmo():
    hit = resolve_region("Краснодарский край")
    assert hit.mapped
    assert hit.subject_oktmo == "03000000"
    assert hit.source == "alias"


def test_rts_dump_region_aliases_cover_sevastopol_and_oblast_short():
    assert resolve_region("Ростовская обл.").subject_oktmo == "60000000"
    assert resolve_region("г. Севастополь").subject_oktmo == "67000000"
    assert resolve_region("Респ. Хакасия").subject_oktmo == "95000000"


def test_parse_auction_search_from_rts_monitor_dump():
    blobs = _json_objects(SEARCH_DUMP.read_text(encoding="utf-8"))
    response = next(b for b in blobs if isinstance(b.get("Data"), dict) and "Data" in (b.get("Data") or {}))
    rows, total = parse_auction_search_response(response)
    assert total >= 1
    assert rows
    first = rows[0]
    assert first.notification_number == "0818300005426000009"
    assert first.region == "Краснодарский край"
    assert first.publication_date.startswith("06.10.2026")
    assert parse_publication_date_string(first.publication_date) == "2026-10-06"


def test_build_ezt_search_payload_uses_type_108():
    payload = build_auction_search_payload(
        ktru_code=CABINET,
        page_size=100,
        aggregate_procedure_types=[str(PURCHASE_METHOD_TYPE_EZT)],
        publication_from="2026-09-30T21:00:00.000Z",
    )
    assert payload["aggregateProcedureTypes"] == ["108"]
    assert payload["ktruCode"] == CABINET
    assert payload["pageSize"] == 100
    assert payload["publicationDate"]["from"] == "2026-09-30T21:00:00.000Z"


def test_match_cabinet_lot_items_from_rts_dump():
    blobs = _json_objects(LOT_DUMP.read_text(encoding="utf-8"))
    payload = next(b for b in blobs if "LotItems" in b)
    items = parse_lot_items_search_paged_payload(payload)
    assert items
    item = items[0]
    assert item.classification_code == CABINET
    names = {ch.name for ch in item.characteristics}
    assert "Тип фасада" in names
    variants = [
        {
            "variant_no": 1,
            "in_core": 1,
            "eis_hash": "eis-closed",
            "characteristics": [
                {"code": FACADE, "value_code": "facade-closed"},
                {"code": GLASS, "value_code": "glass-no"},
                {"code": DRAWERS, "value_code": "drawers-no"},
            ],
        }
    ]
    template = _cabinet_template()
    hit = match_lot_item(
        item,
        variants,
        template,
        region_raw="Краснодарский край",
        delivery_days=5,
    )
    miss_term = match_lot_item(
        item,
        variants,
        template,
        region_raw="Краснодарский край",
        delivery_days=200,
    )
    assert hit["char_status"] == "hit"
    assert hit["variant_no"] == 1
    assert hit["in_core"] == 1
    assert hit["subject_oktmo"] == "03000000"
    assert hit["quantity"] == 48.0
    assert hit["delivery_ok"] is True
    assert miss_term["char_status"] == hit["char_status"]
    swing = [
        {
            "variant_no": 2,
            "in_core": 1,
            "eis_hash": "eis-swing",
            "characteristics": [
                {"code": FACADE, "value_code": "facade-swing"},
                {"code": GLASS, "value_code": "glass-no"},
                {"code": DRAWERS, "value_code": "drawers-no"},
            ],
        }
    ]
    missed = match_lot_item(item, swing, template, region_raw="Краснодарский край")
    assert missed["char_status"] == "miss"


def test_sync_virtual_set_and_stats(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CABINET, [_closed_combo()], _cabinet_template(), total_notices=12)
    summary = sync_virtual_set_from_core(db, CABINET)
    assert summary["core_n"] == 1
    assert summary["belt_n"] == 0
    rows = list_virtual_pp_variants(db, CABINET)
    assert rows[0]["in_core"] == 1
    upsert_rts_ezt_notice(
        db,
        {
            "notification_number": "0818300005426000009",
            "ktru_code": CABINET,
            "region_raw": "Краснодарский край",
            "subject_oktmo": "03000000",
            "nmck": 1171901.76,
            "publication_date": date.today().isoformat(),
            "trade_name": "Шкаф",
        },
    )
    upsert_virtual_match_event(
        db,
        {
            "notification_number": "0818300005426000009",
            "line_index": 0,
            "ktru_code": CABINET,
            "variant_no": 1,
            "in_core": 1,
            "char_status": "hit",
            "subject_oktmo": "03000000",
            "region_raw": "Краснодарский край",
            "unit_price": 24414.62,
            "quantity": 48,
            "nmck": 1171901.76,
            "set_id": summary["set_id"],
        },
    )
    stats = virtual_match_stats(db, CABINET, days=30, set_id=summary["set_id"])
    assert stats["hit_core"] == 1
    assert stats["regions"][0]["subject_oktmo"] == "03000000"
    upsert_virtual_match_event(
        db,
        {
            "notification_number": "0818300005426000009",
            "line_index": 1,
            "ktru_code": CABINET,
            "variant_no": 1,
            "in_core": 1,
            "char_status": "hit",
            "subject_oktmo": "03000000",
            "region_raw": "Краснодарский край",
            "unit_price": 24414.62,
            "quantity": 48,
            "nmck": 1171901.76,
            "set_id": summary["set_id"],
        },
    )
    upsert_virtual_match_event(
        db,
        {
            "notification_number": "0358300233926000051",
            "line_index": 0,
            "ktru_code": CABINET,
            "variant_no": None,
            "in_core": None,
            "char_status": "miss",
            "subject_oktmo": "60000000",
            "region_raw": "Ростовская обл",
            "unit_price": 185000,
            "quantity": 2,
            "nmck": 370000,
            "set_id": summary["set_id"],
        },
    )
    rows = list_virtual_match_events(db, CABINET, days=30)
    assert len(rows) == 3
    hit = next(r for r in rows if r["notification_number"].endswith("0009"))
    assert hit["matched"] is True
    assert "regNumber=0818300005426000009" in hit["eis_url"]
    assert hit["line_sum"] == round(24414.62 * 48, 2)
    assert list_virtual_match_events(db, CABINET, days=30, matched="yes")[0]["matched"] is True
    assert list_virtual_match_events(db, CABINET, days=30, matched="no")[0]["char_status"] == "miss"
    variant = get_virtual_pp_variant(db, CABINET, 1)
    assert variant is not None
    assert variant["in_core"] == 1
    assert get_virtual_pp_variant(db, CABINET, 99) is None


def test_winner_unit_price_survives_rematch(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CABINET, [_closed_combo()], _cabinet_template(), total_notices=12)
    summary = sync_virtual_set_from_core(db, CABINET)
    event = {
        "notification_number": "0818300005426000009",
        "line_index": 0,
        "ktru_code": CABINET,
        "variant_no": 1,
        "in_core": 1,
        "char_status": "hit",
        "subject_oktmo": "03000000",
        "unit_price": 24414.62,
        "quantity": 48,
        "set_id": summary["set_id"],
    }
    upsert_virtual_match_event(db, event)
    assert parse_winner_unit_price("1 100,5") == 1100.5
    n = save_virtual_match_winner_unit_price(
        db,
        notification_number=event["notification_number"],
        line_index=0,
        ktru_code=CABINET,
        winner_unit_price=1100.5,
    )
    assert n == 1
    event["unit_price"] = 25000
    upsert_virtual_match_event(db, event)
    row = list_virtual_match_events(db, CABINET, days=30)[0]
    assert row["winner_unit_price"] == 1100.5
    assert row["unit_price"] == 25000


def test_list_virtual_match_events_hides_old_set_ids(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CABINET, [_closed_combo()], _cabinet_template(), total_notices=12)
    first = sync_virtual_set_from_core(db, CABINET)
    event = {
        "notification_number": "0818300005426000009",
        "line_index": 0,
        "ktru_code": CABINET,
        "variant_no": 1,
        "in_core": 1,
        "char_status": "hit",
        "subject_oktmo": "03000000",
        "unit_price": 24414.62,
        "quantity": 48,
        "set_id": first["set_id"],
    }
    upsert_virtual_match_event(db, event)
    second = sync_virtual_set_from_core(db, CABINET)
    assert second["set_id"] != first["set_id"]
    event["set_id"] = second["set_id"]
    event["char_status"] = "miss"
    upsert_virtual_match_event(db, event)
    rows = list_virtual_match_events(db, CABINET, days=30)
    assert len(rows) == 1
    assert rows[0]["set_id"] == second["set_id"]
    assert rows[0]["char_status"] == "miss"


def test_char_status_override_survives_rematch(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CABINET, [_closed_combo()], _cabinet_template(), total_notices=12)
    summary = sync_virtual_set_from_core(db, CABINET)
    event = {
        "notification_number": "0372200076526000008",
        "line_index": 0,
        "ktru_code": CABINET,
        "variant_no": 11,
        "in_core": 0,
        "char_status": "hit",
        "subject_oktmo": "03000000",
        "unit_price": 3100,
        "quantity": 1,
        "set_id": summary["set_id"],
    }
    upsert_virtual_match_event(db, event)
    from virtual_pp_store import parse_char_status_override, save_virtual_match_char_status_override

    assert parse_char_status_override("нет") == "miss"
    n = save_virtual_match_char_status_override(
        db,
        notification_number=event["notification_number"],
        line_index=0,
        ktru_code=CABINET,
        char_status_override="miss",
    )
    assert n == 1
    event["char_status"] = "hit"
    upsert_virtual_match_event(db, event)
    row = list_virtual_match_events(db, CABINET, days=30)[0]
    assert row["char_status"] == "hit"
    assert row["char_status_override"] == "miss"
    assert row["matched"] is False
    assert list_virtual_match_events(db, CABINET, days=30, matched="no")[0]["notification_number"].endswith("0008")


def test_ingest_ezt_lk_dump_from_monitor_files(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CABINET, [_closed_combo()], _cabinet_template(), total_notices=12)
    summary = sync_virtual_set_from_core(db, CABINET)
    blobs = _json_objects(SEARCH_DUMP.read_text(encoding="utf-8"))
    search = next(b for b in blobs if isinstance(b.get("Data"), dict) and "Data" in (b.get("Data") or {}))
    lot_blobs = _json_objects(LOT_DUMP.read_text(encoding="utf-8"))
    lots = next(b for b in lot_blobs if "LotItems" in b)
    first = search["Data"]["Data"][0]
    trade_id = first["TradeId"]
    procedures = {
        trade_id: {
            "CurrentVersionProcedure": {
                "Id": 4242,
                "UrlTradeId": trade_id,
                "CommonInfo": {
                    "NotificationNumber": first["NotificationNumber"],
                    "NotificationName": first["TradeName"],
                },
                "ContractObject": {"Sum": {"Amount": first["StartPrice"]}},
            }
        }
    }
    result = ingest_ezt_lk_dump(
        db,
        ktru_code=CABINET,
        search_payload=search,
        procedures=procedures,
        lot_items={"4242": lots},
    )
    assert result["ok"] is True
    assert result["notices"] >= 1
    assert result["matched"] >= 1
    assert summary["core_n"] == 1


def _cabinet_eis_chars(*, facade: str = "Закрытый") -> list[dict]:
    return [
        {"display_label": "Тип фасада", "value_display": facade},
        {"display_label": "Наличие остекления", "value_display": "Нет"},
        {"display_label": "Наличие выдвижных ящиков", "value_display": "Нет"},
    ]


def test_match_eis_html_chars_to_virtual_cabinet():
    variants = [
        {
            "variant_no": 1,
            "in_core": 1,
            "eis_hash": "eis-closed",
            "characteristics": [
                {"code": FACADE, "value_code": "facade-closed"},
                {"code": GLASS, "value_code": "glass-no"},
                {"code": DRAWERS, "value_code": "drawers-no"},
            ],
        }
    ]
    template = _cabinet_template()
    hit = match_eis_html_line(
        _cabinet_eis_chars(),
        variants,
        template,
        region_raw="Краснодарский край",
        quantity=48,
        unit_price=24414.62,
    )
    assert hit["char_status"] == "hit"
    assert hit["variant_no"] == 1
    assert hit["subject_oktmo"] == "03000000"
    miss = match_eis_html_line(_cabinet_eis_chars(facade="Распашной"), variants, template)
    assert miss["char_status"] == "miss"
    extra = _cabinet_eis_chars() + [{"display_label": "Неизвестное поле", "value_display": "Да"}]
    still_hit = match_eis_html_line(extra, variants, template, region_raw="Краснодарский край")
    assert still_hit["char_status"] == "hit"
    assert still_hit["variant_no"] == 1


def test_match_eis_html_requires_draft_region():
    variants = [
        {
            "variant_no": 1,
            "in_core": 1,
            "eis_hash": "eis-closed",
            "characteristics": [
                {"code": FACADE, "value_code": "facade-closed"},
                {"code": GLASS, "value_code": "glass-no"},
                {"code": DRAWERS, "value_code": "drawers-no"},
            ],
            "regions": [{"code": "01000000", "unit_price": "4500", "quantity": "2"}],
        }
    ]
    template = _cabinet_template()
    miss = match_eis_html_line(
        _cabinet_eis_chars(),
        variants,
        template,
        region_raw="Краснодарский край",
        unit_price=24414.62,
    )
    assert miss["char_status"] == "miss"
    variants[0]["regions"] = [
        {"code": "03000000", "unit_price": "4100", "quantity": "10"},
        {"code": "01000000", "unit_price": "4500", "quantity": "2"},
    ]
    hit = match_eis_html_line(
        _cabinet_eis_chars(),
        variants,
        template,
        region_raw="Краснодарский край",
        unit_price=24414.62,
    )
    assert hit["char_status"] == "hit"
    assert hit["offer_unit_price"] == 4100.0


def test_belt_from_leftovers_ready_only():
    leftover = [
        {
            "hash": "belt-ready",
            "n": 5,
            "preview": "Тип фасада=Распашной",
            "chars": [
                {"display_label": "Тип фасада", "characteristic_code": FACADE, "value_display": "Распашной"},
                {"display_label": "Наличие остекления", "characteristic_code": GLASS, "value_display": "Нет"},
                {
                    "display_label": "Наличие выдвижных ящиков",
                    "characteristic_code": DRAWERS,
                    "value_display": "Нет",
                },
            ],
        },
        {
            "hash": "belt-gap",
            "n": 4,
            "preview": "unknown",
            "chars": [{"display_label": "Нет такого", "value_display": "xxx"}],
        },
    ]
    belt = pick_belt_from_leftovers(leftover, total=21, core_share=0.8, template=_cabinet_template())
    assert [row["eis_hash"] for row in belt] == ["belt-ready"]
    assert belt[0]["map_status"] == "ready"


def test_sync_preserves_belt_variant_no(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CABINET, [_closed_combo()], _cabinet_template(), total_notices=12)
    belt = [
        {
            "eis_hash": "belt-swing",
            "map_status": "ready",
            "characteristics": [
                {"code": FACADE, "value_code": "facade-swing"},
                {"code": GLASS, "value_code": "glass-no"},
                {"code": DRAWERS, "value_code": "drawers-no"},
            ],
            "chars_preview": "Распашной",
        }
    ]
    first = sync_virtual_set_from_core(db, CABINET, belt_rows=belt)
    assert first["belt_n"] == 1
    no = next(v["variant_no"] for v in list_virtual_pp_variants(db, CABINET) if not v["in_core"])
    second = sync_virtual_set_from_core(db, CABINET, belt_rows=belt)
    assert second["belt_n"] == 1
    again = next(v["variant_no"] for v in list_virtual_pp_variants(db, CABINET) if not v["in_core"])
    assert again == no


def test_poll_virtual_pp_from_eis_html_monitor(tmp_path: Path):
    from catalog_pp_draft import CATALOG_PURCHASE_NUMBER
    from eis_analytics_db import (
        insert_pp_virtual_queue_row,
        list_pp_virtual_queue,
        save_rts_pp_draft,
        update_pp_virtual_queue,
    )

    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CABINET, [_closed_combo()], _cabinet_template(), total_notices=12)
    summary = sync_virtual_set_from_core(db, CABINET)
    insert_pp_virtual_queue_row(
        db,
        run_id="run-v",
        ktru_code=CABINET,
        variant_no=1,
        eis_hash="eis-closed",
        in_core=1,
        set_id=summary["set_id"],
        rts_profile="IPSokolova",
        regions=[{"code": "03000000", "unit_price": "4100", "quantity": "10"}],
    )
    job = list_pp_virtual_queue(db, ktru_code=CABINET)[0]
    update_pp_virtual_queue(db, int(job["id"]), status="drafted", rts_draft_id=99001)
    save_rts_pp_draft(
        db,
        purchase_number=CATALOG_PURCHASE_NUMBER,
        line_key="virtual-1",
        ktru_code=CABINET,
        rts_draft_id=99001,
        status="created",
        rts_profile="IPSokolova",
        fill_report_json='{"source":"virtual_core","variant_no":1,"in_core":1}',
    )
    line = KtruPurchaseLine(
        line_key="0818300005426000009|cab|0",
        purchase_number="0818300005426000009",
        object_sid="1",
        object_external_sid="",
        position_index=0,
        ktru_code=CABINET,
        position_display_name="Шкаф деревянный для документов",
        quantity_display="48",
        price_per_unit_display="24414,62",
        line_sum_display="1171901,76",
        characteristics=[
            CustomerCharacteristic(
                match_key="facade",
                characteristic_kind="reference",
                characteristic_code=None,
                display_label="Тип фасада",
                value_display="Закрытый",
            ),
            CustomerCharacteristic(
                match_key="glass",
                characteristic_kind="reference",
                characteristic_code=None,
                display_label="Наличие остекления",
                value_display="Нет",
            ),
            CustomerCharacteristic(
                match_key="drawers",
                characteristic_kind="reference",
                characteristic_code=None,
                display_label="Наличие выдвижных ящиков",
                value_display="Нет",
            ),
        ],
    )
    record_notice_html_page(
        db,
        purchase_number="0818300005426000009",
        source_url="https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0818300005426000009",
        html_text="<html></html>",
        lines=[line],
        fetched_at_utc=datetime.now(timezone.utc),
        ok=True,
        http_status=200,
        page_context={"region": "Краснодарский край"},
    )
    result = poll_virtual_pp(db, ktru_code=CABINET, days=7, template=_cabinet_template())
    assert result["source"] == "eis"
    assert result["notices"] == 1
    assert result["matched"] == 1
    stats = virtual_match_stats(db, CABINET, days=7)
    assert stats["hit_core"] == 1
    assert stats["notices"] == 1


def test_poll_virtual_pp_uses_set_when_no_drafts(tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CABINET, [_closed_combo()], _cabinet_template(), total_notices=12)
    sync_virtual_set_from_core(db, CABINET)
    line = KtruPurchaseLine(
        line_key="0342300012026000185|cab|0",
        purchase_number="0342300012026000185",
        object_sid="1",
        object_external_sid="",
        position_index=0,
        ktru_code=CABINET,
        position_display_name="Шкаф",
        quantity_display="1",
        price_per_unit_display="5000",
        line_sum_display="5000",
        characteristics=[
            CustomerCharacteristic(
                match_key="facade",
                characteristic_kind="reference",
                characteristic_code=None,
                display_label="Тип фасада",
                value_display="Закрытый",
            ),
            CustomerCharacteristic(
                match_key="glass",
                characteristic_kind="reference",
                characteristic_code=None,
                display_label="Наличие остекления",
                value_display="Нет",
            ),
            CustomerCharacteristic(
                match_key="drawers",
                characteristic_kind="reference",
                characteristic_code=None,
                display_label="Наличие выдвижных ящиков",
                value_display="Нет",
            ),
        ],
    )
    second = KtruPurchaseLine(
        line_key="0342300012026000185|cab|1",
        purchase_number="0342300012026000185",
        object_sid="2",
        object_external_sid="",
        position_index=1,
        ktru_code=CABINET,
        position_display_name="Шкаф",
        quantity_display="1",
        price_per_unit_display="5000",
        line_sum_display="5000",
        characteristics=line.characteristics,
    )
    record_notice_html_page(
        db,
        purchase_number="0342300012026000185",
        source_url="https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0342300012026000185",
        html_text="<html></html>",
        lines=[line, second],
        fetched_at_utc=datetime.now(timezone.utc),
        ok=True,
        http_status=200,
        page_context={"region": "Краснодарский край"},
    )
    result = poll_virtual_pp(db, ktru_code=CABINET, days=7, template=_cabinet_template())
    assert result["notices"] == 1
    assert result["matched"] == 2
    rows = list_virtual_match_events(db, CABINET, days=7)
    assert len(rows) == 2
    assert {int(r["line_index"]) for r in rows} == {0, 1}
    assert all(r["notification_number"] == "0342300012026000185" for r in rows)


def _student_template() -> dict:
    data = json.loads(STUDENT_TMPL.read_text(encoding="utf-8"))
    return data.get("item") or data


def _student_notice_chars(*, height: str, material: str = "Пластик") -> list[dict]:
    return [
        {"display_label": "Регулировка по высоте", "value_display": "Да"},
        {"display_label": "Тип каркаса", "value_display": "Металлический"},
        {"display_label": "Вид материала спинки/сиденья", "value_display": material},
        {"display_label": "Ростовая группа", "value_display": height},
    ]


def _student_variant(variant_no: int, height_code: str, *, in_core: int = 0) -> dict:
    return {
        "variant_no": variant_no,
        "in_core": in_core,
        "eis_hash": f"h-{height_code}",
        "chars_preview": f"Ростовая группа={'4' if height_code == HEIGHT_4 else '6'}; Вид материала спинки/сиденья=Пластик",
        "characteristics": [
            {"code": HEIGHT, "value_code": height_code},
            {"code": MATERIAL, "value_code": PLASTIC},
        ],
    }


def test_pp_json_keeps_five_growth_groups_and_covers_notice_six():
    raw = json.loads((ROOT / ".cursor" / "stul_uch_5.txt").read_text(encoding="utf-8"))
    tmpl = _student_template()
    pairs = find_selection_pairs(raw, tmpl)
    heights = [row["value_code"] for row in pairs if row["code"] == HEIGHT]
    assert heights == [HEIGHT_3, HEIGHT_4, HEIGHT_5, HEIGHT_6, HEIGHT_7]
    assert find_selection(raw, tmpl)[HEIGHT] == HEIGHT_7
    variant = {
        "variant_no": 1,
        "in_core": 1,
        "eis_hash": "five-growth",
        "chars_preview": "Ростовая группа=3; Ростовая группа=4; Ростовая группа=5; Ростовая группа=6; Ростовая группа=7; Вид материала спинки/сиденья=Фанера",
        "characteristics": pairs,
    }
    notice_plywood = _student_notice_chars(height="6", material="Фанера")
    hit = match_eis_html_line(notice_plywood, [variant], tmpl)
    assert hit["char_status"] == "hit"
    assert hit["variant_no"] == 1
    miss = match_eis_html_line(_student_notice_chars(height="2", material="Фанера"), [variant], tmpl)
    assert miss["char_status"] == "miss"
    rows = compare_virtual_notice_characteristics(
        [
            {"display_label": "Ростовая группа", "value_display": "3"},
            {"display_label": "Ростовая группа", "value_display": "4"},
            {"display_label": "Ростовая группа", "value_display": "5"},
            {"display_label": "Ростовая группа", "value_display": "6"},
            {"display_label": "Ростовая группа", "value_display": "7"},
        ],
        [{"display_label": "Ростовая группа", "value_display": "6"}],
    )
    assert rows[0]["row_class"] == "match"
    assert rows[0]["virtual_value"] == "3; 4; 5; 6; 7"


def test_match_student_chair_growth_group_mismatch_is_miss():
    variants = [_student_variant(11, HEIGHT_4)]
    miss = match_eis_html_line(_student_notice_chars(height="6"), variants, _student_template())
    assert miss["char_status"] == "miss"
    assert miss["variant_no"] is None
    hit = match_eis_html_line(_student_notice_chars(height="4"), variants, _student_template())
    assert hit["char_status"] == "hit"
    assert hit["variant_no"] == 11


def test_match_student_chair_picks_variant_with_same_growth_group():
    variants = [_student_variant(11, HEIGHT_4), _student_variant(12, HEIGHT_6)]
    hit = match_eis_html_line(_student_notice_chars(height="6"), variants, _student_template())
    assert hit["char_status"] == "hit"
    assert hit["variant_no"] == 12


def test_match_student_chair_notice_two_growth_groups_not_covered_by_one():
    chars = _student_notice_chars(height="4") + [{"display_label": "Ростовая группа", "value_display": "6"}]
    miss = match_eis_html_line(chars, [_student_variant(11, HEIGHT_4)], _student_template())
    assert miss["char_status"] == "miss"
    both = {
        **_student_variant(3, HEIGHT_4, in_core=1),
        "characteristics": [
            {"code": HEIGHT, "value_code": HEIGHT_4},
            {"code": HEIGHT, "value_code": HEIGHT_6},
            {"code": MATERIAL, "value_code": PLASTIC},
        ],
    }
    hit = match_eis_html_line(chars, [both], _student_template())
    assert hit["char_status"] == "hit"
    assert hit["variant_no"] == 3


def test_compare_virtual_notice_characteristics_match_and_extra():
    rows = compare_virtual_notice_characteristics(
        [
            {"display_label": "Тип фасада", "value_display": "Закрытый"},
            {"display_label": "Наличие остекления", "value_display": "Нет"},
        ],
        [
            {"name": "Тип фасада", "value": "Закрытый [тип 1] вид 1"},
            {"name": "Цвет", "value": "Белый"},
        ],
    )
    by_label = {r["display_label"]: r for r in rows}
    assert by_label["Тип фасада"]["row_class"] == "match"
    assert by_label["Тип фасада"]["notice_value"].startswith("Закрытый")
    assert by_label["Наличие остекления"]["row_class"] == "virtual_only"
    assert by_label["Цвет"]["row_class"] == "notice_only"
    assert by_label["Цвет"]["virtual_value"] == ""


def test_compare_growth_group_mismatch_and_multi_value():
    mismatch = compare_virtual_notice_characteristics(
        [{"display_label": "Ростовая группа", "value_display": "4"}],
        [{"display_label": "Ростовая группа", "value_display": "6"}],
    )
    assert mismatch[0]["row_class"] == "mismatch"
    covered = compare_virtual_notice_characteristics(
        [
            {"display_label": "Ростовая группа", "value_display": "4"},
            {"display_label": "Ростовая группа", "value_display": "6"},
        ],
        [{"display_label": "Ростовая группа", "value_display": "6"}],
    )
    assert covered[0]["row_class"] == "match"
    assert "4" in covered[0]["virtual_value"]
    assert covered[0]["notice_value"] == "6"

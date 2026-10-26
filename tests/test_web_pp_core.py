from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from eis_analytics_db import analytics_db_path, list_rts_pp_drafts, save_rts_pp_draft, upsert_pp_monitor_finding
from virtual_pp_store import replace_rts_ezt_lot_items, upsert_virtual_match_event
from pp_core_catalog import CHAIR_KTRU_CODE, display_mapped_characteristics, persist_core_snapshot
from pp_core_queue import CoreDrainSummary
from virtual_pp_set import sync_virtual_set_from_core
from web_ui import main as web_main

ROOT = Path(__file__).resolve().parents[1]
CHAIR_PATH = ROOT / "deploy" / "310111150_03.txt"


def _chair_item() -> dict:
    import json

    return json.loads(CHAIR_PATH.read_text(encoding="utf-8"))


def _ready_combo() -> dict:
    return {
        "hash": "h-ready",
        "n": 10,
        "preview": "Вид материала спинки=Металл",
        "chars": [
            {
                "display_label": "Вид материала спинки",
                "characteristic_code": "94b59978-4",
                "value_display": "Металл",
            }
        ],
    }


def test_pp_core_page_200():
    client = TestClient(web_main.app)
    r = client.get("/pp-core")
    assert r.status_code == 200
    assert "Ядро КТРУ" in r.text
    assert "Отметить доступные" in r.text
    assert "Частые регионы опроса" in r.text
    assert "Создать и опубликовать выбранные" in r.text
    assert "pp-core-focus" in r.text
    assert "pp-core-focus-ladder" in r.text
    assert "pp-core-known" in r.text
    assert "ученический" in r.text
    assert "жидкое мыло" in r.text
    assert "Виртуальные ПП" in r.text
    assert "Журнал сопоставления" in r.text
    assert "nav-stack-registries" in r.text
    assert "Итоги дня" in r.text


def test_pp_virtual_page_200():
    client = TestClient(web_main.app)
    r = client.get("/pp-virtual")
    assert r.status_code == 200
    assert "Виртуальные ПП" in r.text
    assert "Собрать виртуальный набор из ядра" in r.text
    assert "Сопоставить с монитором ЕИС" in r.text
    assert "Создать черновики (без публикации)" in r.text
    assert "Журнал сопоставления" in r.text
    assert "pp-virtual-focus-ladder" in r.text
    assert "Проставить цену" in r.text
    assert 'id="pp-virtual-ktru"' in r.text
    assert "<select id=\"pp-virtual-ktru\">" in r.text or "<select id='pp-virtual-ktru'>" in r.text
    assert "input type=\"text\" id=\"pp-virtual-ktru\"" not in r.text


def test_pp_virtual_events_page_200():
    client = TestClient(web_main.app)
    r = client.get("/pp-virtual/events")
    assert r.status_code == 200
    assert "Дата" in r.text
    assert "Совпало?" in r.text
    assert "КТРУ" in r.text
    assert "/api/rts/pp/virtual/events" in r.text
    assert "Виртуальное ПП" in r.text
    assert "Извещение" in r.text
    assert "pp-virtual-ev-table-wrap" in r.text
    assert "ch.virtual_value" in r.text
    assert "ch.notice_value" in r.text
    assert "Цена виртуального ПП" in r.text
    assert "Цена извещения" in r.text
    assert "Создать виртуальное ПП" in r.text
    assert "/api/rts/pp/virtual/from-notice" in r.text
    assert "function matchCell" in r.text
    assert "Цена победителя" in r.text
    assert "pp-virtual-ev-winner" in r.text
    assert "/api/rts/pp/virtual/events/winner-price" in r.text
    assert "/api/rts/pp/virtual/events/char-status" in r.text
    assert "pp-virtual-ev-match-ov" in r.text
    assert "Наименование" in r.text
    assert "ktru_name" in r.text
    assert "pp-virtual-ev-ktru" in r.text
    assert "ch.value_code" not in r.text


def test_format_ktru_option_label():
    assert web_main._format_ktru_option("31.01.12.139-00000001", "Стул на металлическом каркасе") == (
        "Стул на металлическом каркасе (31.01.12.139-00000001)"
    )
    assert web_main._format_ktru_option("31.01.12.139-00000001", "") == "31.01.12.139-00000001"


def test_pp_virtual_ktru_dropdown_shows_virtual_names(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    sync_virtual_set_from_core(db, CHAIR_KTRU_CODE)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(
        web_main,
        "_ktru_display_name",
        lambda _db, _code: "Стул на металлическом каркасе",
    )
    client = TestClient(web_main.app)
    expected = f"Стул на металлическом каркасе ({CHAIR_KTRU_CODE})"
    virtual_page = client.get("/pp-virtual")
    assert virtual_page.status_code == 200
    assert expected in virtual_page.text
    assert f'value="{CHAIR_KTRU_CODE}"' in virtual_page.text
    events_page = client.get("/pp-virtual/events")
    assert events_page.status_code == 200
    assert expected in events_page.text
    assert "Наименование" in events_page.text


def test_display_mapped_characteristics_uses_template_names():
    rows = display_mapped_characteristics(
        [{"code": "94b59978-4", "value_code": "c98c9acd-2"}],
        _chair_item(),
        chars_preview="Вид материала спинки=Металл",
    )
    assert rows
    hit = next(r for r in rows if r["display_label"] == "Вид материала спинки")
    assert "Металл" in hit["value_display"]
    assert hit["code"] == "94b59978-4"


def test_display_mapped_characteristics_falls_back_to_preview():
    rows = display_mapped_characteristics(
        [{"code": "8e50a5cd-4", "value_code": "b99a85776-1"}],
        None,
        chars_preview="Наличие мягкого сидения=Да [тип 1] вид 1; Вид материала спинки=Дерево",
    )
    assert [r["display_label"] for r in rows] == ["Наличие мягкого сидения", "Вид материала спинки"]
    assert rows[0]["value_display"].startswith("Да")


def test_virtual_winner_price_api(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    summary = sync_virtual_set_from_core(db, CHAIR_KTRU_CODE)
    upsert_virtual_match_event(
        db,
        {
            "notification_number": "0315800000126000044",
            "line_index": 0,
            "ktru_code": CHAIR_KTRU_CODE,
            "char_status": "miss",
            "subject_oktmo": "01000000",
            "unit_price": 1200,
            "set_id": summary["set_id"],
        },
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(
        web_main,
        "_ktru_display_name",
        lambda _db, _code: "Стул на металлическом каркасе",
    )
    client = TestClient(web_main.app)
    saved = client.post(
        "/api/rts/pp/virtual/events/winner-price",
        json={
            "ktru_code": CHAIR_KTRU_CODE,
            "notification_number": "0315800000126000044",
            "line_index": 0,
            "winner_unit_price": "1 250,5",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["winner_unit_price"] == 1250.5
    listed = client.get(
        "/api/rts/pp/virtual/events",
        params={"ktru_code": CHAIR_KTRU_CODE, "days": 30},
    )
    assert listed.status_code == 200
    row = listed.json()["events"][0]
    assert row["winner_unit_price"] == 1250.5
    assert row["ktru_name"] == "Стул на металлическом каркасе"


def test_virtual_char_status_override_api(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    summary = sync_virtual_set_from_core(db, CHAIR_KTRU_CODE)
    upsert_virtual_match_event(
        db,
        {
            "notification_number": "0372200076526000008",
            "line_index": 0,
            "ktru_code": CHAIR_KTRU_CODE,
            "char_status": "hit",
            "variant_no": 11,
            "in_core": 0,
            "subject_oktmo": "01000000",
            "unit_price": 3100,
            "set_id": summary["set_id"],
        },
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    saved = client.post(
        "/api/rts/pp/virtual/events/char-status",
        json={
            "ktru_code": CHAIR_KTRU_CODE,
            "notification_number": "0372200076526000008",
            "line_index": 0,
            "char_status_override": "нет",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["char_status_override"] == "miss"
    listed = client.get(
        "/api/rts/pp/virtual/events",
        params={"ktru_code": CHAIR_KTRU_CODE, "days": 30},
    )
    row = listed.json()["events"][0]
    assert row["char_status"] == "hit"
    assert row["char_status_override"] == "miss"
    assert row["matched"] is False


def test_virtual_variant_api_returns_readable_chars(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    sync_virtual_set_from_core(db, CHAIR_KTRU_CODE)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    r = client.get("/api/rts/pp/virtual/variant", params={"ktru_code": CHAIR_KTRU_CODE, "variant_no": 1})
    assert r.status_code == 200
    chars = r.json()["characteristics"]
    assert chars
    assert all(row.get("display_label") for row in chars)
    assert not any("94b59978-4" == row.get("display_label") for row in chars)
    assert any("спинки" in str(row.get("display_label") or "") for row in chars)
    assert "virtual_value" in chars[0]
    assert chars[0].get("notice_value") in ("", None)


def test_virtual_variant_api_compares_notice_chars(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    summary = sync_virtual_set_from_core(db, CHAIR_KTRU_CODE)
    replace_rts_ezt_lot_items(
        db,
        "0315800000126000001",
        [
            {
                "line_index": 0,
                "ktru_code": CHAIR_KTRU_CODE,
                "unit_price": 1200,
                "characteristics": [
                    {"name": "Вид материала спинки", "value": "Металл [тип 1] вид 1"},
                    {"name": "Наличие подлокотников", "value": "Да"},
                ],
            }
        ],
    )
    upsert_virtual_match_event(
        db,
        {
            "notification_number": "0315800000126000001",
            "line_index": 0,
            "ktru_code": CHAIR_KTRU_CODE,
            "variant_no": 1,
            "in_core": 1,
            "char_status": "hit",
            "subject_oktmo": "01000000",
            "unit_price": 1200,
            "offer_unit_price": 1100,
            "set_id": summary["set_id"],
        },
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    r = client.get(
        "/api/rts/pp/virtual/variant",
        params={
            "ktru_code": CHAIR_KTRU_CODE,
            "variant_no": 1,
            "notification_number": "0315800000126000001",
            "line_index": 0,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["notification_number"] == "0315800000126000001"
    chars = body["characteristics"]
    by_label = {row["display_label"]: row for row in chars}
    back = by_label["Вид материала спинки"]
    assert back["row_class"] == "match"
    assert "Металл" in back["virtual_value"]
    assert "Металл" in back["notice_value"]
    extra = by_label["Наличие подлокотников"]
    assert extra["row_class"] == "notice_only"
    assert extra["notice_value"] == "Да"
    assert body["notice_unit_price"] == 1200
    assert body["offer_unit_price"] == 1100
    assert body.get("can_create") in (False, None)


def test_virtual_variant_api_notice_only_can_create(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    sync_virtual_set_from_core(db, CHAIR_KTRU_CODE)
    replace_rts_ezt_lot_items(
        db,
        "0315800000126000002",
        [
            {
                "line_index": 0,
                "ktru_code": CHAIR_KTRU_CODE,
                "unit_price": 1300,
                "characteristics": [{"name": "Вид материала спинки", "value": "Дерево"}],
            }
        ],
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    r = client.get(
        "/api/rts/pp/virtual/variant",
        params={
            "ktru_code": CHAIR_KTRU_CODE,
            "variant_no": 0,
            "notification_number": "0315800000126000002",
            "line_index": 0,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["can_create"] is True
    assert body["variant_no"] is None
    assert body["notice_unit_price"] == 1300
    by_label = {row["display_label"]: row for row in body["characteristics"]}
    assert "Дерево" in by_label["Вид материала спинки"]["notice_value"]
    assert by_label["Вид материала спинки"]["row_class"] == "notice_only"


def test_virtual_from_notice_api_creates_local_draft(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    sync = sync_virtual_set_from_core(db, CHAIR_KTRU_CODE)
    replace_rts_ezt_lot_items(
        db,
        "0315800000126000003",
        [
            {
                "line_index": 0,
                "ktru_code": CHAIR_KTRU_CODE,
                "unit_price": 1400,
                "quantity": 1,
                "characteristics": [{"name": "Вид материала спинки", "value": "Дерево"}],
            }
        ],
    )
    upsert_virtual_match_event(
        db,
        {
            "notification_number": "0315800000126000003",
            "line_index": 0,
            "ktru_code": CHAIR_KTRU_CODE,
            "char_status": "miss",
            "subject_oktmo": "01000000",
            "unit_price": 1400,
            "quantity": 1,
            "set_id": sync["set_id"],
        },
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_require_operator_rts_profile", lambda _r: "IPSokolova")
    monkeypatch.setattr("pp_virtual_queue.resolve_ktru_template", lambda *a, **k: _chair_item())
    client = TestClient(web_main.app)
    r = client.post(
        "/api/rts/pp/virtual/from-notice",
        json={
            "ktru_code": CHAIR_KTRU_CODE,
            "notification_number": "0315800000126000003",
            "line_index": 0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["already"] is False
    assert int(body["variant_no"]) == 2
    assert int(body["in_core"]) == 0
    assert int(body["rts_draft_id"]) < 0
    drafts = list_rts_pp_drafts(db, rts_profile="IPSokolova")
    assert len(drafts) == 1


def test_virtual_set_price_ladder_from_findings(monkeypatch, tmp_path: Path):
    import json

    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    sync_virtual_set_from_core(db, CHAIR_KTRU_CODE)
    find_item = {
        "ktruCharacteristics": [
            {
                "code": "94b59978-4",
                "name": "Вид материала спинки",
                "characteristicValues": [{"qualityDescription": "Металл", "valueCode": None}],
            }
        ],
        "deliveryPlaces": [{"oktmo": {"code": "01000000"}}],
    }
    for i, price in enumerate([1000, 2000, 3000], start=1):
        upsert_pp_monitor_finding(
            db,
            offer_price_list_item_id=f"virt-oid-{i}",
            ktru_code=CHAIR_KTRU_CODE,
            unit_price=float(price),
            find_json=json.dumps(find_item),
            trigger_recompute=False,
        )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "resolve_ktru_template", lambda *a, **k: _chair_item())
    client = TestClient(web_main.app)
    listed = client.get("/api/rts/pp/virtual/set", params={"ktru_code": CHAIR_KTRU_CODE})
    assert listed.status_code == 200
    row = listed.json()["variants"][0]
    assert row["pp_n"] == 3
    assert row["price_ladder"][0]["price"] == 1000
    assert row["top5"]["open_slots"] == 2


def test_pp_publish_sign_js_includes_core():
    js = (ROOT / "web_ui" / "static" / "pp_publish_sign.js").read_text(encoding="utf-8")
    assert 'j.source === "core"' in js


def test_core_variants_and_enqueue_validation(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_require_operator_rts_profile", lambda _r: "IPSokolova")
    monkeypatch.setattr(web_main, "process_pp_core_queue", lambda *a, **k: CoreDrainSummary())

    async def _noop():
        return None

    monkeypatch.setattr(web_main, "_kick_pp_core_drain", _noop)
    client = TestClient(web_main.app)

    listed = client.get("/api/rts/pp/core/variants", params={"ktru_code": CHAIR_KTRU_CODE})
    assert listed.status_code == 200
    body = listed.json()
    assert body["ktru_code"] == CHAIR_KTRU_CODE
    assert body["variants"][0]["can_enqueue"] is True
    assert body["variants"][0]["variant_no"] == 1
    assert body["variants"][0]["rts_dup_of"] is None
    assert body["variants"][0]["price_ladder"] == []
    assert "open_slots" in body["variants"][0]["top5"]

    empty_nos = client.post(
        "/api/rts/pp/core/enqueue",
        json={"ktru_code": CHAIR_KTRU_CODE, "variant_nos": [], "regions": [{"code": "01000000", "unit_price": "1", "quantity": "1"}]},
    )
    assert empty_nos.status_code == 400

    empty_regions = client.post(
        "/api/rts/pp/core/enqueue",
        json={"ktru_code": CHAIR_KTRU_CODE, "variant_nos": [1], "regions": []},
    )
    assert empty_regions.status_code == 400

    ok = client.post(
        "/api/rts/pp/core/enqueue",
        json={
            "ktru_code": CHAIR_KTRU_CODE,
            "variant_nos": [1],
            "regions": [{"code": "01000000", "unit_price": "4500", "quantity": "2"}],
        },
    )
    assert ok.status_code == 200
    data = ok.json()
    assert data["enqueued"] == 1
    assert data["run_id"]
    assert data["items"][0]["status"] == "pending"

    occupied = client.post(
        "/api/rts/pp/core/enqueue",
        json={
            "ktru_code": CHAIR_KTRU_CODE,
            "variant_nos": [1],
            "regions": [{"code": "01000000", "unit_price": "4500", "quantity": "2"}],
        },
    )
    assert occupied.status_code == 400
    assert "Ни один вариант не поставлен в очередь" in occupied.json()["detail"]
    assert "v1:" in occupied.json()["detail"]


def test_core_variants_marks_duplicate_rts_selection(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    dup = {
        "hash": "h-dup",
        "n": 3,
        "preview": "same",
        "chars": _ready_combo()["chars"],
    }
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo(), dup], _chair_item(), total_notices=13)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    client = TestClient(web_main.app)
    listed = client.get("/api/rts/pp/core/variants", params={"ktru_code": CHAIR_KTRU_CODE})
    assert listed.status_code == 200
    rows = listed.json()["variants"]
    assert rows[0]["can_enqueue"] is True
    assert rows[0]["rts_dup_of"] is None
    assert rows[1]["can_enqueue"] is False
    assert rows[1]["rts_dup_of"] == 1


def test_pp_drafts_lists_core_source(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="catalog",
        line_key="core-line-1",
        ktru_code=CHAIR_KTRU_CODE,
        rts_draft_id=88001,
        status="created",
        rts_profile="IPSokolova",
        created_at_utc=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        fill_report_json='{"source":"core","ktru_name":"Стул на металлическом каркасе","regions_count":2,"variant_no":1,"run_id":"run-1"}',
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: False)
    monkeypatch.setattr(web_main, "_rts_profile_for_request", lambda _req: "IPSokolova")
    monkeypatch.delenv("RTS_PP_DRAFTS_ENRICH_RTS", raising=False)
    client = TestClient(web_main.app)
    r = client.get("/pp-drafts")
    assert r.status_code == 200
    assert "Стул на металлическом каркасе" in r.text
    assert "2 рег." in r.text
    assert "88001" in r.text
    assert "Справочник КТРУ" not in r.text


def test_pp_drafts_lists_virtual_core_source_without_publish(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="catalog",
        line_key="virtual-line-1",
        ktru_code=CHAIR_KTRU_CODE,
        rts_draft_id=99021,
        status="created",
        rts_profile="IPSokolova",
        created_at_utc=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        fill_report_json='{"source":"virtual_core","ktru_name":"Стул на металлическом каркасе","regions_count":2,"variant_no":3,"in_core":1,"run_id":"vrun"}',
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: False)
    monkeypatch.setattr(web_main, "_rts_profile_for_request", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "_pp_publish_enabled", lambda: True)
    monkeypatch.delenv("RTS_PP_DRAFTS_ENRICH_RTS", raising=False)
    client = TestClient(web_main.app)
    r = client.get("/pp-drafts")
    assert r.status_code == 200
    assert "Виртуальное_ядро" in r.text
    assert "#3" in r.text
    assert "99021" in r.text
    assert 'data-source="virtual_core"' not in r.text


def test_pp_drafts_lists_local_virtual_without_rts_cabinet_id(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="catalog",
        line_key="virtual-local-1",
        ktru_code=CHAIR_KTRU_CODE,
        rts_draft_id=-1,
        status="created",
        rts_profile="IPSokolova",
        created_at_utc=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        fill_report_json='{"source":"virtual_core","ktru_name":"Стул на металлическом каркасе","regions_count":2,"variant_no":3,"in_core":1,"local":true}',
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: False)
    monkeypatch.setattr(web_main, "_rts_profile_for_request", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "_pp_publish_enabled", lambda: True)
    monkeypatch.delenv("RTS_PP_DRAFTS_ENRICH_RTS", raising=False)
    client = TestClient(web_main.app)
    r = client.get("/pp-drafts")
    assert r.status_code == 200
    assert "Виртуальное_ядро" in r.text
    assert "локальный" in r.text
    assert "Открыть в РТС" not in r.text
    assert 'data-source="virtual_core"' not in r.text


def test_core_variants_price_ladder_from_findings(monkeypatch, tmp_path: Path):
    import json

    db = analytics_db_path(tmp_path)
    persist_core_snapshot(db, CHAIR_KTRU_CODE, [_ready_combo()], _chair_item(), total_notices=10)
    find_item = {
        "ktruCharacteristics": [
            {
                "code": "94b59978-4",
                "name": "Вид материала спинки",
                "characteristicValues": [{"qualityDescription": "Металл", "valueCode": None}],
            }
        ],
        "deliveryPlaces": [{"oktmo": {"code": "01000000"}}],
    }
    for i, price in enumerate([1000, 2000, 3000], start=1):
        upsert_pp_monitor_finding(
            db,
            offer_price_list_item_id=f"web-oid-{i}",
            ktru_code=CHAIR_KTRU_CODE,
            unit_price=float(price),
            find_json=json.dumps(find_item),
            trigger_recompute=False,
        )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "resolve_ktru_template", lambda *a, **k: _chair_item())
    client = TestClient(web_main.app)
    listed = client.get("/api/rts/pp/core/variants", params={"ktru_code": CHAIR_KTRU_CODE})
    assert listed.status_code == 200
    row = listed.json()["variants"][0]
    assert row["pp_n"] == 3
    assert row["price_ladder"][0]["price"] == 1000
    assert row["top5"]["open_slots"] == 2


def test_core_frequent_regions_api(monkeypatch, tmp_path: Path):
    db = analytics_db_path(tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    r = client.get("/api/rts/pp/core/frequent-regions", params={"ktru_code": CHAIR_KTRU_CODE})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["items"] == []
    assert body["selected_n"] == 0
    assert "big_pp_n" in body

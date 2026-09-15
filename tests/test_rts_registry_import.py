from __future__ import annotations

import json
from pathlib import Path

import pytest

from rts_registry_import import (
    apply_registry_import,
    delete_registry_import_draft,
    items_from_payload,
    load_registry_import_draft,
    offer_id_for_position,
    parse_registry_json,
    payload_from_items,
    preview_rows_from_items,
    rts_offer_edit_url,
    save_registry_import_draft,
)
from rts_preoffers import load_preoffers_raw, preoffer_by_id


FIXTURE = Path(__file__).parent / "fixtures" / "rts_registry_mini.json"


def test_parse_registry_json_published_filter():
    text = FIXTURE.read_text(encoding="utf-8")
    result = parse_registry_json(text, only_published=True)

    assert result.total_in_file == 3
    assert len(result.items) == 2
    assert len(result.filtered_unpublished) == 1
    assert "Черновик" in result.filtered_unpublished[0]

    chair = next(x for x in result.items if x.source_position_id == 335155)
    assert chair.ktru_code == "31.01.11.150-00000003"
    assert chair.enabled is True
    assert chair.rts_url == rts_offer_edit_url(335155)
    assert chair.offer_id == offer_id_for_position(335155)
    assert chair.characteristics[0]["label"] == "Вид материала спинки"
    assert chair.characteristics[0]["value"] == "Дерево"


def test_parse_registry_json_includes_draft_when_filter_off():
    text = FIXTURE.read_text(encoding="utf-8")
    result = parse_registry_json(text, only_published=False)

    assert len(result.items) == 3
    draft = next(x for x in result.items if x.source_position_id == 999001)
    assert draft.enabled is False
    assert draft.offer_status == "Draft"


def test_parse_range_characteristic_value():
    text = FIXTURE.read_text(encoding="utf-8")
    result = parse_registry_json(text, only_published=True)
    monitor = next(x for x in result.items if x.source_position_id == 999002)
    diag = next(c for c in monitor.characteristics if "Диагональ" in c["label"])
    assert "65,0" in diag["value"]
    assert "70,0" in diag["value"]
    assert "Дюйм" in diag["value"]


def test_apply_registry_import_upsert_and_update(tmp_path):
    path = tmp_path / "rts_preoffers.json"
    text = FIXTURE.read_text(encoding="utf-8")
    items = parse_registry_json(text, only_published=True).items

    written, unchanged = apply_registry_import(path, items)
    assert written == 2
    assert unchanged == 0

    chair = preoffer_by_id(path, offer_id_for_position(335155))
    assert chair is not None
    assert chair["rts_url"] == rts_offer_edit_url(335155)
    assert chair["enabled"] is True

    chair_item = next(x for x in items if x.source_position_id == 335155)
    updated = type(chair_item)(
        offer_id=chair_item.offer_id,
        title="Стул обновлённый",
        ktru_code=chair_item.ktru_code,
        rts_url=chair_item.rts_url,
        sku=chair_item.sku,
        enabled=chair_item.enabled,
        characteristics=chair_item.characteristics,
        source_position_id=chair_item.source_position_id,
        offer_status=chair_item.offer_status,
    )
    written2, unchanged2 = apply_registry_import(path, [updated, *[x for x in items if x.source_position_id != 335155]])
    assert written2 >= 1
    assert preoffer_by_id(path, offer_id_for_position(335155))["title"] == "Стул обновлённый"


def test_registry_import_draft_file_not_in_session_cookie(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8")
    result = parse_registry_json(text, only_published=True)
    token = save_registry_import_draft(
        tmp_path,
        preview=preview_rows_from_items(result.items),
        payload=payload_from_items(result.items),
        filtered=result.filtered_unpublished,
        skipped=result.skipped,
    )
    draft = load_registry_import_draft(tmp_path, token)
    assert draft is not None
    items = items_from_payload(draft["payload"])
    assert len(items) == 2
    delete_registry_import_draft(tmp_path, token)
    assert load_registry_import_draft(tmp_path, token) is None


def test_parse_rejects_too_many_positions():
    positions = [
        {"id": i, "name": f"P{i}", "ktru": {"code": f"00.00.00.000-{i:08d}"}, "offer": {"status": "Published"}}
        for i in range(501)
    ]
    huge = json.dumps({"page": positions, "total": 501})
    with pytest.raises(ValueError, match="Слишком много позиций"):
        parse_registry_json(huge)

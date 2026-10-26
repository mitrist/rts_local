"""Tests for HTML ↔ creation-data compare and DB helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from eis_analytics_db import (
    analytics_db_path,
    get_creation_data_snapshot,
    list_collector_candidates,
    record_notice_html_page,
    record_rss_poll_run,
    save_creation_data_snapshot,
    save_html_creation_diffs,
)
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from html_creation_compare import (
    aggregate_pattern_summary,
    compare_html_to_creation_data,
    normalize_creation_characteristics,
)


def _html_char(label: str, value: str, *, code: str | None = None) -> dict:
    return {
        "display_label": label,
        "characteristic_code": code,
        "match_key": f"code:{code}" if code else f"name:{label.lower()}",
        "value_display": value,
        "characteristic_kind": "manual",
    }


def _creation_char(code: str, name: str, value: str, *, required: bool = False) -> dict:
    return {
        "code": code,
        "name": name,
        "dictionaryId": 1,
        "isRequired": required,
        "ktruCharacteristicValues": [{"qualityDescription": value, "dictionaryId": 10}],
    }


def test_normalize_creation_characteristics():
    data = {"ktruCharacteristics": [_creation_char("c1", "Цвет", "белый")]}
    rows = normalize_creation_characteristics(data)
    assert len(rows) == 1
    assert rows[0]["code"] == "c1"
    assert rows[0]["values"] == ["белый"]


def test_compare_ok_by_code():
    html = [_html_char("Цвет", "белый", code="c1")]
    creation = {"ktruCharacteristics": [_creation_char("c1", "Цвет", "белый")]}
    report = compare_html_to_creation_data(html, creation)
    assert report["rows"][0]["status"] == "ok"
    assert report["summary"]["by_status"].get("ok") == 1


def test_compare_matched_by_name_only():
    html = [_html_char("Цвет корпуса", "белый")]
    creation = {
        "ktruCharacteristics": [_creation_char("other-code", "Цвет корпуса", "белый")]
    }
    report = compare_html_to_creation_data(html, creation)
    assert report["rows"][0]["status"] == "matched_by_name_only"


def test_compare_value_mismatch():
    html = [_html_char("Цвет", "чёрный", code="c1")]
    creation = {"ktruCharacteristics": [_creation_char("c1", "Цвет", "белый")]}
    report = compare_html_to_creation_data(html, creation)
    assert report["rows"][0]["status"] == "value_mismatch"
    assert report["rows"][0]["create_risk"]


def test_compare_html_missing_required():
    html = [_html_char("Цвет", "белый", code="c1")]
    creation = {
        "ktruCharacteristics": [
            _creation_char("c1", "Цвет", "белый"),
            _creation_char("c2", "Материал", "сталь", required=True),
        ]
    }
    report = compare_html_to_creation_data(html, creation)
    statuses = {r["status"] for r in report["rows"]}
    assert "required_html_gap" in statuses
    assert report["summary"]["has_create_risk"] is True


def test_compare_rts_missing():
    html = [_html_char("Секретное поле", "x", code="zzz")]
    creation = {"ktruCharacteristics": [_creation_char("c1", "Цвет", "белый")]}
    report = compare_html_to_creation_data(html, creation)
    statuses = {r["status"] for r in report["rows"]}
    assert "rts_missing" in statuses
    assert "html_missing" in statuses or "required_html_gap" not in statuses


def test_aggregate_pattern_summary():
    rows = [
        {"status": "ok", "purchase_number": "PN1", "html_label": "a"},
        {"status": "value_mismatch", "purchase_number": "PN2", "html_label": "b", "detail": "x"},
        {"status": "value_mismatch", "purchase_number": "PN3", "html_label": "c", "detail": "y"},
    ]
    out = aggregate_pattern_summary(rows, notices_with_risk=1, notices_total=2)
    by = {r["status"]: r for r in out}
    assert by["value_mismatch"]["count"] == 2
    assert by["_notices_with_create_risk"]["count"] == 1


def test_save_snapshot_and_diffs(tmp_path: Path):
    db = analytics_db_path(tmp_path / "data")
    creation = {"ktruCode": "K1", "ktruCharacteristics": [_creation_char("c1", "Цвет", "белый")]}
    save_creation_data_snapshot(
        db,
        purchase_number="PN1",
        star="lPN1-1",
        creation_data=creation,
        source="collector",
    )
    snap = get_creation_data_snapshot(db, purchase_number="PN1", star="lPN1-1")
    assert snap is not None
    assert snap["ktru_code"] == "K1"
    assert snap["creation_data"]["ktruCode"] == "K1"

    n = save_html_creation_diffs(
        db,
        purchase_number="PN1",
        line_key="PN1|K1|line1",
        star="lPN1-1",
        rows=[
            {
                "html_code": "c1",
                "html_label": "Цвет",
                "html_values": ["белый"],
                "rts_code": "c1",
                "rts_name": "Цвет",
                "rts_values": ["белый"],
                "status": "ok",
                "detail": "",
                "create_risk": "",
            }
        ],
    )
    assert n == 1


def test_list_collector_candidates(tmp_path: Path):
    db = analytics_db_path(tmp_path / "data")
    pn = "0117300004426000001"
    ktru = "22.22.10.000-00000005"
    line = KtruPurchaseLine(
        line_key=f"{pn}|{ktru}|line1",
        purchase_number=pn,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=ktru,
        characteristics=[
            CustomerCharacteristic(
                match_key="name:цвет",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="Цвет",
                value_display="белый",
            )
        ],
    )
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": f"https://example.test/{pn}", "pub_date_utc": "2026-09-17T10:00:00Z"}],
        query_label="t",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url="https://example.test/card",
        html_text="<html/>",
        lines=[line],
        fetched_at_utc=datetime(2026, 9, 17, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    cands = list_collector_candidates(db, limit=10)
    assert len(cands) == 1
    assert cands[0]["purchase_number"] == pn
    assert cands[0]["characteristics"]

from __future__ import annotations

import json
from pathlib import Path

from eis_analytics_db import (
    ANALYTICS_SCHEMA,
    connect,
    delete_pp_monitor_draft,
    get_pp_monitor_draft,
    init_db,
    load_pp_monitor_draft_offer_ids,
    load_pp_monitor_draft_created_at,
    save_pp_monitor_draft,
)


def test_pp_monitor_draft_schema_v12(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='rts_pp_monitor_drafts'"
            ).fetchall()
        }
    assert int(row[0]) >= ANALYTICS_SCHEMA
    assert ANALYTICS_SCHEMA == 19
    assert "rts_pp_monitor_drafts" in tables


def test_pp_monitor_draft_upsert_idempotent(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    save_pp_monitor_draft(
        db,
        offer_price_list_item_id="oid1",
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=12345,
        edit_url="https://example.test/edit",
        rts_profile="IPSokolova",
        status="created",
    )
    row = get_pp_monitor_draft(db, "oid1", rts_profile="IPSokolova")
    assert row is not None
    assert row["rts_draft_id"] == 12345
    save_pp_monitor_draft(
        db,
        offer_price_list_item_id="oid1",
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=99999,
        rts_profile="IPSokolova",
        status="created",
        verify_json=json.dumps({"ok": 1}),
    )
    row2 = get_pp_monitor_draft(db, "oid1", rts_profile="IPSokolova")
    assert row2 is not None
    assert row2["rts_draft_id"] == 99999


def test_load_pp_monitor_draft_offer_ids(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    save_pp_monitor_draft(
        db,
        offer_price_list_item_id="oid1",
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=12345,
        rts_profile="IPSokolova",
        status="created",
    )
    save_pp_monitor_draft(
        db,
        offer_price_list_item_id="oid2",
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=222,
        rts_profile="Nacpro",
        status="created",
    )
    assert load_pp_monitor_draft_offer_ids(db, ["oid1", "oid2", "missing"], rts_profiles=["IPSokolova"]) == {"oid1"}
    assert load_pp_monitor_draft_offer_ids(db, ["oid1", "oid2"], rts_profiles=["IPSokolova", "Nacpro"]) == {
        "oid1",
        "oid2",
    }
    delete_pp_monitor_draft(db, "oid1", rts_profile="IPSokolova")
    assert load_pp_monitor_draft_offer_ids(db, ["oid1"], rts_profiles=["IPSokolova"]) == set()
    assert load_pp_monitor_draft_offer_ids(db, ["oid1", "oid2"]) == {"oid2"}
    created = load_pp_monitor_draft_created_at(db, ["oid2"], rts_profiles=["Nacpro"])
    assert "oid2" in created
    assert created["oid2"]

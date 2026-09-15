from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from eis_analytics_db import (
    analytics_db_path,
    delete_rts_pp_draft,
    get_rts_pp_draft,
    list_rts_pp_drafts,
    save_rts_pp_draft,
)


def test_rts_pp_draft_roundtrip(tmp_path):
    db = analytics_db_path(tmp_path)
    verify = '{"total": 2, "ok": 2, "issues": 0, "rows": []}'
    save_rts_pp_draft(
        db,
        purchase_number="PN1",
        line_key="PN1|K1|line1",
        ktru_code="K1",
        rts_draft_id=100,
        protected_id="prot1",
        edit_url="https://example.test/edit/prot1",
        offer_id="offer-1",
        status="created",
        created_by="operator",
        created_at_utc=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
        verify_json=verify,
    )
    row = get_rts_pp_draft(db, "PN1", "PN1|K1|line1")
    assert row is not None
    assert row["rts_draft_id"] == 100
    assert row["protected_id"] == "prot1"
    assert row["status"] == "created"
    assert row["created_by"] == "operator"
    assert row["verify_json"] == verify


def test_list_rts_pp_drafts_orders_newest_first(tmp_path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="PN1",
        line_key="k1",
        ktru_code="K1",
        rts_draft_id=1,
        status="created",
        created_at_utc=datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc),
    )
    save_rts_pp_draft(
        db,
        purchase_number="PN2",
        line_key="k2",
        ktru_code="K2",
        rts_draft_id=2,
        status="error",
        created_at_utc=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
    )
    rows = list_rts_pp_drafts(db)
    assert [r["purchase_number"] for r in rows] == ["PN2", "PN1"]


def test_delete_rts_pp_draft(tmp_path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="PN1",
        line_key="k1",
        ktru_code="K1",
        rts_draft_id=99,
        status="created",
        rts_profile="IPSokolova",
    )
    assert delete_rts_pp_draft(db, "PN1", "k1", rts_profile="IPSokolova") is True
    assert get_rts_pp_draft(db, "PN1", "k1", rts_profile="IPSokolova") is None
    assert delete_rts_pp_draft(db, "PN1", "k1", rts_profile="IPSokolova") is False


def test_rts_pp_drafts_isolated_by_profile(tmp_path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="PN1",
        line_key="line1",
        ktru_code="K1",
        rts_draft_id=100,
        status="created",
        rts_profile="IPSokolova",
    )
    save_rts_pp_draft(
        db,
        purchase_number="PN1",
        line_key="line1",
        ktru_code="K1",
        rts_draft_id=200,
        status="created",
        rts_profile="Nacpro",
    )
    a = get_rts_pp_draft(db, "PN1", "line1", rts_profile="IPSokolova")
    b = get_rts_pp_draft(db, "PN1", "line1", rts_profile="Nacpro")
    assert a is not None and a["rts_draft_id"] == 100
    assert b is not None and b["rts_draft_id"] == 200

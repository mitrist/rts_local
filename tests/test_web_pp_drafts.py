from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from eis_analytics_db import (
    analytics_db_path,
    monitor_draft_line_key,
    save_pp_monitor_draft,
    save_rts_pp_draft,
    upsert_pp_monitor_registry_row,
)
from web_ui import main as web_main


def test_pp_drafts_page_lists_rows(monkeypatch, tmp_path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="0329300280426000050",
        line_key="line1",
        ktru_code="21.20.23.110-00000958",
        rts_draft_id=495778,
        status="created",
        created_at_utc=datetime(2026, 6, 9, 17, 18, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_load_pp_drafts_for_page", lambda _request, *, page=1, enrich_rts=True: (
        [
            {
                "purchase_number": "0329300280426000050",
                "line_key": "line1",
                "ktru_code": "21.20.23.110-00000958",
                "rts_draft_id": 495778,
                "protected_id": "",
                "edit_url": "https://offers.rts-tender.ru/supplier/price-lists/positions/offer-item/edit/495778",
                "offer_id": "",
                "status": "created",
                "error_text": "",
                "created_at_utc": "2026-06-09T17:18:49Z",
                "created_display_msk": "09.06.2026 20:18",
                "created_by": "",
                "fill_report_json": '{"matched": 2, "unmatched_notice": []}',
                "fill_summary": "2/2",
                "fill_warnings": [],
                "rts_status": "Draft",
                "price_list_item_id": 342872,
                "rts_live_ok": True,
            }
        ],
        {
            "cookie_ok": True,
            "cookie_message": "",
            "pagination": {
                "page": 1,
                "per_page": 30,
                "total_rows": 1,
                "total_pages": 1,
                "has_prev": False,
                "has_next": False,
                "prev_page": 1,
                "next_page": 1,
                "first_row": 1,
                "last_row": 1,
            },
        },
    ))

    client = TestClient(web_main.app)
    r = client.get("/pp-drafts")
    assert r.status_code == 200
    assert "Черновики ПП" in r.text
    assert "21.20.23.110-00000958" in r.text
    assert "495778" in r.text
    assert "/supplier/price-lists/positions" in r.text
    assert "2/2" in r.text
    assert "Удалить ПП" in r.text


def test_pp_drafts_page_includes_monitor_drafts(monkeypatch, tmp_path):
    db = analytics_db_path(tmp_path)
    oid = "oid-monitor-1"
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id=oid,
        ktru_code="26.20.13.000-00000002",
        name="Мониторная панель 65",
        link_date="2026-07-06T14:20:00",
    )
    save_pp_monitor_draft(
        db,
        offer_price_list_item_id=oid,
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=501001,
        edit_url="https://offers.rts-tender.ru/supplier/price-lists/positions/offer-item/edit/501001",
        status="created",
        created_at_utc=datetime(2026, 7, 7, 8, 30, tzinfo=timezone.utc),
        rts_profile="IPSokolova",
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_rts_profile_for_request", lambda _req: "IPSokolova")

    client = TestClient(web_main.app)
    r = client.get("/pp-drafts")
    assert r.status_code == 200
    assert "Образец конкурента" in r.text
    assert oid in r.text
    assert "501001" in r.text
    assert "26.20.13.000-00000002" in r.text
    assert "Мониторная панель 65" in r.text
    assert "07.07.2026" in r.text
    assert "Время добавления" in r.text
    assert "06.07.2026 17:20:00" in r.text


def test_pp_draft_delete_monitor_api(monkeypatch, tmp_path):
    db = analytics_db_path(tmp_path)
    oid = "oid-monitor-del"
    save_pp_monitor_draft(
        db,
        offer_price_list_item_id=oid,
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=501002,
        status="created",
        rts_profile="IPSokolova",
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: False)
    client = TestClient(web_main.app)
    lk = monitor_draft_line_key(oid)

    r = client.post(
        "/api/rts/pp/drafts/delete",
        json={"purchase_number": oid, "line_key": lk, "rts_profile": "IPSokolova"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_pp_drafts_admin_sees_all_operator_profiles(monkeypatch, tmp_path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="PN-IP",
        line_key="line-ip",
        ktru_code="21.20.23.110-00000958",
        rts_draft_id=495778,
        status="created",
        rts_profile="IPSokolova",
        created_at_utc=datetime(2026, 6, 9, 17, 18, tzinfo=timezone.utc),
    )
    save_rts_pp_draft(
        db,
        purchase_number="PN-NA",
        line_key="line-na",
        ktru_code="26.20.11.110-00000139",
        rts_draft_id=496701,
        status="created",
        rts_profile="Nacpro",
        created_at_utc=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "admin")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: True)

    client = TestClient(web_main.app)
    r = client.get("/pp-drafts")
    assert r.status_code == 200
    assert "PN-IP" in r.text
    assert "PN-NA" in r.text
    assert "IPSokolova" in r.text
    assert "Nacpro" in r.text
    assert "Режим администратора" in r.text


def test_pp_drafts_operator_sees_only_own_profile(monkeypatch, tmp_path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="PN-IP",
        line_key="line-ip",
        ktru_code="21.20.23.110-00000958",
        rts_draft_id=495778,
        status="created",
        rts_profile="IPSokolova",
    )
    save_rts_pp_draft(
        db,
        purchase_number="PN-NA",
        line_key="line-na",
        ktru_code="26.20.11.110-00000139",
        rts_draft_id=496701,
        status="created",
        rts_profile="Nacpro",
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: False)

    client = TestClient(web_main.app)
    r = client.get("/pp-drafts")
    assert r.status_code == 200
    assert "PN-IP" in r.text
    assert "PN-NA" not in r.text


def test_pp_draft_delete_api(monkeypatch, tmp_path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="0368200001526000007",
        line_key="line-notebook",
        ktru_code="26.20.11.110-00000139",
        rts_draft_id=496701,
        status="created",
        rts_profile="IPSokolova",
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: False)
    client = TestClient(web_main.app)

    r = client.post(
        "/api/rts/pp/drafts/delete",
        json={"purchase_number": "0368200001526000007", "line_key": "line-notebook"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert client.post(
        "/api/rts/pp/drafts/delete",
        json={"purchase_number": "0368200001526000007", "line_key": "line-notebook"},
    ).status_code == 404


def test_pp_drafts_page_pagination(monkeypatch, tmp_path):
    db = analytics_db_path(tmp_path)
    for i in range(31):
        save_rts_pp_draft(
            db,
            purchase_number=f"PN-{i:02d}",
            line_key=f"line-{i:02d}",
            ktru_code="21.20.23.110-00000958",
            rts_draft_id=500000 + i,
            status="created",
            rts_profile="IPSokolova",
            created_at_utc=datetime(2026, 6, 1, 12, i, tzinfo=timezone.utc),
        )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: False)
    monkeypatch.setattr(web_main, "_rts_profile_for_request", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "load_offers_cookie", lambda _p: "cookie")
    monkeypatch.setattr(web_main, "validate_cookie_session", lambda _c, probe=False: None)
    monkeypatch.setattr(web_main, "get_draft", lambda *_a, **_k: None)

    client = TestClient(web_main.app)
    r1 = client.get("/pp-drafts")
    assert r1.status_code == 200
    assert "Наши черновики (31)" in r1.text
    assert "из 31" in r1.text
    assert "PN-30" in r1.text
    assert "PN-00" not in r1.text
    assert "Следующая" in r1.text

    r2 = client.get("/pp-drafts?page=2")
    assert r2.status_code == 200
    assert "из 31" in r2.text
    assert "PN-00" in r2.text
    assert "PN-30" not in r2.text


def test_pp_drafts_rts_enrich_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RTS_PP_DRAFTS_ENRICH_RTS", raising=False)
    assert web_main._pp_drafts_enrich_rts_enabled() is False
    monkeypatch.setenv("RTS_PP_DRAFTS_ENRICH_RTS", "1")
    assert web_main._pp_drafts_enrich_rts_enabled() is True


def test_pp_drafts_page_skips_get_draft_without_enrich_env(monkeypatch, tmp_path):
    db = analytics_db_path(tmp_path)
    save_rts_pp_draft(
        db,
        purchase_number="0329300280426000050",
        line_key="line1",
        ktru_code="21.20.23.110-00000958",
        rts_draft_id=495778,
        status="created",
        rts_profile="IPSokolova",
        created_at_utc=datetime(2026, 6, 9, 17, 18, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "session_username", lambda _req: "IPSokolova")
    monkeypatch.setattr(web_main, "is_settings_admin", lambda _user: False)
    monkeypatch.setattr(web_main, "_rts_profile_for_request", lambda _req: "IPSokolova")
    monkeypatch.delenv("RTS_PP_DRAFTS_ENRICH_RTS", raising=False)
    calls: list[int] = []
    monkeypatch.setattr(web_main, "get_draft", lambda *args, **kwargs: calls.append(1) or {})

    client = TestClient(web_main.app)
    page = client.get("/pp-drafts")
    assert page.status_code == 200
    assert "495778" in page.text
    assert calls == []


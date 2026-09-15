from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "event_journal_sample.html"

from eis_analytics_db import connect, init_db, record_notice_event_journal_page, upsert_pp_eis_match, upsert_pp_monitor_registry_row
from web_ui import auth as web_auth
from web_ui import main as web_main


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_eis_pp_link_page_and_confirm(tmp_path: Path, monkeypatch):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO notice_html_pages(purchase_number, source_url, fetched_at_utc, ok, html_text)
            VALUES ('PN-1', 'http://x', '2026-07-01T00:00:00Z', 1, '<html></html>')
            """
        )
        conn.execute(
            """
            INSERT INTO notice_html_ktru_lines(
              line_key, purchase_number, ktru_code, position_display_name, unit_text,
              quantity_text, price_per_unit_text, line_sum_text, characteristics_count,
              characteristics_json, parsed_at_utc
            ) VALUES (
              'lk1', 'PN-1', '26.20.13.000-00000002', 'x', 'шт', '1',
              '73 300,00', '73 300,00', 1, '[]', '2026-07-01T00:00:00Z'
            )
            """
        )
        conn.commit()
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id="oid1",
        ktru_code="26.20.13.000-00000002",
        link_date="2026-07-01T12:15:00Z",
        unit_price=73300.0,
    )
    upsert_pp_eis_match(
        db,
        purchase_number="PN-1",
        line_key="lk1",
        offer_price_list_item_id="oid1",
        ktru_code="26.20.13.000-00000002",
        matched_count=2,
        total_compared=2,
        diff_json=json.dumps(
            [
                {
                    "label": "Test",
                    "notice_values": ["A"],
                    "pp_values": ["A"],
                    "status": "ok",
                }
            ],
            ensure_ascii=False,
        ),
        notice_pub_date_utc="2026-07-01T12:00:00Z",
        pp_link_date="2026-07-01T12:15:00Z",
        is_exact_match=True,
        lag_minutes=15.0,
    )
    record_notice_event_journal_page(
        db,
        purchase_number="PN-1",
        source_url="https://example/event-journal.html",
        html_text=FIXTURE.read_text(encoding="utf-8"),
        ok=True,
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")

    listing = client.get("/eis-pp-link")
    assert listing.status_code == 200
    assert "проверка гипотезы" in listing.text.lower() or "Этап 1" in listing.text
    assert "+15 мин" in listing.text
    assert "PN-1" in listing.text
    assert "01.07.2026 15:00:00" in listing.text
    assert "Публикация на ЕИС" in listing.text
    assert "Публикация ПП" in listing.text
    assert "zakupki.gov.ru" in listing.text
    assert "offers.rts-tender.ru/customer/offers/offer-item/oid1" in listing.text
    assert "73 300,00" in listing.text
    assert "Сумма закупки" in listing.text

    sorted_by_lag = client.get("/eis-pp-link", params={"sort": "lag", "dir": "desc"})
    assert sorted_by_lag.status_code == 200
    assert "PN-1" in sorted_by_lag.text

    detail = client.get(
        "/eis-pp-link",
        params={
            "purchase_number": "PN-1",
            "line_key": "lk1",
            "offer_id": "oid1",
        },
    )
    assert detail.status_code == 200
    assert "Цепочка событий" in detail.text
    assert "Публикация извещения" in detail.text
    assert "Публикация ПП" in detail.text
    assert "Diff характеристик" in detail.text
    assert "Гипотеза подтверждена" in detail.text
    assert "Сценарий" in detail.text
    assert "Участие" in detail.text
    assert "S1_pre_pp" in detail.text or "ПП до извещения" in detail.text

    confirm = client.post(
        "/action/eis-pp-link-status",
        data={
            "purchase_number": "PN-1",
            "line_key": "lk1",
            "offer_price_list_item_id": "oid1",
            "analyst_status": "confirmed",
            "analyst_note": "looks good",
            "scenario_tag": "S2_post_pp",
            "participation_intent": "would_join",
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 303

    from eis_analytics_db import get_pp_eis_match

    saved = get_pp_eis_match(db, purchase_number="PN-1", line_key="lk1", offer_price_list_item_id="oid1")
    assert saved is not None
    assert saved["analyst_status"] == "confirmed"
    assert saved["scenario_tag"] == "S2_post_pp"
    assert saved["participation_intent"] == "would_join"
    assert saved["scenario_tag_label"]
    assert saved["participation_intent_label"] == "Готов участвовать"

    tags_only = client.post(
        "/action/eis-pp-link-status",
        data={
            "purchase_number": "PN-1",
            "line_key": "lk1",
            "offer_price_list_item_id": "oid1",
            "tag_only": "1",
            "current_status": "confirmed",
            "analyst_note": "updated note",
            "scenario_tag": "S3_reuse",
            "participation_intent": "would_skip",
        },
        follow_redirects=False,
    )
    assert tags_only.status_code == 303
    saved2 = get_pp_eis_match(db, purchase_number="PN-1", line_key="lk1", offer_price_list_item_id="oid1")
    assert saved2 is not None
    assert saved2["analyst_status"] == "confirmed"
    assert saved2["scenario_tag"] == "S3_reuse"
    assert saved2["participation_intent"] == "would_skip"

    stats = client.get("/api/eis-pp-link/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["hypothesis"]["total_candidates"] >= 1
    assert body["by_status"].get("confirmed", 0) >= 1


def test_api_eis_pp_manual_register_json(tmp_path: Path, monkeypatch):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    from web_ui import auth as web_auth

    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")

    with patch("eis_pp_manual_register.register_manual_eis_pp_pair") as reg_mock:
        from eis_pp_manual_register import ManualRegisterResult

        reg_mock.return_value = ManualRegisterResult(
            offer_price_list_item_id="oid9",
            purchase_number="PN-9",
            line_key="lk9",
            matched_count=7,
            total_compared=7,
            is_exact_match=True,
            lag_minutes=12.0,
            analyst_status="pending",
            oktmo_match=True,
            oktmo_detail="ok",
            pp_link_date="2026-07-01T12:00:00Z",
        )
        resp = client.post(
            "/api/eis-pp/manual-register",
            json={
                "purchase_number": "PN-9",
                "line_key": "lk9",
                "offer_id": "oid9",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["offer_price_list_item_id"] == "oid9"
    assert "eis_pp_link_url" in body

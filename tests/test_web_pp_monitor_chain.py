from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from eis_analytics_db import (
    backfill_pp_characteristics_hashes,
    count_pp_monitor_stacks,
    list_pp_monitor_stack_members,
    list_pp_monitor_stacks,
    upsert_pp_monitor_finding,
    upsert_pp_monitor_registry_row,
)
from pp_stack import stack_key
from web_ui import auth as web_auth
from web_ui import main as web_main


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _seed_stack_pair(db: Path) -> str:
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
    backfill_pp_characteristics_hashes(db)
    from eis_analytics_db import get_pp_monitor_registry_row

    reg = get_pp_monitor_registry_row(db, "comp1")
    assert reg is not None
    chash = str(reg.get("characteristics_hash") or "")
    return stack_key(ktru, chash)


def test_pp_monitor_stacks_grouping(tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    sk = _seed_stack_pair(db)
    stacks = list_pp_monitor_stacks(db, min_members=2)
    assert len(stacks) == 1
    assert stacks[0]["member_count"] == 2
    assert stacks[0]["has_our_pp"] is True
    members = list_pp_monitor_stack_members(db, sk)
    ours = [m for m in members if m.get("is_our_pp")]
    assert len(ours) == 1
    assert ours[0]["offer_price_list_item_id"] == "our1"
    assert count_pp_monitor_stacks(db, min_members=2) == 1


def test_web_pp_monitor_chain_admin_only(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\noperator:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    sk = _seed_stack_pair(db)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)

    client = TestClient(web_main.app)
    r_op = client.get("/pp-monitor-chain", follow_redirects=False)
    assert r_op.status_code in (401, 403, 303)

    _login(client, "operator", "pass")
    r_op2 = client.get("/pp-monitor-chain", follow_redirects=False)
    assert r_op2.status_code == 303

    client.post("/login", data={"username": "admin", "password": "secret", "next": "/pp-monitor-chain"})
    r_admin = client.get("/pp-monitor-chain")
    assert r_admin.status_code == 200
    assert "Стопки" in r_admin.text

    r_detail = client.get(f"/pp-monitor-chain/{sk}")
    assert r_detail.status_code == 200
    assert "our1" in r_detail.text or "95000" in r_detail.text
    assert "Наше" in r_detail.text

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from eis_analytics_db import get_pp_monitor_finding, save_pp_monitor_draft, upsert_pp_eis_match, upsert_pp_monitor_finding, upsert_pp_monitor_registry_row
from web_ui import auth as web_auth
from web_ui import main as web_main


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/analytics"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _seed_registry(
    db: Path,
    oid: str = "sample1",
    *,
    name: str = "Интерактивная панель 65",
    seen_at_utc: datetime | None = None,
    unit_price_override: float | None = None,
) -> None:
    upsert_pp_monitor_registry_row(
        db,
        offer_price_list_item_id=oid,
        ktru_code="26.20.13.000-00000002",
        name=name,
        trade_mark="Test TM",
        link_date="2026-06-29T10:00:03",
        validity_date="2026-07-30T16:59:59",
        unit_price=100000.0 if unit_price_override is None else unit_price_override,
        delivery_regions_count=2,
        card_url=f"https://offers.rts-tender.ru/customer/offers/offer-item/{oid}",
        price_list_item_id=9001,
        seen_at_utc=seen_at_utc,
    )


def _seed_finding(db: Path, oid: str = "sample1") -> None:
    upsert_pp_monitor_finding(
        db,
        offer_price_list_item_id=oid,
        find_json=json.dumps(
            {
                "offerPriceListItemId": oid,
                "tradeMark": "Test TM",
                "createDateTime": "2026-06-29T10:00:00",
                "ktru": {"code": "26.20.13.000-00000002", "name": "Интерактивная панель"},
                "unit": {"name": "Штука"},
                "offerPriceListItemLinkDate": "2026-06-29T10:00:03",
                "validityDate": "2026-07-30T16:59:59",
                "deliveryPlaces": [
                    {"oktmo": {"code": "45000000", "name": "Москва"}},
                    {"oktmo": {"code": "40000000", "name": "СПб"}},
                    {"oktmo": {"code": "45000000", "name": "Москва"}},
                ],
                "ktruCharacteristics": [
                    {
                        "name": "Диагональ",
                        "characteristicValues": [{"concreteValue": "65"}],
                    }
                ],
            }
        ),
        ktru_code="26.20.13.000-00000002",
        trade_mark="Test TM",
        unit_price=100000.0,
        link_date="2026-06-29T10:00:03",
        card_url=f"https://offers.rts-tender.ru/customer/offers/offer-item/{oid}",
        source="notify",
    )


def test_pp_monitor_samples_operator_access(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\nIPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_registry(db)
    _seed_finding(db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)

    _login(client, "IPSokolova", "pass")
    listing = client.get("/pp-monitor-samples")
    assert listing.status_code == 200
    assert "Реестр предложений" in listing.text
    assert "Черновик поставщика" in listing.text
    assert "Интерактивная панель 65" in listing.text
    assert "Обновлено" not in listing.text
    assert "Создан черновик" in listing.text
    assert "Дата в реестре РТС" in listing.text
    assert "Срок ПП" in listing.text
    assert "Кол-во регионов поставки" in listing.text
    assert "9001" not in listing.text
    assert "Загрузить" not in listing.text
    assert ">2<" in listing.text
    assert "Диагональ" not in listing.text
    assert "Find" in listing.text
    assert "pp-registry-next-poll-timer" in listing.text

    filtered = client.get("/pp-monitor-samples", params={"ktru": "26.20.13.000-00000002"})
    assert filtered.status_code == 200
    assert "sample1" in filtered.text

    by_name = client.get(
        "/pp-monitor-samples",
        params={"name": "Интерактивная панель 65"},
    )
    assert by_name.status_code == 200
    assert "sample1" in by_name.text

    detail = client.get("/pp-monitor-samples/sample1")
    assert detail.status_code == 200
    assert "Диагональ" in detail.text
    assert "65" in detail.text
    assert "Создать черновик" in detail.text


def test_pp_monitor_samples_name_filter_persists_in_session(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_registry(db, oid="keep1", name="Keep me")
    _seed_registry(db, oid="drop1", name="Drop me")
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")

    applied = client.get(
        "/pp-monitor-samples",
        params={"apply_filters": 1, "name": "Keep me"},
    )
    assert applied.status_code == 200
    assert "keep1" in applied.text
    assert "drop1" not in applied.text

    refreshed = client.get("/pp-monitor-samples")
    assert refreshed.status_code == 200
    assert "keep1" in refreshed.text
    assert "drop1" not in refreshed.text

    cleared = client.get("/pp-monitor-samples", params={"reset_names": 1})
    assert cleared.status_code == 200
    assert "keep1" in cleared.text
    assert "drop1" in cleared.text


def test_pp_monitor_samples_unit_price_filter_pagination(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    base_seen = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    for i in range(55):
        _seed_registry(
            db,
            oid=f"cheap{i}",
            name=f"Cheap {i}",
            seen_at_utc=base_seen.replace(minute=i),
            unit_price_override=50000.0 + i,
        )
    for i in range(55):
        _seed_registry(
            db,
            oid=f"expensive{i}",
            name=f"Expensive {i}",
            seen_at_utc=base_seen.replace(hour=11, minute=i),
            unit_price_override=200000.0 + i,
        )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")

    page1 = client.get(
        "/pp-monitor-samples",
        params={"price_min": "100000", "apply_filters": 1},
    )
    assert page1.status_code == 200
    assert "expensive53" in page1.text
    assert "cheap0" not in page1.text
    assert "страница 1 из 2" in page1.text
    assert "price_min=100000" in page1.text

    page2 = client.get(
        "/pp-monitor-samples",
        params={"price_min": "100000", "page": 2},
    )
    assert page2.status_code == 200
    assert "expensive54" in page2.text
    assert "cheap0" not in page2.text


def test_pp_monitor_samples_pagination(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    base_seen = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    for i in range(55):
        _seed_registry(
            db,
            oid=f"row{i}",
            name=f"Item {i}",
            seen_at_utc=base_seen.replace(minute=i),
        )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")

    page1 = client.get("/pp-monitor-samples")
    assert page1.status_code == 200
    assert "страница 1 из 2" in page1.text
    assert "row0" in page1.text
    assert "row54" not in page1.text

    page2 = client.get("/pp-monitor-samples", params={"page": 2})
    assert page2.status_code == 200
    assert "row54" in page2.text
    assert "row0" not in page2.text


def test_pp_monitor_sample_detail_404(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: tmp_path / "empty.sqlite3")
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")
    r = client.get("/pp-monitor-samples/missing")
    assert r.status_code == 404


def test_operator_fetch_find_from_registry(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_registry(db, oid="seeded1")
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "load_customer_cookie", lambda _meta: "cookie")
    monkeypatch.setattr(
        web_main,
        "find_offer_item",
        lambda _cookie, oid: {
            "offerPriceListItemId": oid,
            "ktruCharacteristics": [],
            "ktru": {"code": "26.20.13.000-00000002"},
        },
    )
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")
    r = client.post(
        "/action/rts-pp-monitor-fetch-find",
        data={
            "offer_id": "seeded1",
            "return_to": "registry",
            "return_ktru": "",
            "return_q": "",
            "return_page": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/pp-monitor-samples" in r.headers["location"]
    saved = get_pp_monitor_finding(db, "seeded1")
    assert saved is not None
    assert saved["source"] == "admin_fetch"
    assert saved["ktru_code"] == "26.20.13.000-00000002"


def test_pp_monitor_samples_grouped_view(monkeypatch, tmp_path: Path):
    from tests.test_web_pp_monitor_chain import _seed_stack_pair

    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\nIPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_stack_pair(db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")

    flat = client.get("/pp-monitor-samples")
    assert flat.status_code == 200
    assert "первичное" not in flat.text

    grouped = client.get("/pp-monitor-samples", params={"group": 1})
    assert grouped.status_code == 200
    assert "групп" in grouped.text
    assert "первичное" in grouped.text
    assert "comp1" in grouped.text
    assert "our1" in grouped.text
    assert "pp-stack-child--collapsed" in grouped.text
    assert 'id="registry-group-stacks"' in grouped.text
    assert "checked" in grouped.text
    assert "2 версий" in grouped.text

    _login(client, "admin", "secret")
    admin_grouped = client.get("/pp-monitor-samples", params={"group": 1})
    assert admin_grouped.status_code == 200
    assert "Цепочка" in admin_grouped.text


def test_pp_monitor_samples_service_draft_indicator(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_registry(db, oid="drafted1")
    _seed_registry(db, oid="plain1", name="Plain item")
    save_pp_monitor_draft(
        db,
        offer_price_list_item_id="drafted1",
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=555,
        rts_profile="IPSokolova",
        status="created",
        created_at_utc=datetime(2026, 7, 9, 12, 30, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")

    page = client.get("/pp-monitor-samples")
    assert page.status_code == 200
    assert "pp-registry-draft-dot" in page.text
    assert "09.07.2026" in page.text or "2026" in page.text
    assert "drafted1" in page.text
    assert page.text.index("pp-registry-draft-dot") < page.text.index("drafted1")
    assert "plain1" in page.text
    plain_pos = page.text.index("plain1")
    assert page.text.rfind("pp-registry-draft-dot", 0, plain_pos) == -1 or page.text.index("pp-registry-draft-dot") < plain_pos

    save_pp_monitor_draft(
        db,
        offer_price_list_item_id="drafted1",
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=555,
        rts_profile="Nacpro",
        status="created",
    )
    page_other_profile = client.get("/pp-monitor-samples")
    assert page_other_profile.text.count("pp-registry-draft-dot") == 1

    from eis_analytics_db import delete_pp_monitor_draft

    delete_pp_monitor_draft(db, "drafted1", rts_profile="IPSokolova")
    after_delete = client.get("/pp-monitor-samples")
    assert "pp-registry-draft-dot" not in after_delete.text


def test_pp_monitor_samples_admin_sees_all_operator_drafts(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("admin:secret\nIPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    monkeypatch.setenv("RTS_SETTINGS_ADMIN_USERS", "admin")
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_registry(db, oid="ips-draft")
    _seed_registry(db, oid="nac-draft", name="Nac item")
    save_pp_monitor_draft(
        db,
        offer_price_list_item_id="ips-draft",
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=111,
        rts_profile="IPSokolova",
        status="created",
    )
    save_pp_monitor_draft(
        db,
        offer_price_list_item_id="nac-draft",
        ktru_code="26.20.13.000-00000002",
        rts_draft_id=222,
        rts_profile="Nacpro",
        status="created",
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "admin", "secret")

    page = client.get("/pp-monitor-samples")
    assert page.status_code == 200
    assert page.text.count("pp-registry-draft-dot") == 2
    assert "ips-draft" in page.text
    assert "nac-draft" in page.text

    _login(client, "IPSokolova", "pass")
    operator_page = client.get("/pp-monitor-samples")
    assert operator_page.text.count("pp-registry-draft-dot") == 1


def test_pp_monitor_samples_winner_row_highlight(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_registry(db, oid="winner1")
    _seed_registry(db, oid="plain1", name="Plain item")
    upsert_pp_eis_match(
        db,
        purchase_number="PN-WIN",
        line_key="lk1",
        offer_price_list_item_id="winner1",
        ktru_code="26.20.13.000-00000002",
        matched_count=1,
        total_compared=1,
        diff_json="[]",
        is_exact_match=True,
        match_origin="eis_winner_price",
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")

    page = client.get("/pp-monitor-samples")
    assert page.status_code == 200
    assert page.text.count("pp-registry-winner") == 1
    assert "победило в процедуре" in page.text


def test_pp_monitor_samples_manual_winner_row_highlight(monkeypatch, tmp_path: Path):
    users_file = tmp_path / "web_users.txt"
    users_file.write_text("IPSokolova:pass\n", encoding="utf-8")
    monkeypatch.setenv("RTS_WEB_USERS_FILE", str(users_file))
    web_auth.configure_web_auth(users_file)
    db = tmp_path / "eis_analytics.sqlite3"
    _seed_registry(db, oid="manual-win")
    upsert_pp_eis_match(
        db,
        purchase_number="PN-MAN",
        line_key="lk1",
        offer_price_list_item_id="manual-win",
        ktru_code="26.20.13.000-00000002",
        matched_count=1,
        total_compared=1,
        diff_json="[]",
        is_exact_match=True,
        match_origin="manual",
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    client = TestClient(web_main.app)
    _login(client, "IPSokolova", "pass")

    page = client.get("/pp-monitor-samples")
    assert page.status_code == 200
    assert page.text.count("pp-registry-winner") == 1
    assert page.text.count("pp-stack-badge--winner") == 1


def test_pp_winner_offer_ids_manual_match(tmp_path: Path):
    from eis_analytics_db import connect, init_db, pp_winner_offer_ids, upsert_pp_eis_match

    db = tmp_path / "eis_analytics.sqlite3"
    with connect(db) as conn:
        init_db(conn)
    upsert_pp_eis_match(
        db,
        purchase_number="PN-MAN",
        line_key="lk1",
        offer_price_list_item_id="manual-win",
        ktru_code="26.20.13.000-00000002",
        matched_count=1,
        total_compared=1,
        diff_json="[]",
        is_exact_match=False,
        match_origin="manual",
    )
    winners = pp_winner_offer_ids(db, ["manual-win", "other"])
    assert winners == {"manual-win"}

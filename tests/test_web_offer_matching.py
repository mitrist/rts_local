from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import participation_price as pp
from fastapi.testclient import TestClient

from eis_analytics_db import record_notice_html_page, record_rss_poll_run
from eis_notice_html_parse import parse_notice_common_info_html
from rts_ktru_cache import save_cached_template
from rts_preoffers import upsert_preoffer
from web_ui import main as web_main

KTRU = "08.12.12.140-00000005"
PRICE_ORD_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "price_ord_mini.csv"


def _use_price_ord_fixture(monkeypatch) -> None:
    monkeypatch.setenv("RTS_PRICE_ORD_FILE", str(PRICE_ORD_FIXTURE))
    pp.reset_catalog_cache()


def _seed_loaded_notice(db: Path) -> None:
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "0117300004426000001",
                "link": "https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber=0117300004426000001",
                "pub_date_utc": "2026-05-21T10:00:00Z",
                "customer_name": "Тестовый заказчик",
                "price_text": "2316600.00",
            }
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    body = Path(".cursor/plans/common_info.html").read_text(encoding="utf-8")
    lines = parse_notice_common_info_html(
        body,
        purchase_number="0117300004426000001",
        source_url="https://example.test/card",
    )
    record_notice_html_page(
        db,
        purchase_number="0117300004426000001",
        source_url="https://example.test/card",
        html_text=body,
        lines=lines,
        fetched_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )


def _seed_ktru_cache(cache_dir: Path, code: str = KTRU) -> None:
    save_cached_template(
        cache_dir,
        code,
        {"code": code, "name": "Щебень", "version": 1, "characteristics": []},
    )


def _seed_matching_preoffer(path: Path) -> None:
    upsert_preoffer(
        path,
        {
            "offer_id": "test-pass",
            "title": "Щебень гранитный М1200 F50 И4 20-40",
            "rts_url": "https://example.test/rts",
            "sku": "SKU",
            "ktru_code": KTRU,
            "enabled": True,
            "characteristics": [
                {"label": "Вид", "value": "Гранитный"},
                {"label": "Марка по дробимости щебня из горных пород", "value": "М1200"},
                {"label": "Марка по морозостойкости", "value": "F50"},
                {"label": "Марка по сопротивлению дроблению и износу", "value": "И4"},
                {"label": "Фракция (смесь фракций), мм", "value": "св. 20 до 40"},
            ],
        },
    )


def test_offer_matching_page_and_menu(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    cache = tmp_path / "ktru_rts_cache"
    _seed_loaded_notice(db)
    _seed_matching_preoffer(preoffers)
    _seed_ktru_cache(cache)
    _use_price_ord_fixture(monkeypatch)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    r = client.get("/offer-matching")

    assert r.status_code == 200
    assert "Подбор предложений" in r.text
    assert 'value="1" selected' in r.text
    assert "Участвовать" in r.text
    assert "Создание черновика ПП на РТС" in r.text
    assert 'id="offer-catalog-filter"' in r.text
    assert "0117300004426000001" in r.text
    assert "offer-profitability" in r.text
    assert "data-offer-column-controls" in r.text
    assert 'data-offer-col="customer"' in r.text
    assert "offer-col-resizer" in r.text
    assert "Участие в процедуре" in r.text
    assert "offer-manual-pp-block" in r.text
    assert "Id ПП" in r.text
    assert "offer-modal-customer" in r.text
    assert "offer-modal-price-unit" in r.text
    assert "offer-modal-unit" in r.text
    assert "offer-modal-quantity" in r.text
    assert 'data-offer-col="price_unit"' in r.text
    assert 'data-offer-col="notice_sum"' in r.text
    assert "Сумма в извещении" in r.text
    assert "2316600.00" in r.text
    assert "Расчетная цена участия" in r.text
    assert "2 200,00" in r.text
    assert "offer-modal-contract-start" in r.text
    assert "offer-modal-delivery-place" in r.text
    assert 'id="offer-ktru-search"' in r.text


def test_offer_matching_api_returns_comparison(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    cache = tmp_path / "ktru_rts_cache"
    _seed_loaded_notice(db)
    _seed_matching_preoffer(preoffers)
    _seed_ktru_cache(cache)
    _use_price_ord_fixture(monkeypatch)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    r = client.get("/api/offer-matching/0117300004426000001")

    data = r.json()
    assert r.status_code == 200
    assert data["status"] == "loaded"
    assert len(data["positions"]) == 1
    assert data["positions"][0]["can_participate"] is True
    best = data["positions"][0]["best_match"]
    assert best["passes"] is True
    assert best["matched"] == 5
    assert best["required"] == 5
    position = data["positions"][0]
    assert position["contract_start_date"] == "с даты заключения контракта"
    assert position["contract_execution_term"] == "09.09.2026"
    assert position["region"] == "Алтайский край"
    assert "ул. Садовая" in position["delivery_place"]
    assert position["delivery_days"] == "30"
    assert "Тонна" in (position.get("unit_text") or "")
    assert position.get("quantity_text") == "1 053,00"
    assert position.get("price_per_unit_text") == "2 200,00"
    econ = position.get("participation_economics") or {}
    assert econ.get("available") is True
    assert econ.get("condition") == 3
    assert econ.get("cpk")
    assert econ.get("unit_price_for_draft")


def test_offer_matching_api_returns_customer_characteristics_without_preoffers(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    _seed_loaded_notice(db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    r = client.get("/api/offer-matching/0117300004426000001")

    data = r.json()
    assert r.status_code == 200
    assert data["status"] == "loaded"
    assert len(data["positions"]) == 1
    position = data["positions"][0]
    assert position["best_match"] is None
    assert position["matches"] == []
    assert len(position["characteristics"]) == 5
    assert position["characteristics"][0]["display_label"] == "Вид"


def test_offer_matching_page_starts_from_rss_rows_without_html(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    record_rss_poll_run(
        db,
        [
            {
                "reg_number": "0117300004426000099",
                "link": "https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber=0117300004426000099",
                "pub_date_utc": "2026-05-21T11:00:00Z",
                "customer_name": "RSS заказчик",
                "price_text": "1000.00",
            }
        ],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 21, 11, 1, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 21, 11, 1, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    r = client.get("/offer-matching")
    api = client.get("/api/offer-matching/0117300004426000099")

    assert r.status_code == 200
    assert "0117300004426000099" in r.text
    assert "н/д" in r.text
    assert "карточка ЕИС не загружена" in r.text
    assert api.json()["status"] == "not_loaded"


def test_offer_matching_page_is_paginated(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    items = [
        {
            "reg_number": f"PN{i:04d}",
            "link": f"https://example.test/{i}",
            "pub_date_utc": f"2026-05-21T10:{i % 60:02d}:00Z",
            "customer_name": f"Заказчик {i}",
        }
        for i in range(31)
    ]
    record_rss_poll_run(
        db,
        items,
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 21, 11, 1, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 21, 11, 1, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    page1 = client.get("/offer-matching")
    page2 = client.get("/offer-matching?page=2")

    assert page1.status_code == 200
    assert "Показаны строки 1–30 из 31" in page1.text
    assert "PN0030" in page1.text
    assert "PN0000" not in page1.text
    assert page2.status_code == 200
    assert "Показаны строки 31–31 из 31" in page2.text
    assert "PN0000" in page2.text


def test_offer_matching_page_filters_by_ktru(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    _seed_loaded_notice(db)
    _seed_matching_preoffer(preoffers)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    found = client.get("/offer-matching?ktru=08.12")
    multi = client.get("/offer-matching?ktru=21.20,08.12")
    missing = client.get("/offer-matching?ktru=21.20")

    assert found.status_code == 200
    assert "0117300004426000001" in found.text
    assert 'value="08.12"' in found.text
    assert multi.status_code == 200
    assert "0117300004426000001" in multi.text
    normalized = client.get("/offer-matching?ktru=081212140")
    assert normalized.status_code == 200
    assert "0117300004426000001" in normalized.text
    assert missing.status_code == 200
    assert "По КТРУ «21.20» ничего не найдено." in missing.text
    assert "0117300004426000001" not in missing.text


def test_offer_matching_page_filters_by_catalog(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    _seed_loaded_notice(db)
    _seed_matching_preoffer(preoffers)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    catalog_rows = client.get("/offer-matching")
    all_rows = client.get("/offer-matching?catalog=0")
    empty_catalog = client.get("/offer-matching?catalog=1&ktru=21.20")

    assert catalog_rows.status_code == 200
    assert 'value="1" selected' in catalog_rows.text
    assert "0117300004426000001" in catalog_rows.text
    assert all_rows.status_code == 200
    assert "0117300004426000001" in all_rows.text
    assert empty_catalog.status_code == 200
    assert "По КТРУ «21.20» ничего не найдено." in empty_catalog.text


def test_offer_matching_api_participate_requires_catalog_and_pp_template(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    cache = tmp_path / "ktru_rts_cache"
    _seed_loaded_notice(db)
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    no_catalog = client.get("/api/offer-matching/0117300004426000001").json()["positions"][0]
    assert no_catalog["can_participate"] is False
    assert "справочнике предложений" in no_catalog["participate_reason"]

    _seed_matching_preoffer(preoffers)
    no_template = client.get("/api/offer-matching/0117300004426000001").json()["positions"][0]
    assert no_template["can_participate"] is False
    assert "draft_ktru_dict" in no_template["participate_reason"]

    _seed_ktru_cache(cache)
    ready = client.get("/api/offer-matching/0117300004426000001").json()["positions"][0]
    assert ready["can_participate"] is True


def test_offer_matching_participate_without_preoffer_catalog_when_env_disabled(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    cache = tmp_path / "ktru_rts_cache"
    _seed_loaded_notice(db)
    _seed_ktru_cache(cache)
    monkeypatch.setenv("RTS_PP_PARTICIPATE_REQUIRE_PREOFFER_CATALOG", "0")
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_KTRU_CACHE", cache)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    row = client.get("/api/offer-matching/0117300004426000001").json()["positions"][0]
    assert row["can_participate"] is True
    assert row["in_preoffer_catalog"] is False


def test_offer_matching_hides_profitability_when_price_ord_disabled(monkeypatch, tmp_path):
    db = tmp_path / "eis_analytics.sqlite3"
    preoffers = tmp_path / "rts_preoffers.json"
    _seed_loaded_notice(db)
    _seed_matching_preoffer(preoffers)
    monkeypatch.setenv("RTS_PARTICIPATION_PRICE_ORD_ENABLED", "0")
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_eis_analytics_db_path", lambda: db)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: preoffers)
    client = TestClient(web_main.app)

    page = client.get("/offer-matching")
    assert page.status_code == 200
    assert 'data-offer-col="profitability"' not in page.text
    row = client.get("/api/offer-matching/0117300004426000001").json()["positions"][0]
    assert row["participation_economics"]["error_reason"] == "disabled"
    assert row["participation_economics"]["profitability_text"] == "н/д"

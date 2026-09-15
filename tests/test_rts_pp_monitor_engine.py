from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rts_customer_pp_api import RegistryOfferSummary
from rts_pp_monitor import run_rts_pp_monitor_backfill, run_rts_pp_monitor_poll
from rts_pp_monitor_settings import save_settings, settings_path


def _summary(oid: str, *, link_date: str = "2026-07-03T10:00:00") -> RegistryOfferSummary:
    return RegistryOfferSummary(
        offer_price_list_item_id=oid,
        ktru_code="26.20.13.000-00000002",
        name="Панель",
        trade_mark="TM",
        link_date=link_date,
        validity_date="2026-12-31T20:59:59",
        unit_price=100000.0,
        delivery_regions_count=2,
        card_url=f"https://offers.rts-tender.ru/customer/offers/offer-item/{oid}",
    )


def _settings_file(tmp_path: Path, **extra: object) -> Path:
    settings_file = settings_path(tmp_path)
    payload = {
        "enabled": True,
        "ktru_codes": ["26.20.13.000-00000002"],
        "registry_backfill_done": True,
        "initial_seed_done": True,
        **extra,
    }
    save_settings(settings_file, payload)
    return settings_file


def _mock_customer_session(monkeypatch) -> None:
    monkeypatch.setattr("rts_pp_monitor.load_customer_cookie", lambda _meta: "cookie")
    monkeypatch.setattr("rts_pp_monitor.probe_customer_session", lambda *_a, **_k: {"ok": True})


def test_pp_monitor_backfill_seeds_registry(monkeypatch, tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    _settings_file(tmp_path, registry_backfill_done=False)
    summaries = [_summary("oid1"), _summary("oid2")]
    calls = {"n": 0}

    def fake_search(_cookie, _payload, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return summaries, 2
        return [], 2

    _mock_customer_session(monkeypatch)
    monkeypatch.setattr("rts_pp_monitor.search_registry_page", fake_search)
    monkeypatch.setattr("rts_pp_monitor.backfill_page_delay_sec", lambda: 0.0)
    monkeypatch.setattr("rts_pp_monitor.backfill_ktru_delay_sec", lambda: 0.0)
    monkeypatch.setattr("rts_pp_monitor.backfill_pages_per_run", lambda: 0)

    result = run_rts_pp_monitor_backfill(tmp_path, db)
    assert result["ok"] is True
    assert result["seeded"] == 2
    assert result["registry_backfill_done"] is True


def test_pp_monitor_incremental_stops_at_known(monkeypatch, tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    _settings_file(tmp_path)
    from eis_analytics_db import connect, init_db, record_pp_monitor_notification
    with connect(db) as conn:
        init_db(conn)
    record_pp_monitor_notification(
        db,
        offer_price_list_item_id="known1",
        ktru_code="26.20.13.000-00000002",
        channel="telegram_rts_pp_monitor",
        status="seeded",
    )
    find_mock = MagicMock(return_value={"offerPriceListItemId": "new1", "ktruCharacteristics": []})
    _mock_customer_session(monkeypatch)
    monkeypatch.setattr(
        "rts_pp_monitor.search_registry_page",
        lambda _c, _p, **_k: ([_summary("known1"), _summary("new1")], 2),
    )
    monkeypatch.setattr("rts_pp_monitor.find_offer_item", find_mock)
    monkeypatch.setattr("rts_pp_monitor.notify_find_enabled", lambda: True)

    senders = [("telegram_rts_pp_monitor", "tg", MagicMock(), "t", "c", 5.0, "html")]
    result = run_rts_pp_monitor_poll(tmp_path, db, force=True, senders=senders)
    assert result["items_new"] == 1
    assert result["notified"] == 1
    find_mock.assert_called_once_with("cookie", "new1")


def test_pp_monitor_full_pages_scans_past_known_first_page(monkeypatch, tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    _settings_file(tmp_path)
    from eis_analytics_db import connect, init_db, record_pp_monitor_notification

    with connect(db) as conn:
        init_db(conn)
    for i in range(10):
        record_pp_monitor_notification(
            db,
            offer_price_list_item_id=f"known{i}",
            ktru_code="26.20.13.000-00000002",
            channel="telegram_rts_pp_monitor",
            status="sent",
        )
    pages = [[_summary(f"known{i}") for i in range(10)], [_summary("new2")]]
    calls = {"n": 0}

    def fake_search(_cookie, payload, **_kw):
        idx = calls["n"]
        calls["n"] += 1
        if idx < len(pages):
            return pages[idx], 2
        return [], 2

    monkeypatch.setenv("RTS_PP_MONITOR_FULL_PAGES", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_MAX_PER_RUN", "20")
    _mock_customer_session(monkeypatch)
    monkeypatch.setattr("rts_pp_monitor.search_registry_page", fake_search)
    monkeypatch.setattr("rts_pp_monitor.find_offer_item", MagicMock(return_value=None))
    monkeypatch.setattr("rts_pp_monitor.notify_find_enabled", lambda: False)

    result = run_rts_pp_monitor_poll(tmp_path, db, force=True)
    assert calls["n"] >= 2
    assert result["pages_fetched"] >= 2
    assert result["items_new"] == 1


def test_pp_monitor_poll_requires_backfill(monkeypatch, tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    _settings_file(tmp_path, registry_backfill_done=False)
    _mock_customer_session(monkeypatch)
    result = run_rts_pp_monitor_poll(tmp_path, db, force=True)
    assert result["skipped"] is True
    assert result["reason"] == "backfill_pending"


def test_pp_monitor_notify_message_format():
    from rts_pp_monitor import pp_monitor_notify_message

    msg = pp_monitor_notify_message(
        _summary("oid1", link_date="2026-06-29T10:00:03"),
        None,
        links="markdown",
    )
    assert "Новая запись в реестре ПП" in msg
    assert "26.20.13.000-00000002" in msg
    assert "Панель" in msg
    assert "Дата добавления:" in msg
    assert "Кол-во регионов поставки: 2" in msg
    assert "Цена за ед.: 100000" in msg
    assert "Карточка ПП на РТС" in msg
    assert "Образец в консоли" not in msg
    assert "Товарный знак" not in msg
    assert "Цена:" not in msg


def test_pp_monitor_poll_probes_cookie_before_search(monkeypatch, tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    _settings_file(tmp_path)
    calls = {"search": 0}

    def fake_search(*_a, **_k):
        calls["search"] += 1
        return [], 0

    _mock_customer_session(monkeypatch)
    monkeypatch.setattr(
        "rts_pp_monitor.probe_customer_session",
        lambda *_a, **_k: {"ok": False, "reason": "session_expired", "detail": "expired"},
    )
    monkeypatch.setattr("rts_pp_monitor.search_registry_page", fake_search)

    result = run_rts_pp_monitor_poll(tmp_path, db, force=True)
    assert result["ok"] is False
    assert result["reason"] == "session_expired"
    assert calls["search"] == 0


def test_pp_monitor_poll_stops_after_session_expired(monkeypatch, tmp_path: Path):
    from rts_customer_pp_api import RtsCustomerPpApiError

    db = tmp_path / "analytics.sqlite3"
    _settings_file(
        tmp_path,
        ktru_codes=[
            "26.20.13.000-00000002",
            "26.20.15.000-00000037",
            "26.20.18.000-00000069",
        ],
    )
    calls = {"search": 0}

    def fake_search(*_a, **_k):
        calls["search"] += 1
        raise RtsCustomerPpApiError("expired", reason="session_expired")

    _mock_customer_session(monkeypatch)
    monkeypatch.setattr("rts_pp_monitor.probe_customer_session", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr("rts_pp_monitor.search_registry_page", fake_search)
    monkeypatch.setattr("rts_pp_monitor.notify_find_enabled", lambda: False)

    result = run_rts_pp_monitor_poll(tmp_path, db, force=True)
    assert calls["search"] == 1
    assert result["ok"] is False
    assert result["reason"] == "session_expired"

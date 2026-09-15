from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rts_customer_pp_api import RegistryOfferSummary
from rts_pp_monitor import run_rts_pp_monitor_poll
from rts_pp_monitor_cron import PollFileLock, cron_status_payload, run_poll_once, save_cron_state
from rts_pp_monitor_settings import save_settings, settings_path


def _summary(oid: str) -> RegistryOfferSummary:
    return RegistryOfferSummary(
        offer_price_list_item_id=oid,
        ktru_code="26.20.13.000-00000002",
        name="Панель",
        trade_mark="TM",
        link_date="2026-07-03T10:00:00",
        validity_date="2026-12-31T20:59:59",
        unit_price=100000.0,
        delivery_regions_count=2,
        card_url=f"https://offers.rts-tender.ru/customer/offers/offer-item/{oid}",
    )


def _settings_file(tmp_path: Path, **extra: object) -> None:
    save_settings(
        settings_path(tmp_path),
        {
            "enabled": True,
            "ktru_codes": ["26.20.13.000-00000002"],
            "registry_backfill_done": True,
            "initial_seed_done": True,
            **extra,
        },
    )


def _mock_customer_session(monkeypatch) -> None:
    monkeypatch.setattr("rts_pp_monitor.load_customer_cookie", lambda _meta: "cookie")
    monkeypatch.setattr("rts_pp_monitor.probe_customer_session", lambda *_a, **_k: {"ok": True})


def test_pp_monitor_full_pages_fetches_all_pages(monkeypatch, tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    _settings_file(tmp_path)
    pages = [[_summary("new1")], [_summary("new2")]]
    calls = {"n": 0}

    def fake_search(_cookie, payload, **_kw):
        idx = calls["n"]
        calls["n"] += 1
        if idx < len(pages):
            return pages[idx], 2
        return [], 2

    monkeypatch.setenv("RTS_PP_MONITOR_FULL_PAGES", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_MAX_PER_RUN", "1")
    _mock_customer_session(monkeypatch)
    monkeypatch.setattr("rts_pp_monitor.search_registry_page", fake_search)
    monkeypatch.setattr("rts_pp_monitor._search_take", lambda _s: 1)
    monkeypatch.setattr("rts_pp_monitor.find_offer_item", MagicMock(return_value=None))
    monkeypatch.setattr("rts_pp_monitor.notify_find_enabled", lambda: False)

    result = run_rts_pp_monitor_poll(tmp_path, db, force=True)
    assert result["pages_fetched"] >= 2
    assert result["items_new"] == 2


def test_pp_monitor_stops_pages_without_full_pages(monkeypatch, tmp_path: Path):
    db = tmp_path / "analytics.sqlite3"
    _settings_file(tmp_path)
    pages = [[_summary("new1")], [_summary("new2")]]
    calls = {"n": 0}

    def fake_search(_cookie, payload, **_kw):
        idx = calls["n"]
        calls["n"] += 1
        if idx < len(pages):
            return pages[idx], 2
        return [], 2

    monkeypatch.setenv("RTS_PP_MONITOR_FULL_PAGES", "0")
    monkeypatch.setenv("RTS_PP_MONITOR_MAX_PER_RUN", "1")
    _mock_customer_session(monkeypatch)
    monkeypatch.setattr("rts_pp_monitor.search_registry_page", fake_search)
    senders = [("telegram_rts_pp_monitor", "tg", MagicMock(), "t", "c", 5.0, "html")]
    monkeypatch.setattr(
        "rts_pp_monitor.find_offer_item",
        MagicMock(return_value={"offerPriceListItemId": "x", "ktruCharacteristics": []}),
    )
    monkeypatch.setattr("rts_pp_monitor.notify_find_enabled", lambda: True)

    result = run_rts_pp_monitor_poll(tmp_path, db, force=True, senders=senders)
    assert result["pages_fetched"] == 1
    assert result["notified"] == 1


def test_cron_run_writes_state(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    monkeypatch.setenv("EIS_ANALYTICS_DB", "analytics.sqlite3")
    db = tmp_path / "analytics.sqlite3"
    _settings_file(tmp_path)
    monkeypatch.setattr(PollFileLock, "acquire", lambda self: True)
    monkeypatch.setattr(PollFileLock, "release", lambda self: None)
    monkeypatch.setattr(
        "rts_pp_monitor_cron.run_rts_pp_monitor_poll",
        lambda *_a, **_k: {"ok": True, "pages_fetched": 3, "items_new": 0, "notified": 0, "notify_failed": 0, "ktru_polled": 1},
    )
    monkeypatch.setattr("rts_pp_monitor_cron.build_pp_monitor_senders", lambda _t: [])

    result = run_poll_once(force=True)
    assert result["ok"] is True
    state_path = tmp_path / "rts_pp_monitor_cron_state.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["running"] is False
    assert state["pages_fetched"] == 3


def test_cron_status_payload(monkeypatch, tmp_path: Path):
    from datetime import datetime, timezone

    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    monkeypatch.setenv("RTS_PP_MONITOR", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_CRON", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_INTERVAL_SEC", "420")
    _settings_file(tmp_path, poll_interval_min=10)
    finished = "2026-07-07T06:00:00Z"
    save_cron_state(
        {
            "running": False,
            "finished_at_utc": finished,
            "interval_sec": 600,
            "ok": True,
        }
    )
    finished_ts = datetime.fromisoformat(finished.replace("Z", "+00:00")).timestamp()
    payload = cron_status_payload(now=finished_ts)
    assert payload["enabled"] is True
    assert payload["cron_mode"] is True
    assert payload["interval_sec"] == 600
    assert payload["seconds_until_next"] == 600


def test_poll_file_lock_recovers_from_stale_pid(tmp_path: Path):
    try:
        import fcntl as fcntl_mod
    except ImportError:
        pytest.skip("fcntl not available")

    if fcntl_mod is None:
        pytest.skip("fcntl not available")

    lock_path = tmp_path / "poll.lock"
    lock_path.write_text("99999999", encoding="utf-8")
    lock = PollFileLock(lock_path)
    assert lock.acquire() is True
    lock.release()


def test_cron_status_ignores_dead_running_pid(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    monkeypatch.setenv("RTS_PP_MONITOR", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_CRON", "1")
    _settings_file(tmp_path)
    save_cron_state({"running": True, "pid": 99999999, "started_at_utc": "2026-07-07T10:00:00Z"})
    payload = cron_status_payload(now=1_000_000_000)
    assert payload["polling"] is False


def test_cron_status_ignores_stale_running_poll(monkeypatch, tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    monkeypatch.setenv("RTS_PP_MONITOR", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_CRON", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_STALE_POLL_SEC", "600")
    _settings_file(tmp_path)
    started = (datetime.now(timezone.utc) - timedelta(minutes=15)).replace(microsecond=0)
    save_cron_state(
        {
            "running": True,
            "pid": os.getpid(),
            "started_at_utc": started.isoformat().replace("+00:00", "Z"),
        }
    )
    payload = cron_status_payload()
    assert payload["polling"] is False


def test_cron_status_poll_due_after_interval(monkeypatch, tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    monkeypatch.setenv("RTS_PP_MONITOR", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_CRON", "1")
    _settings_file(tmp_path, poll_interval_min=8)
    finished = datetime.now(timezone.utc) - timedelta(minutes=10)
    save_cron_state({"running": False, "finished_at_utc": finished.isoformat().replace("+00:00", "Z")})
    payload = cron_status_payload()
    assert payload["polling"] is False
    assert payload["seconds_until_next"] == 0
    assert payload["poll_due"] is True


def test_cron_status_running_without_lock_is_not_polling(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    monkeypatch.setenv("RTS_PP_MONITOR", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_CRON", "1")
    _settings_file(tmp_path)
    save_cron_state(
        {
            "running": True,
            "pid": os.getpid(),
            "started_at_utc": "2026-07-07T10:00:00Z",
        }
    )
    payload = cron_status_payload(now=1_000_000_000)
    assert payload["polling"] is False
    state = json.loads((tmp_path / "rts_pp_monitor_cron_state.json").read_text(encoding="utf-8"))
    assert state["running"] is False


def test_cron_status_stale_running_without_finish_is_poll_due(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    monkeypatch.setenv("RTS_PP_MONITOR", "1")
    monkeypatch.setenv("RTS_PP_MONITOR_CRON", "1")
    _settings_file(tmp_path)
    save_cron_state({"running": True, "pid": 99999999, "started_at_utc": "2026-07-07T10:00:00Z"})
    payload = cron_status_payload(now=1_000_000_000)
    assert payload["polling"] is False
    assert payload["seconds_until_next"] == 0
    assert payload["poll_due"] is True


def test_run_poll_once_clears_running_on_crash(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RTS_META_DIR", str(tmp_path))
    monkeypatch.setenv("EIS_ANALYTICS_DB", "analytics.sqlite3")
    _settings_file(tmp_path)
    monkeypatch.setattr(PollFileLock, "acquire", lambda self: True)
    monkeypatch.setattr(PollFileLock, "release", lambda self: None)

    def boom(*_a, **_k):
        raise RuntimeError("poll crashed")

    monkeypatch.setattr("rts_pp_monitor_cron.run_rts_pp_monitor_poll", boom)
    monkeypatch.setattr("rts_pp_monitor_cron.build_pp_monitor_senders", lambda _t: [])

    result = run_poll_once(force=True)
    assert result["ok"] is False
    state = json.loads((tmp_path / "rts_pp_monitor_cron_state.json").read_text(encoding="utf-8"))
    assert state["running"] is False
    assert state["reason"] == "internal_error"

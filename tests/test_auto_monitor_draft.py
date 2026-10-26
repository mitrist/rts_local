"""Tests for auto_monitor_draft engine and queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import auto_monitor_draft as amd
from auto_monitor_draft_settings import save_settings, settings_path
from eis_analytics_db import (
    claim_next_auto_monitor_draft,
    count_auto_monitor_draft_pending,
    enqueue_auto_monitor_draft,
    get_auto_monitor_draft_queue_row,
    list_auto_monitor_draft_queue,
    update_auto_monitor_draft_queue,
)


def _find_item(*, unit_price: str = "1000,00", qty: str = "10") -> dict:
    return {
        "ktru": {"code": "27.51.11.110-00000009"},
        "deliveryPlaces": [
            {
                "unitPrice": unit_price,
                "minQuantity": qty,
                "oktmo": {"name": "Москва"},
            }
        ],
    }


def test_compute_pp_sum_and_no_quantity():
    assert amd.compute_pp_sum(_find_item()) == 10000.0
    assert amd.compute_pp_sum({"deliveryPlaces": [{"unitPrice": "100"}]}) is None
    assert amd.compute_pp_sum(_find_item(qty=""), fallback_unit_price=50) is None


def test_matches_rules():
    settings = {
        "enabled": True,
        "ktru_codes": ["27.51.11.110-00000009"],
        "min_pp_amount": 5000,
    }
    ok, reason = amd.matches_rules(
        ktru_code="27.51.11.110-00000009", pp_sum=10000, settings=settings
    )
    assert ok and reason == ""
    ok, reason = amd.matches_rules(
        ktru_code="27.51.11.110-00000009", pp_sum=100, settings=settings
    )
    assert not ok and reason == "min_amount"
    ok, reason = amd.matches_rules(ktru_code="OTHER", pp_sum=10000, settings=settings)
    assert not ok and reason == "ktru_whitelist"
    ok, reason = amd.matches_rules(
        ktru_code="27.51.11.110-00000009", pp_sum=None, settings=settings
    )
    assert not ok and reason == "no_quantity"


def test_queue_enqueue_claim_idempotent(tmp_path: Path):
    db = tmp_path / "a.sqlite3"
    assert enqueue_auto_monitor_draft(
        db,
        offer_price_list_item_id="oid1",
        rts_profile="IPSokolova",
        ktru_code="K1",
        pp_sum=12345,
    )
    assert enqueue_auto_monitor_draft(
        db,
        offer_price_list_item_id="oid1",
        rts_profile="IPSokolova",
        ktru_code="K1",
        pp_sum=12345,
    )
    assert count_auto_monitor_draft_pending(db) == 1
    job = claim_next_auto_monitor_draft(db, rts_profile="IPSokolova")
    assert job is not None
    assert job["offer_price_list_item_id"] == "oid1"
    assert job["status"] == "processing"
    assert count_auto_monitor_draft_pending(db) == 0
    assert claim_next_auto_monitor_draft(db, rts_profile="IPSokolova") is None

    update_auto_monitor_draft_queue(
        db, "oid1", rts_profile="IPSokolova", status="error", error_text="boom"
    )
    assert enqueue_auto_monitor_draft(
        db, offer_price_list_item_id="oid1", rts_profile="IPSokolova", pp_sum=1
    )
    row = get_auto_monitor_draft_queue_row(db, "oid1", rts_profile="IPSokolova")
    assert row["status"] == "pending"


def test_try_enqueue_respects_env_and_rules(monkeypatch, tmp_path: Path):
    db = tmp_path / "a.sqlite3"
    save_settings(
        settings_path(tmp_path),
        {
            "enabled": True,
            "ktru_codes": ["27.51.11.110-00000009"],
            "min_pp_amount": 5000,
            "rts_profile": "IPSokolova",
        },
    )
    monkeypatch.setenv("RTS_AUTO_MONITOR_DRAFT", "0")
    assert not amd.try_enqueue_from_finding(
        db,
        tmp_path,
        offer_price_list_item_id="oidA",
        find_item=_find_item(),
        ktru_code="27.51.11.110-00000009",
    )
    monkeypatch.setenv("RTS_AUTO_MONITOR_DRAFT", "1")
    assert amd.try_enqueue_from_finding(
        db,
        tmp_path,
        offer_price_list_item_id="oidA",
        find_item=_find_item(),
        ktru_code="27.51.11.110-00000009",
    )
    assert count_auto_monitor_draft_pending(db) == 1
    assert not amd.try_enqueue_from_finding(
        db,
        tmp_path,
        offer_price_list_item_id="oidB",
        find_item=_find_item(qty="1", unit_price="10"),
        ktru_code="27.51.11.110-00000009",
    )


def test_process_queue_happy_and_hour_limit(monkeypatch, tmp_path: Path):
    db = tmp_path / "a.sqlite3"
    save_settings(
        settings_path(tmp_path),
        {
            "enabled": True,
            "ktru_codes": ["K"],
            "min_pp_amount": 1,
            "max_per_hour": 1,
            "rts_profile": "IPSokolova",
        },
    )
    monkeypatch.setenv("RTS_AUTO_MONITOR_DRAFT", "1")
    enqueue_auto_monitor_draft(
        db, offer_price_list_item_id="o1", rts_profile="IPSokolova", ktru_code="K", pp_sum=10
    )
    enqueue_auto_monitor_draft(
        db, offer_price_list_item_id="o2", rts_profile="IPSokolova", ktru_code="K", pp_sum=20
    )

    calls: list[str] = []

    def fake_create(**kwargs):
        oid = kwargs["offer_price_list_item_id"]
        calls.append(oid)
        return SimpleNamespace(
            ok=True,
            already_existed=False,
            rts_draft_id=100 + len(calls),
            reason="",
            error="",
        )

    monkeypatch.setattr(amd, "create_pp_draft_from_monitor", fake_create)
    summary = amd.process_auto_monitor_draft_queue(tmp_path, db, project_root=tmp_path, limit=5)
    assert summary.done == 1
    assert summary.stopped_reason == "hour_limit"
    assert calls == ["o1"]
    rows = {r["offer_price_list_item_id"]: r for r in list_auto_monitor_draft_queue(db, limit=10)}
    assert rows["o1"]["status"] == "created"
    assert rows["o2"]["status"] == "pending"


def test_process_queue_auto_publish_prepare(monkeypatch, tmp_path: Path):
    db = tmp_path / "a.sqlite3"
    save_settings(
        settings_path(tmp_path),
        {
            "enabled": True,
            "auto_publish": True,
            "ktru_codes": ["K"],
            "min_pp_amount": 1,
            "max_per_hour": 10,
            "rts_profile": "IPSokolova",
        },
    )
    monkeypatch.setenv("RTS_AUTO_MONITOR_DRAFT", "1")
    enqueue_auto_monitor_draft(
        db, offer_price_list_item_id="o1", rts_profile="IPSokolova", ktru_code="K", pp_sum=10
    )

    def fake_create(**kwargs):
        return SimpleNamespace(
            ok=True,
            already_existed=False,
            rts_draft_id=555,
            reason="",
            error="",
        )

    def fake_start(**kwargs):
        return SimpleNamespace(
            ok=True,
            status="awaiting_signature",
            error="",
            reason="",
            job_id=9,
        )

    monkeypatch.setattr(amd, "create_pp_draft_from_monitor", fake_create)
    monkeypatch.setattr(amd, "publish_enabled", lambda: True)
    monkeypatch.setattr(amd, "start_publish_job", fake_start)
    summary = amd.process_auto_monitor_draft_queue(tmp_path, db, project_root=tmp_path, limit=5)
    assert summary.done == 1
    assert summary.publish_prepared == 1
    row = get_auto_monitor_draft_queue_row(db, "o1", rts_profile="IPSokolova")
    assert row["status"] == "awaiting_signature"
    assert row["rts_draft_id"] == 555


def test_process_queue_auto_publish_catchup(monkeypatch, tmp_path: Path):
    """Drafts left as created/done get prepare when auto_publish is on."""
    db = tmp_path / "a.sqlite3"
    save_settings(
        settings_path(tmp_path),
        {
            "enabled": True,
            "auto_publish": True,
            "ktru_codes": ["K"],
            "min_pp_amount": 1,
            "max_per_hour": 10,
            "rts_profile": "IPSokolova",
        },
    )
    monkeypatch.setenv("RTS_AUTO_MONITOR_DRAFT", "1")
    enqueue_auto_monitor_draft(
        db, offer_price_list_item_id="o_created", rts_profile="IPSokolova", ktru_code="K", pp_sum=10
    )
    enqueue_auto_monitor_draft(
        db, offer_price_list_item_id="o_legacy", rts_profile="IPSokolova", ktru_code="K", pp_sum=20
    )
    update_auto_monitor_draft_queue(
        db,
        "o_created",
        rts_profile="IPSokolova",
        status="created",
        rts_draft_id=701,
    )
    update_auto_monitor_draft_queue(
        db,
        "o_legacy",
        rts_profile="IPSokolova",
        status="done",
        rts_draft_id=702,
    )

    started: list[int] = []

    def fake_start(**kwargs):
        started.append(int(kwargs["rts_draft_id"]))
        return SimpleNamespace(
            ok=True,
            status="awaiting_signature",
            error="",
            reason="",
            job_id=len(started),
        )

    monkeypatch.setattr(
        amd,
        "create_pp_draft_from_monitor",
        lambda **kw: (_ for _ in ()).throw(AssertionError("create should not run")),
    )
    monkeypatch.setattr(amd, "publish_enabled", lambda: True)
    monkeypatch.setattr(amd, "start_publish_job", fake_start)

    summary = amd.process_auto_monitor_draft_queue(tmp_path, db, project_root=tmp_path, limit=5)
    assert summary.publish_prepared == 2
    assert set(started) == {701, 702}
    rows = {
        r["offer_price_list_item_id"]: r
        for r in list_auto_monitor_draft_queue(db, limit=10, rts_profile="IPSokolova")
    }
    assert rows["o_created"]["status"] == "awaiting_signature"
    assert rows["o_legacy"]["status"] == "awaiting_signature"


def test_process_queue_cookie_stop(monkeypatch, tmp_path: Path):
    db = tmp_path / "a.sqlite3"
    save_settings(
        settings_path(tmp_path),
        {
            "enabled": True,
            "ktru_codes": ["K"],
            "min_pp_amount": 1,
            "max_per_hour": 10,
            "rts_profile": "IPSokolova",
        },
    )
    monkeypatch.setenv("RTS_AUTO_MONITOR_DRAFT", "1")
    enqueue_auto_monitor_draft(
        db, offer_price_list_item_id="o1", rts_profile="IPSokolova", ktru_code="K", pp_sum=10
    )
    enqueue_auto_monitor_draft(
        db, offer_price_list_item_id="o2", rts_profile="IPSokolova", ktru_code="K", pp_sum=20
    )

    def fake_create(**kwargs):
        return SimpleNamespace(
            ok=False,
            already_existed=False,
            rts_draft_id=None,
            reason="session_expired",
            error="cookie dead",
        )

    monkeypatch.setattr(amd, "create_pp_draft_from_monitor", fake_create)
    summary = amd.process_auto_monitor_draft_queue(tmp_path, db, project_root=tmp_path, limit=5)
    assert summary.cookie_expired == 1
    assert summary.stopped_reason == "cookie_expired"
    assert count_auto_monitor_draft_pending(db) == 1

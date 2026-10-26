from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import auto_participate as ap
from auto_participate_settings import parse_profile_rules, save_settings, settings_path
from eis_analytics_db import analytics_db_path, record_notice_html_page, record_rss_poll_run
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from rts_ktru_cache import save_cached_template
from rts_pp_draft_service import DraftResult
from rts_preoffers import RtsPreoffer, RtsPreofferCharacteristic, upsert_preoffer


KTRU = "08.12.12.140-00000005"


def _offer() -> RtsPreoffer:
    return RtsPreoffer(
        offer_id="offer-1",
        title="Щебень",
        sku="SKU",
        ktru_code=KTRU,
        rts_url="https://example.test/rts",
        enabled=True,
        characteristics=[
            RtsPreofferCharacteristic(match_key="name:параметр", label="Параметр", value="да")
        ],
    )


def _row(*, line_sum: str = "500000,00") -> dict:
    return {
        "ktru_code": KTRU,
        "line_sum_text": line_sum,
        "characteristics": [
            {
                "match_key": "name:параметр",
                "display_label": "Параметр",
                "value_display": "да",
            }
        ],
    }


def test_row_matches_rules_disabled():
    ok, reason = ap.row_matches_rules(
        _row(),
        rules=parse_profile_rules({"enabled": False}),
        min_amount=None,
        offers=[_offer()],
        ktru_cache_dir=Path("/tmp"),
    )
    assert ok is False
    assert reason == "disabled"


def test_row_matches_rules_min_amount():
    ok, reason = ap.row_matches_rules(
        _row(line_sum="100000,00"),
        rules=parse_profile_rules({"enabled": True}),
        min_amount=200000,
        offers=[_offer()],
        ktru_cache_dir=Path("/tmp"),
    )
    assert ok is False
    assert reason == "min_amount"


def test_row_matches_rules_whitelist():
    cache = Path("/tmp/ktru_cache_test")
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "Щебень", "version": 1, "characteristics": []})
    ok, reason = ap.row_matches_rules(
        _row(),
        rules=parse_profile_rules({"enabled": True, "ktru_mode": "whitelist", "ktru_codes": ["OTHER-CODE"]}),
        min_amount=None,
        offers=[_offer()],
        ktru_cache_dir=cache,
    )
    assert ok is False
    assert reason == "ktru_whitelist"


def test_row_matches_rules_catalog_without_template(tmp_path):
    ok, reason = ap.row_matches_rules(
        _row(),
        rules=parse_profile_rules({"enabled": True, "ktru_mode": "catalog"}),
        min_amount=None,
        offers=[_offer()],
        ktru_cache_dir=tmp_path / "empty_cache",
    )
    assert ok is True
    assert reason == ""


def test_row_matches_rules_min_ratio_skipped_without_chars(tmp_path):
    row = _row()
    row["characteristics"] = []
    ok, reason = ap.row_matches_rules(
        row,
        rules=parse_profile_rules({"enabled": True, "ktru_mode": "catalog", "min_match_ratio": 0.9}),
        min_amount=None,
        offers=[_offer()],
    )
    assert ok is True
    assert reason == ""


def test_row_matches_rules_etp_allowlist(tmp_path):
    cache = tmp_path / "ktru_cache"
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "Щебень", "version": 1, "characteristics": []})
    row = _row()
    row["etp_name"] = "АО «Сбербанк-АСТ»"
    ok, reason = ap.row_matches_rules(
        row,
        rules=parse_profile_rules({"enabled": True, "ktru_mode": "catalog"}),
        min_amount=None,
        offers=[_offer()],
        ktru_cache_dir=cache,
        allowed_etp=["РТС-тендер"],
    )
    assert ok is False
    assert reason == "etp"

    ok2, reason2 = ap.row_matches_rules(
        row,
        rules=parse_profile_rules({"enabled": True, "ktru_mode": "catalog"}),
        min_amount=None,
        offers=[_offer()],
        ktru_cache_dir=cache,
        allowed_etp=["АО «Сбербанк-АСТ»"],
    )
    assert ok2 is True
    assert reason2 == ""


def test_run_auto_participate_creates_draft(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "offer-1",
            "title": "Щебень",
            "sku": "SKU",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [{"match_key": "name:параметр", "label": "Параметр", "value": "да"}],
        },
    )
    cache = meta / "ktru_rts_cache"
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "Щебень", "version": 1, "characteristics": []})
    cookie = meta / "cookies" / "IPSokolova.txt"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")

    pn = "0117300004426000001"
    line = KtruPurchaseLine(
        line_key=f"{pn}|{KTRU}|line1",
        purchase_number=pn,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=KTRU,
        position_display_name="Щебень",
        line_sum_display="500000,00",
        characteristics=[
            CustomerCharacteristic(
                match_key="name:параметр",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="Параметр",
                value_display="да",
            )
        ],
    )
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": f"https://example.test/{pn}", "pub_date_utc": "2026-06-25T10:00:00Z"}],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url="https://example.test/card",
        html_text="<html></html>",
        lines=[line],
        fetched_at_utc=datetime(2026, 6, 25, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )

    settings = {
        "enabled": True,
        "profiles": {
            "IPSokolova": parse_profile_rules({"enabled": True, "min_purchase_amount": 100000}),
            "Nacpro": parse_profile_rules({"enabled": False}),
        }
    }
    save_settings(settings_path(meta), settings)

    created_calls: list[tuple[str, str]] = []

    def fake_create(**kwargs):
        created_calls.append((kwargs["purchase_number"], kwargs["line_key"]))
        return DraftResult(
            ok=True,
            rts_draft_id=555,
            edit_url="https://example.test/edit/555",
            ktru_code=KTRU,
        )

    monkeypatch.setattr(ap, "create_pp_draft_from_notice", fake_create)
    monkeypatch.setattr(ap, "validate_cookie_session", lambda cookie: None)
    monkeypatch.setattr(ap, "load_offers_cookie", lambda path: "cookie=value")

    summary = ap.run_auto_participate_after_html(
        analytics_db=db,
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=preoffers,
    )
    assert summary.created == 1
    assert len(created_calls) == 1
    assert created_calls[0][0] == pn

    summary2 = ap.run_auto_participate_after_html(
        analytics_db=db,
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=preoffers,
    )
    assert summary2.created == 0
    assert len(created_calls) == 1


def test_run_auto_participate_off_when_env_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("RTS_AUTO_PARTICIPATE", raising=False)
    summary = ap.run_auto_participate_after_html(
        analytics_db=analytics_db_path(tmp_path),
        history_db=tmp_path / "history.sqlite3",
        meta_dir=tmp_path,
        project_root=tmp_path,
        preoffers_path=tmp_path / "preoffers.json",
    )
    assert summary.profiles_checked == 0
    assert summary.created == 0


def test_run_auto_participate_off_when_global_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    meta = tmp_path / "meta"
    meta.mkdir()
    save_settings(
        settings_path(meta),
        {
            "enabled": False,
            "profiles": {
                "IPSokolova": parse_profile_rules({"enabled": True}),
                "Nacpro": parse_profile_rules({"enabled": True}),
            },
        },
    )
    summary = ap.run_auto_participate_after_html(
        analytics_db=analytics_db_path(tmp_path),
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=tmp_path / "preoffers.json",
    )
    assert summary.profiles_checked == 0
    assert summary.created == 0


def test_cookie_alert_rate_limited(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    meta = tmp_path / "meta"
    meta.mkdir()
    settings = {
        "enabled": True,
        "profiles": {
            "IPSokolova": parse_profile_rules({"enabled": True}),
            "Nacpro": parse_profile_rules({"enabled": False}),
        }
    }
    save_settings(settings_path(meta), settings)
    alerts: list[str] = []

    def notify(_profile: str, msg: str) -> None:
        alerts.append(msg)

    ap._last_cookie_alert_at.clear()
    summary1 = ap.run_auto_participate_after_html(
        analytics_db=analytics_db_path(tmp_path),
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=tmp_path / "preoffers.json",
        notify_cookie_alert=notify,
    )
    summary2 = ap.run_auto_participate_after_html(
        analytics_db=analytics_db_path(tmp_path),
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=tmp_path / "preoffers.json",
        notify_cookie_alert=notify,
    )
    assert summary1.cookie_expired == 1
    assert len(alerts) == 1
    assert summary2.cookie_expired == 1
    assert len(alerts) == 1


def test_run_auto_participate_records_cookie_skip_for_matching_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "offer-1",
            "title": "Щебень",
            "sku": "SKU",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [{"match_key": "name:параметр", "label": "Параметр", "value": "да"}],
        },
    )
    cache = meta / "ktru_rts_cache"
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "Щебень", "version": 1, "characteristics": []})

    pn = "0117300004426000001"
    line = KtruPurchaseLine(
        line_key=f"{pn}|{KTRU}|line1",
        purchase_number=pn,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=KTRU,
        position_display_name="Щебень",
        line_sum_display="500000,00",
        characteristics=[
            CustomerCharacteristic(
                match_key="name:параметр",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="Параметр",
                value_display="да",
            )
        ],
    )
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": f"https://example.test/{pn}", "pub_date_utc": "2026-06-25T10:00:00Z"}],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url="https://example.test/card",
        html_text="<html></html>",
        lines=[line],
        fetched_at_utc=datetime(2026, 6, 25, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    settings = {
        "enabled": True,
        "profiles": {
            "IPSokolova": parse_profile_rules({"enabled": True, "min_purchase_amount": 100000}),
            "Nacpro": parse_profile_rules({"enabled": False}),
        }
    }
    save_settings(settings_path(meta), settings)

    from eis_analytics_db import get_auto_participate_attempt

    summary = ap.run_auto_participate_after_html(
        analytics_db=db,
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=preoffers,
    )
    assert summary.cookie_expired == 1
    assert summary.skipped_blocked == 1
    attempt = get_auto_participate_attempt(db, pn, line.line_key, rts_profile="IPSokolova")
    assert attempt is not None
    assert attempt["status"] == "cookie_expired"
    assert attempt["error_text"]


def test_run_auto_participate_does_not_overwrite_created_with_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "offer-1",
            "title": "Щебень",
            "sku": "SKU",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [{"match_key": "name:параметр", "label": "Параметр", "value": "да"}],
        },
    )
    cache = meta / "ktru_rts_cache"
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "Щебень", "version": 1, "characteristics": []})

    pn = "0117300004426000001"
    line_key = f"{pn}|{KTRU}|line1"
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": f"https://example.test/{pn}", "pub_date_utc": "2026-06-25T10:00:00Z"}],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url="https://example.test/card",
        html_text="<html></html>",
        lines=[
            KtruPurchaseLine(
                line_key=line_key,
                purchase_number=pn,
                object_sid="",
                object_external_sid="",
                position_index=0,
                ktru_code=KTRU,
                position_display_name="Щебень",
                line_sum_display="500000,00",
                characteristics=[],
            )
        ],
        fetched_at_utc=datetime(2026, 6, 25, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    settings = {
        "enabled": True,
        "profiles": {
            "IPSokolova": parse_profile_rules({"enabled": True, "min_purchase_amount": 100000}),
            "Nacpro": parse_profile_rules({"enabled": False}),
        }
    }
    save_settings(settings_path(meta), settings)

    from eis_analytics_db import get_auto_participate_attempt, record_auto_participate_attempt

    record_auto_participate_attempt(
        db,
        purchase_number=pn,
        line_key=line_key,
        rts_profile="IPSokolova",
        status="created",
        ktru_code=KTRU,
        rts_draft_id=777,
        edit_url="https://example.test/edit/777",
    )

    summary = ap.run_auto_participate_after_html(
        analytics_db=db,
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=preoffers,
    )
    assert summary.skipped_blocked == 0
    attempt = get_auto_participate_attempt(db, pn, line_key, rts_profile="IPSokolova")
    assert attempt is not None
    assert attempt["status"] == "created"
    assert attempt["rts_draft_id"] == 777


def test_run_auto_participate_auto_publish_prepare(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    monkeypatch.setenv("RTS_PP_PUBLISH_ENABLED", "1")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "offer-1",
            "title": "Щебень",
            "sku": "SKU",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [{"match_key": "name:параметр", "label": "Параметр", "value": "да"}],
        },
    )
    cache = meta / "ktru_rts_cache"
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "Щебень", "version": 1, "characteristics": []})
    cookie = meta / "cookies" / "IPSokolova.txt"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")

    pn = "0117300004426000099"
    line = KtruPurchaseLine(
        line_key=f"{pn}|{KTRU}|line1",
        purchase_number=pn,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=KTRU,
        position_display_name="Щебень",
        line_sum_display="500000,00",
        characteristics=[
            CustomerCharacteristic(
                match_key="name:параметр",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="Параметр",
                value_display="да",
            )
        ],
    )
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": f"https://example.test/{pn}", "pub_date_utc": "2026-05-21T10:00:00Z"}],
        query_label="test",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url="https://example.test/card",
        html_text="<html/>",
        lines=[line],
        fetched_at_utc=datetime(2026, 5, 21, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )

    save_settings(
        settings_path(meta),
        {
            "enabled": True,
            "profiles": {
                "IPSokolova": parse_profile_rules(
                    {"enabled": True, "auto_publish": True, "min_purchase_amount": 100000}
                ),
                "Nacpro": parse_profile_rules({"enabled": False}),
            },
        },
    )

    def fake_create(**kwargs):
        return DraftResult(
            ok=True,
            rts_draft_id=888,
            edit_url="https://example.test/edit/888",
            ktru_code=KTRU,
        )

    class _Pub:
        ok = True
        status = "awaiting_signature"
        error = ""
        reason = ""

    monkeypatch.setattr(ap, "create_pp_draft_from_notice", fake_create)
    monkeypatch.setattr(ap, "validate_cookie_session", lambda cookie: None)
    monkeypatch.setattr(ap, "load_offers_cookie", lambda path: "cookie=value")
    monkeypatch.setattr(ap, "publish_enabled", lambda: True)
    monkeypatch.setattr(ap, "start_publish_job", lambda **kwargs: _Pub())

    from eis_analytics_db import get_auto_participate_attempt

    summary = ap.run_auto_participate_after_html(
        analytics_db=db,
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=preoffers,
    )
    assert summary.created == 1
    assert summary.publish_prepared == 1
    attempt = get_auto_participate_attempt(db, pn, line.line_key, rts_profile="IPSokolova")
    assert attempt is not None
    assert attempt["status"] == "awaiting_signature"


def test_run_auto_participate_publish_catchup_for_created(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    monkeypatch.setenv("RTS_PP_PUBLISH_ENABLED", "1")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "offer-1",
            "title": "Щебень",
            "sku": "SKU",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [{"match_key": "name:параметр", "label": "Параметр", "value": "да"}],
        },
    )
    cache = meta / "ktru_rts_cache"
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "Щебень", "version": 1, "characteristics": []})
    cookie = meta / "cookies" / "IPSokolova.txt"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")

    from eis_analytics_db import get_auto_participate_attempt, record_auto_participate_attempt

    pn = "0117300004426000199"
    lk = f"{pn}|{KTRU}|line1"
    record_auto_participate_attempt(
        db,
        purchase_number=pn,
        line_key=lk,
        rts_profile="IPSokolova",
        status="created",
        ktru_code=KTRU,
        rts_draft_id=777,
        edit_url="https://example.test/edit/777",
    )
    save_settings(
        settings_path(meta),
        {
            "enabled": True,
            "profiles": {
                "IPSokolova": parse_profile_rules(
                    {"enabled": True, "auto_publish": True, "min_purchase_amount": 100000}
                ),
                "Nacpro": parse_profile_rules({"enabled": False}),
            },
        },
    )

    class _Pub:
        ok = True
        status = "awaiting_signature"
        error = ""
        reason = ""

    monkeypatch.setattr(ap, "validate_cookie_session", lambda cookie: None)
    monkeypatch.setattr(ap, "load_offers_cookie", lambda path: "cookie=value")
    monkeypatch.setattr(ap, "publish_enabled", lambda: True)
    monkeypatch.setattr(ap, "start_publish_job", lambda **kwargs: _Pub())
    monkeypatch.setattr(ap, "auto_participate_candidates", lambda *a, **k: [])

    summary = ap.run_auto_participate_after_html(
        analytics_db=db,
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=preoffers,
    )
    assert summary.publish_prepared == 1
    attempt = get_auto_participate_attempt(db, pn, lk, rts_profile="IPSokolova")
    assert attempt is not None
    assert attempt["status"] == "awaiting_signature"


def test_notice_has_auto_participate_ktru_catalog_and_whitelist():
    offers = [_offer()]
    settings = {
        "enabled": True,
        "profiles": {
            "IPSokolova": parse_profile_rules(
                {"enabled": True, "ktru_mode": "catalog", "min_purchase_amount": 100000}
            ),
            "Nacpro": parse_profile_rules({"enabled": False}),
        },
    }
    assert ap.notice_has_auto_participate_ktru([KTRU], settings=settings, offers=offers) is True
    assert ap.notice_has_auto_participate_ktru(["OTHER"], settings=settings, offers=offers) is False

    settings["profiles"]["IPSokolova"] = parse_profile_rules(
        {
            "enabled": True,
            "ktru_mode": "whitelist",
            "ktru_codes": ["ONLY-THIS"],
            "min_purchase_amount": 100000,
        }
    )
    assert ap.notice_has_auto_participate_ktru([KTRU], settings=settings, offers=offers) is False
    assert ap.notice_has_auto_participate_ktru(["ONLY-THIS"], settings=settings, offers=offers) is True


def test_run_auto_participate_focuses_purchase_number(monkeypatch, tmp_path):
    monkeypatch.setenv("RTS_AUTO_PARTICIPATE", "1")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "offer-1",
            "title": "Щебень",
            "sku": "SKU",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [{"match_key": "name:параметр", "label": "Параметр", "value": "да"}],
        },
    )
    cache = meta / "ktru_rts_cache"
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "Щебень", "version": 1, "characteristics": []})
    cookie = meta / "cookies" / "IPSokolova.txt"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    save_settings(
        settings_path(meta),
        {
            "enabled": True,
            "profiles": {
                "IPSokolova": parse_profile_rules(
                    {"enabled": True, "min_purchase_amount": 100000}
                ),
                "Nacpro": parse_profile_rules({"enabled": False}),
            },
        },
    )

    pn_focus = "0117300004426000101"
    pn_other = "0117300004426000102"
    created_pns: list[str] = []

    for pn in (pn_focus, pn_other):
        line = KtruPurchaseLine(
            line_key=f"{pn}|{KTRU}|line1",
            purchase_number=pn,
            object_sid="",
            object_external_sid="",
            position_index=0,
            ktru_code=KTRU,
            position_display_name="Щебень",
            line_sum_display="500000,00",
            characteristics=[
                CustomerCharacteristic(
                    match_key="name:параметр",
                    characteristic_kind="manual",
                    characteristic_code=None,
                    display_label="Параметр",
                    value_display="да",
                )
            ],
        )
        record_rss_poll_run(
            db,
            [{"reg_number": pn, "link": f"https://example.test/{pn}", "pub_date_utc": "2026-06-25T10:00:00Z"}],
            query_label="test",
            rss_url="https://example.test/rss",
            started_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
            finished_at_utc=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
            ok=True,
            errors=[],
        )
        record_notice_html_page(
            db,
            purchase_number=pn,
            source_url=f"https://example.test/{pn}",
            html_text="<html></html>",
            lines=[line],
            fetched_at_utc=datetime(2026, 6, 25, 10, 1, tzinfo=timezone.utc),
            ok=True,
            http_status=200,
            page_context={"etp_name": "РТС-тендер"},
        )

    def _create(**kwargs):
        created_pns.append(str(kwargs.get("purchase_number") or ""))
        return DraftResult(
            ok=True,
            already_existed=False,
            rts_draft_id=1000 + len(created_pns),
            edit_url=f"https://example.test/edit/{len(created_pns)}",
            reason="",
            error="",
            http_status=200,
        )

    monkeypatch.setattr(ap, "validate_cookie_session", lambda cookie: None)
    monkeypatch.setattr(ap, "load_offers_cookie", lambda path: "cookie=value")
    monkeypatch.setattr(ap, "create_pp_draft_from_notice", _create)
    monkeypatch.setattr(ap, "publish_enabled", lambda: False)

    summary = ap.run_auto_participate_after_html(
        analytics_db=db,
        history_db=tmp_path / "history.sqlite3",
        meta_dir=meta,
        project_root=tmp_path,
        preoffers_path=preoffers,
        purchase_number=pn_focus,
    )
    assert summary.created == 1
    assert created_pns == [pn_focus]

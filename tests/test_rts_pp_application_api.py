from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from rts_pp_application_api import (
    PRELIMINARY_OFFERS_PURCHASE,
    RtsPpApplicationApiError,
    parse_application_summary,
    parse_auction_search_row,
    parse_auction_search_response,
    parse_procedure_summary,
    resolve_procedure,
    search_by_notification_number,
)

_FIXTURES = Path(__file__).resolve().parents[1] / ".cursor" / "investigate"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_procedure_summary_with_own_application():
    summary = parse_procedure_summary(_load("model2.json"))
    assert summary is not None
    assert summary.notification_number == "0330300112026000008"
    assert summary.url_trade_id == "GoZIu0ymYS3q"
    assert summary.numeric_trade_id == 108013625
    assert summary.ktru_code == "31.01.11.150-00000003"
    assert summary.has_own_application is True
    assert summary.own_application_url_id == "v7Q1uoKJVpyq"
    assert summary.last_application_id == 121349747


def test_parse_procedure_summary_without_own_application_ignores_spurious_url_id():
    summary = parse_procedure_summary(_load("dump3.json"))
    assert summary is not None
    assert summary.notification_number == "0391300004326000011"
    assert summary.last_application_id == 0
    assert summary.last_application_url_id == "WpSvO8VcSrxV"
    assert summary.has_own_application is False
    assert summary.own_application_url_id is None


def test_parse_application_summary():
    summary = parse_application_summary(_load("model.json"))
    assert summary is not None
    assert summary.application_id == 121349747
    assert summary.application_url_id == "v7Q1uoKJVpyq"
    assert summary.application_state == "RejectedByPart2"
    assert summary.quotation_amount == 260000.0
    assert summary.notification_number == "0330300112026000008"


def test_parse_auction_search_row_from_dump4_shape():
    raw = {
        "TradeId": "v390c4R78pUq",
        "TradeLotId": "VGyNSCBXmn1v",
        "NotificationNumber": "0331200017826000130",
        "PurchaseMethod": PRELIMINARY_OFFERS_PURCHASE,
        "PurchaseMethodString": "Электронная закупка, осуществляемая в соответствии с ч. 12 ст. 93 44-ФЗ",
        "TradeName": "Комплект изделий",
        "Region": "Воронежская обл.",
        "OrganizerName": "БУЗ ВО ВОКБ №1",
        "StartPrice": 64000.0,
        "LotState": 5,
        "LotStateString": "Подведение итогов",
        "HasApplications": True,
        "ParticipantHasActiveApplication": False,
        "ActiveApplicationId": None,
    }
    row = parse_auction_search_row(raw)
    assert row is not None
    assert row.url_trade_id == "v390c4R78pUq"
    assert row.has_applications is True
    assert row.participant_has_active_application is False


def test_parse_auction_search_response_wrapper():
    rows, total = parse_auction_search_response(
        {
            "Success": True,
            "Data": {
                "Data": [
                    {
                        "TradeId": "v390c4R78pUq",
                        "TradeLotId": "VGyNSCBXmn1v",
                        "NotificationNumber": "0331200017826000130",
                        "PurchaseMethod": PRELIMINARY_OFFERS_PURCHASE,
                        "TradeName": "x",
                        "Region": "r",
                        "OrganizerName": "o",
                        "StartPrice": 1,
                        "LotState": 5,
                        "LotStateString": "s",
                        "HasApplications": False,
                        "ParticipantHasActiveApplication": False,
                    }
                ],
                "Count": 1,
            },
        }
    )
    assert total == 1
    assert len(rows) == 1
    assert rows[0].notification_number == "0331200017826000130"


def test_resolve_procedure_no_participation_skips_application_fetch(monkeypatch):
    session = MagicMock()

    def fake_search(_session, _notice, **_kw):
        return parse_auction_search_row(
            {
                "TradeId": "v390c4R78pUq",
                "TradeLotId": "VGyNSCBXmn1v",
                "NotificationNumber": "0331200017826000130",
                "PurchaseMethod": PRELIMINARY_OFFERS_PURCHASE,
                "PurchaseMethodString": "ЭЗТ",
                "TradeName": "x",
                "Region": "r",
                "OrganizerName": "o",
                "StartPrice": 64000,
                "LotState": 5,
                "LotStateString": "s",
                "HasApplications": True,
                "ParticipantHasActiveApplication": False,
            }
        )

    monkeypatch.setattr("rts_pp_application_api.search_by_notification_number", fake_search)
    monkeypatch.setattr(
        "rts_pp_application_api.fetch_procedure_model",
        lambda *_a, **_k: _load("dump3.json"),
    )
    lot_called = {"n": 0}

    def fake_lot_items(_session, _trade_id, **_kw):
        lot_called["n"] += 1
        return {"LotItems": [], "TotalRows": 0}

    monkeypatch.setattr("rts_pp_application_api.fetch_lot_items_search_paged", fake_lot_items)
    app_called = {"n": 0}

    def fake_app(*_a, **_k):
        app_called["n"] += 1
        return {}

    monkeypatch.setattr("rts_pp_application_api.fetch_application_model", fake_app)

    bundle = resolve_procedure(session, notification_number="0391300004326000011")
    assert bundle.procedure is not None
    assert bundle.application is None
    assert bundle.skip_reason == "no_own_application"
    assert lot_called["n"] == 1
    assert app_called["n"] == 0


def test_resolve_procedure_with_application(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(
        "rts_pp_application_api.fetch_procedure_model",
        lambda *_a, **_k: _load("model2.json"),
    )
    monkeypatch.setattr(
        "rts_pp_application_api.fetch_lot_items_search_paged",
        lambda *_a, **_k: {"LotItems": [{"ClassificationCode": "31.01.11.150-00000003"}], "TotalRows": 1},
    )
    monkeypatch.setattr(
        "rts_pp_application_api.fetch_application_model",
        lambda *_a, **_k: _load("model.json"),
    )

    bundle = resolve_procedure(
        session,
        url_trade_id="GoZIu0ymYS3q",
        fetch_lot_items=True,
        fetch_application=True,
    )
    assert bundle.procedure is not None
    assert bundle.procedure.has_own_application is True
    assert bundle.application is not None
    assert bundle.application.application_state == "RejectedByPart2"
    assert bundle.lot_items is not None
    assert bundle.skip_reason == ""


def test_resolve_procedure_not_ezt_stops_early(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(
        "rts_pp_application_api.search_by_notification_number",
        lambda *_a, **_k: parse_auction_search_row(
            {
                "TradeId": "vmnpc1pro64x",
                "TradeLotId": "Jx1r7cYkpQmq",
                "NotificationNumber": "0366300033826000408",
                "PurchaseMethod": "Auction20",
                "PurchaseMethodString": "Электронный аукцион",
                "TradeName": "x",
                "Region": "r",
                "OrganizerName": "o",
                "StartPrice": 1,
                "LotState": 20,
                "LotStateString": "s",
                "HasApplications": False,
                "ParticipantHasActiveApplication": False,
            }
        ),
    )
    proc_called = {"n": 0}

    def fake_proc(*_a, **_k):
        proc_called["n"] += 1
        return {}

    monkeypatch.setattr("rts_pp_application_api.fetch_procedure_model", fake_proc)

    bundle = resolve_procedure(session, notification_number="0366300033826000408")
    assert bundle.skip_reason == "not_preliminary_offers_purchase"
    assert proc_called["n"] == 0


def test_search_by_notification_number_empty_raises():
    with pytest.raises(RtsPpApplicationApiError, match="notification_number"):
        search_by_notification_number(MagicMock(), "")


def test_lk_origin_candidates_prefer_app(monkeypatch):
    monkeypatch.delenv("RTS_APP_ORIGIN", raising=False)
    from rts_pp_application_api import APP_ORIGIN, GOST_ORIGIN, lk_origin_candidates

    assert lk_origin_candidates()[0] == APP_ORIGIN
    assert GOST_ORIGIN in lk_origin_candidates()
    monkeypatch.setenv("RTS_APP_ORIGIN", "https://app-gost.rts-tender.ru")
    assert lk_origin_candidates() == ["https://app-gost.rts-tender.ru"]


def test_is_waf_body_detects_anti_ddos():
    from rts_pp_application_api import is_waf_body

    html = "<!DOCTYPE html><html><head><title>Anti-DDoS защита</title></head></html>"
    assert is_waf_body(html, 503) is True
    assert is_waf_body('{"Success":true}', 200) is False


def test_fetch_auction_search_uses_session_origin():
    from rts_pp_application_api import GOST_ORIGIN, fetch_auction_search

    session = MagicMock()
    session.headers = {"Origin": GOST_ORIGIN}
    resp = MagicMock()
    resp.status_code = 200
    resp.url = GOST_ORIGIN + "/supplier/lk/api/auction/search"
    resp.text = '{"Success":true,"Data":{"Data":[],"Count":0}}'
    resp.json.return_value = {"Success": True, "Data": {"Data": [], "Count": 0}}
    session.request.return_value = resp
    rows, total = fetch_auction_search(session, {"ktruCode": "x"})
    assert rows == []
    assert total == 0
    url = session.request.call_args.args[1]
    assert url.startswith(GOST_ORIGIN)
    assert url.endswith("/supplier/lk/api/auction/search")


def test_request_json_short_waf_error(monkeypatch):
    from rts_pp_application_api import GOST_ORIGIN, _request_json

    monkeypatch.setattr("rts_pp_application_api.WAF_RETRY_ATTEMPTS", 1)
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 503
    resp.url = GOST_ORIGIN + "/supplier/lk/api/auction/search"
    resp.text = "<!DOCTYPE html><html><title>Anti-DDoS защита</title></html>"
    session.request.return_value = resp
    with pytest.raises(RtsPpApplicationApiError, match="анти-DDoS") as exc:
        _request_json(session, "POST", resp.url, json_body={})
    assert exc.value.reason == "waf"
    assert "DOCTYPE" not in str(exc.value)


def test_open_lk_session_falls_back_from_waf(monkeypatch):
    from rts_pp_application_api import APP_ORIGIN, GOST_ORIGIN, open_lk_session_for_ezt

    calls: list[str] = []

    def fake_search(session, ktru, **_kw):
        origin = session.headers.get("Origin")
        calls.append(origin)
        if origin == APP_ORIGIN:
            raise RtsPpApplicationApiError("waf app", reason="waf")
        return [], 0

    monkeypatch.delenv("RTS_APP_ORIGIN", raising=False)
    monkeypatch.setattr("rts_pp_application_api.warmup_lk_session", lambda *_a, **_k: "ok")
    monkeypatch.setattr("rts_pp_application_api.search_ezt_by_ktru", fake_search)
    session, origin, rows, total = open_lk_session_for_ezt("Auth=1", probe_ktru="31.01.12.139-00000001")
    assert origin == GOST_ORIGIN
    assert rows == []
    assert total == 0
    assert calls == [APP_ORIGIN, GOST_ORIGIN]
    assert session.headers["Origin"] == GOST_ORIGIN


def test_open_lk_session_skips_network_warmup(monkeypatch):
    from rts_pp_application_api import APP_ORIGIN, GOST_ORIGIN, open_lk_session_for_ezt

    calls: list[str] = []

    def fake_warm(session, timeout=8.0):
        origin = session.headers.get("Origin")
        return "network" if origin == APP_ORIGIN else "ok"

    def fake_search(session, ktru, **_kw):
        calls.append(session.headers.get("Origin"))
        return [], 0

    monkeypatch.delenv("RTS_APP_ORIGIN", raising=False)
    monkeypatch.setattr("rts_pp_application_api.warmup_lk_session", fake_warm)
    monkeypatch.setattr("rts_pp_application_api.search_ezt_by_ktru", fake_search)
    _session, origin, rows, total = open_lk_session_for_ezt("Auth=1", probe_ktru="x")
    assert origin == GOST_ORIGIN
    assert calls == [GOST_ORIGIN]
    assert rows == []
    assert total == 0


def test_request_json_connection_reset_is_network(monkeypatch):
    from rts_pp_application_api import GOST_ORIGIN, _request_json

    monkeypatch.setattr("rts_pp_application_api.WAF_RETRY_ATTEMPTS", 1)
    session = MagicMock()
    session.request.side_effect = requests.exceptions.ConnectionError(
        ("Connection aborted.", ConnectionResetError(104, "Connection reset by peer"))
    )
    with pytest.raises(RtsPpApplicationApiError, match="Сеть РТС") as exc:
        _request_json(session, "POST", GOST_ORIGIN + "/supplier/lk/api/auction/search")
    assert exc.value.reason == "network"


def test_supplier_session_puts_cookie_in_jar_not_header():
    from rts_pp_application_api import GOST_ORIGIN, supplier_session

    session = supplier_session(".Auth.Sso=abc; .Auth.Rts.P=def", origin=GOST_ORIGIN)
    assert "Cookie" not in session.headers
    assert session.headers["Origin"] == GOST_ORIGIN
    names = {c.name for c in session.cookies}
    assert ".Auth.Sso" in names
    assert ".Auth.Rts.P" in names

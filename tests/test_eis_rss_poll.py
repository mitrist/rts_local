from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from eis_analytics_db import etp_name_from_rss_url
from eis_rss_poll import parse_rss_items, rss_requests_verify, rss_url_for_ktru, rss_url_with_cache_buster


def test_rss_requests_verify_env(monkeypatch):
    monkeypatch.delenv("EIS_RSS_VERIFY_SSL", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    assert rss_requests_verify() is True
    monkeypatch.setenv("EIS_RSS_VERIFY_SSL", "0")
    assert rss_requests_verify() is False
    monkeypatch.delenv("EIS_RSS_VERIFY_SSL", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    assert rss_requests_verify() is False
    monkeypatch.setenv("EIS_RSS_VERIFY_SSL", "1")
    assert rss_requests_verify() is True


def test_parse_rss_plan_sample():
    xml_text = Path(".cursor/plans/rss.xml").read_text(encoding="utf-8")

    items = parse_rss_items(xml_text)

    assert len(items) == 10
    first = items[0]
    assert first.reg_number == "0330300204926000024"
    assert first.pub_date_utc == "2026-05-19T12:44:52Z"
    assert first.stage_text == "Определение поставщика завершено"
    assert first.customer_name.startswith("МУНИЦИПАЛЬНОЕ БЮДЖЕТНОЕ УЧРЕЖДЕНИЕ")
    assert first.price_text == "30000.00"
    assert first.ikz == "263350610007635060100100090110000244"


def test_rss_url_with_cache_buster_preserves_filters_and_replaces_old_stamp():
    url = (
        "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html?"
        "morphology=on&gws=Выберите+тип+закупки&_rts_ts=old"
    )

    out = rss_url_with_cache_buster(url, ts=12345)
    qs = parse_qs(urlsplit(out).query)

    assert qs["morphology"] == ["on"]
    assert qs["gws"] == ["Выберите тип закупки"]
    assert qs["_rts_ts"] == ["12345"]


def test_rss_url_for_ktru_adds_classifier_and_page_size():
    url = (
        "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html?"
        "morphology=on&placingWayList=EZT20%2CEZTP20&recordsPerPage=_10&searchString=old"
    )
    out = rss_url_for_ktru(url, "31.01.11.150-00000021")
    qs = parse_qs(urlsplit(out).query)
    assert qs["kos"] == ["on"]
    assert qs["ktruIdsCodes"] == ["31.01.11.150-00000021"]
    assert qs["recordsPerPage"] == ["_50"]
    assert qs["placingWayList"] == ["EZT20,EZTP20"]
    assert "searchString" not in qs


def test_etp_name_from_rss_url_maps_rts_tender():
    url = (
        "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html?"
        "etp_7360768=on&etp=7360768&placingWayList=EZT20"
    )
    assert etp_name_from_rss_url(url) == "РТС-тендер"
    assert etp_name_from_rss_url("https://zakupki.gov.ru/epz/order/extendedsearch/rss.html") == ""

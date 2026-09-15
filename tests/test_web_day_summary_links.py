"""Deep links «Итоги дня» → модалка сравнения."""

from __future__ import annotations

from datetime import date, datetime, timezone

from web_ui.day_summary_links import (
    build_day_summary_compare_url,
    build_offer_matching_compare_url,
    build_pp_monitor_sample_url,
    summary_date_from_row,
)


def test_build_offer_matching_compare_url(monkeypatch):
    monkeypatch.setenv("RTS_WEB_PUBLIC_BASE_URL", "http://161.104.18.194")
    url = build_offer_matching_compare_url(
        purchase_number="PN-1",
        line_key="PN-1|08.12.12.140-00000005|1",
    )
    assert url.startswith("http://161.104.18.194/offer-matching?")
    assert "purchase=PN-1" in url
    assert "open_compare=1" in url
    assert "line_key=PN-1%7C08.12.12.140-00000005%7C1" in url
    assert "date=" not in url


def test_web_public_base_url_falls_back_to_rts_public_base_url(monkeypatch):
    monkeypatch.delenv("RTS_WEB_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("RTS_PUBLIC_BASE_URL", "http://161.104.18.194")
    url = build_offer_matching_compare_url(purchase_number="PN-1", line_key="lk")
    assert url.startswith("http://161.104.18.194/offer-matching?")


def test_build_day_summary_compare_url(monkeypatch):
    monkeypatch.setenv("RTS_WEB_PUBLIC_BASE_URL", "https://tender.nacpro-web-service.ru")
    url = build_day_summary_compare_url(
        purchase_number="PN-1",
        line_key="PN-1|08.12.12.140-00000005|1",
        summary_date=date(2026, 5, 21),
    )
    assert url.startswith("https://tender.nacpro-web-service.ru/offer-matching?")
    assert "purchase=PN-1" in url
    assert "open_compare=1" in url
    assert "line_key=PN-1%7C08.12.12.140-00000005%7C1" in url


def test_build_pp_monitor_sample_url(monkeypatch):
    monkeypatch.setenv("RTS_WEB_PUBLIC_BASE_URL", "https://rts.raytec-log.pro")
    url = build_pp_monitor_sample_url("RyvkyKuzMWAq")
    assert url == "https://rts.raytec-log.pro/pp-monitor-samples/RyvkyKuzMWAq"


def test_summary_date_from_row_pub_date_utc():
    row = {"pub_date_utc": "2026-05-21T10:00:00Z"}
    assert summary_date_from_row(row) == date(2026, 5, 21)


def test_summary_date_from_row_fetched_fallback():
    row = {"fetched_at_utc": datetime(2026, 5, 22, 1, 30, tzinfo=timezone.utc).isoformat()}
    assert summary_date_from_row(row) == date(2026, 5, 22)

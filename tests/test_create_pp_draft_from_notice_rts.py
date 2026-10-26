"""create_pp_draft_from_notice via RTS creation-data / temporary/create."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from eis_analytics_db import analytics_db_path, get_rts_pp_draft, record_notice_html_page, record_rss_poll_run
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from rts_ktru_cache import save_cached_template
from rts_pp_draft_service import create_pp_draft_from_notice, pp_participate_eligibility
from rts_preoffers import load_preoffers, upsert_preoffer

KTRU = "22.22.10.000-00000005"


def _seed(db: Path, meta: Path, preoffers: Path) -> tuple[str, str]:
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "o1",
            "title": "Мешок",
            "sku": "SKU",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [],
        },
    )
    cache = meta / "ktru_rts_cache"
    save_cached_template(cache, KTRU, {"code": KTRU, "name": "Мешок", "version": 6, "characteristics": []})
    pn = "0369200012126000008"
    line = KtruPurchaseLine(
        line_key=f"{pn}|{KTRU}|line1",
        purchase_number=pn,
        object_sid="",
        object_external_sid="",
        position_index=0,
        ktru_code=KTRU,
        position_display_name="Мешок",
        line_sum_display="2000,00",
        characteristics=[
            CustomerCharacteristic(
                match_key="name:x",
                characteristic_kind="manual",
                characteristic_code=None,
                display_label="x",
                value_display="y",
            )
        ],
    )
    record_rss_poll_run(
        db,
        [{"reg_number": pn, "link": f"https://example.test/{pn}", "pub_date_utc": "2026-09-17T10:00:00Z"}],
        query_label="t",
        rss_url="https://example.test/rss",
        started_at_utc=datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
        finished_at_utc=datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
        ok=True,
        errors=[],
    )
    record_notice_html_page(
        db,
        purchase_number=pn,
        source_url="https://example.test/card",
        html_text="<html/>",
        lines=[line],
        fetched_at_utc=datetime(2026, 9, 17, 10, 1, tzinfo=timezone.utc),
        ok=True,
        http_status=200,
    )
    return pn, line.line_key


def test_create_pp_draft_from_notice_temporary_path(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RTS_PP_PARTICIPATE_REQUIRE_PREOFFER_CATALOG", "1")
    monkeypatch.setenv("RTS_PARTICIPATION_PRICE_ORD_ENABLED", "0")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    cookie = meta / "cookies" / "IPSokolova.txt"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    pn, lk = _seed(db, meta, preoffers)

    creation = {
        "notificationNumber": pn,
        "ktruCode": KTRU,
        "ktruName": "Мешок",
        "ktruVersion": 6,
        "okeiCode": "736",
        "okeiName": "РУЛ",
        "minQuantity": "1",
        "maxQuantity": "10",
        "unitPrice": "200,40",
        "maxDeliveryDays": 14,
        "deliveryPlaceName": "Челябинск",
        "deliveryPlaceOktmoCode": "75701000001",
        "ktruCharacteristics": [],
    }

    with patch("rts_pp_draft_service.validate_cookie_session"), patch(
        "rts_pp_draft_service.load_offers_cookie", return_value="cookie"
    ), patch(
        "rts_pp_draft_service.resolve_offer_creation_data",
        return_value=(creation, f"l{pn}-1"),
    ) as resolve_mock, patch(
        "rts_pp_draft_service.fetch_national_mode_info_types", return_value={}
    ), patch(
        "rts_pp_draft_service.create_temporary_offer", return_value=562287
    ) as create_mock, patch(
        "rts_pp_draft_service.draft_search_by_id", return_value=None
    ):
        result = create_pp_draft_from_notice(
            analytics_db=db,
            history_db=tmp_path / "h.sqlite3",
            meta_dir=meta,
            preoffers_path=preoffers,
            cookie_path=cookie,
            purchase_number=pn,
            line_key=lk,
            rts_profile="IPSokolova",
            created_by="test",
        )

    assert result.ok
    assert result.rts_draft_id == 562287
    assert "starNotificationNumber=l0369200012126000008-1" in result.edit_url
    assert result.fill_report.get("source") == "rts_creation_data"
    assert result.fill_report.get("country_code") == "643"
    assert result.fill_report.get("html_present") is True
    resolve_mock.assert_called_once()
    create_mock.assert_called_once()
    body = create_mock.call_args[0][1]
    assert body["ktru"]["code"] == KTRU
    assert body["ktruCharacteristics"] == []
    assert body["countriesOfOrigin"][0]["code"] == "643"
    assert body["nationalModeInfoGroups"]
    assert body["nationalModeInfoGroups"][0]["nationalModeInfos"][0]["registryNumberNotProvided"] is True
    row = get_rts_pp_draft(db, pn, lk, rts_profile="IPSokolova")
    assert row is not None
    assert int(row["rts_draft_id"]) == 562287


def test_create_pp_draft_from_notice_uses_country_csv(tmp_path: Path, monkeypatch):
    from ktru_country_origin import CountryOriginRow, save_country_origin_rows

    monkeypatch.setenv("RTS_PP_PARTICIPATE_REQUIRE_PREOFFER_CATALOG", "1")
    monkeypatch.setenv("RTS_PARTICIPATION_PRICE_ORD_ENABLED", "0")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    cookie = meta / "cookies" / "IPSokolova.txt"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    pn, lk = _seed(db, meta, preoffers)

    csv_path = tmp_path / "country_origin.csv"
    save_country_origin_rows(
        csv_path,
        [
            CountryOriginRow(
                row_id="r1",
                ktru_code=KTRU,
                title="Мешок",
                country_name="Китайская Народная Республика",
                enabled=True,
            )
        ],
    )

    creation = {
        "notificationNumber": pn,
        "ktruCode": KTRU,
        "ktruName": "Мешок",
        "ktruVersion": 6,
        "okeiCode": "736",
        "okeiName": "РУЛ",
        "ktruCharacteristics": [],
    }

    with patch(
        "rts_pp_draft_service.default_country_origin_path", return_value=csv_path
    ), patch("rts_pp_draft_service.validate_cookie_session"), patch(
        "rts_pp_draft_service.load_offers_cookie", return_value="cookie"
    ), patch(
        "rts_pp_draft_service.resolve_offer_creation_data",
        return_value=(creation, f"l{pn}-1"),
    ), patch(
        "rts_pp_draft_service.fetch_national_mode_info_types", return_value={}
    ), patch(
        "rts_pp_draft_service.create_temporary_offer", return_value=1001
    ) as create_mock, patch(
        "rts_pp_draft_service.draft_search_by_id", return_value=None
    ):
        result = create_pp_draft_from_notice(
            analytics_db=db,
            history_db=tmp_path / "h.sqlite3",
            meta_dir=meta,
            preoffers_path=preoffers,
            cookie_path=cookie,
            purchase_number=pn,
            line_key=lk,
            rts_profile="IPSokolova",
        )

    assert result.ok
    assert result.fill_report.get("country_code") == "156"
    body = create_mock.call_args[0][1]
    assert body["countriesOfOrigin"] == [
        {"code": "156", "name": "Китайская Народная Республика"}
    ]
    assert body["nationalModeInfoGroups"] == []


def test_create_pp_draft_from_notice_star_mismatch(tmp_path: Path, monkeypatch):
    from rts_pp_api import RtsPpApiError

    monkeypatch.setenv("RTS_PP_PARTICIPATE_REQUIRE_PREOFFER_CATALOG", "1")
    monkeypatch.setenv("RTS_PARTICIPATION_PRICE_ORD_ENABLED", "0")
    db = analytics_db_path(tmp_path / "data")
    meta = tmp_path / "meta"
    meta.mkdir()
    preoffers = tmp_path / "preoffers.json"
    cookie = meta / "cookies" / "IPSokolova.txt"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    pn, lk = _seed(db, meta, preoffers)

    with patch("rts_pp_draft_service.validate_cookie_session"), patch(
        "rts_pp_draft_service.load_offers_cookie", return_value="cookie"
    ), patch(
        "rts_pp_draft_service.resolve_offer_creation_data",
        side_effect=RtsPpApiError("КТРУ не совпадает", reason="star_mismatch"),
    ):
        result = create_pp_draft_from_notice(
            analytics_db=db,
            history_db=tmp_path / "h.sqlite3",
            meta_dir=meta,
            preoffers_path=preoffers,
            cookie_path=cookie,
            purchase_number=pn,
            line_key=lk,
            rts_profile="IPSokolova",
        )

    assert result.ok is False
    assert result.reason == "star_mismatch"


def test_notice_eligibility_does_not_require_pp_template(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RTS_PP_PARTICIPATE_REQUIRE_PREOFFER_CATALOG", "1")
    preoffers = tmp_path / "preoffers.json"
    upsert_preoffer(
        preoffers,
        {
            "offer_id": "o1",
            "title": "Мешок",
            "sku": "SKU",
            "ktru_code": KTRU,
            "rts_url": "",
            "enabled": True,
            "characteristics": [],
        },
    )
    offers = load_preoffers(preoffers)
    cache = tmp_path / "empty_cache"
    cache.mkdir()

    blocked = pp_participate_eligibility(KTRU, preoffers=offers, ktru_cache_dir=cache)
    assert blocked["can_participate"] is False
    assert "draft_ktru_dict" in blocked["participate_reason"]

    allowed = pp_participate_eligibility(
        KTRU,
        preoffers=offers,
        ktru_cache_dir=cache,
        require_pp_template=False,
    )
    assert allowed["can_participate"] is True
    assert allowed["in_pp_template"] is False

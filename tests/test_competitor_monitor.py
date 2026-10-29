from __future__ import annotations

from datetime import datetime, timezone

from competitor_monitor import (
    build_ktru_view,
    run_competitor_poll,
    snapshot_rows_from_items,
    variant_identity,
    variant_key_text,
)
from competitor_monitor_settings import (
    add_ktru_code,
    advance_next_run,
    compute_next_run,
    load_settings,
    remove_ktru_code,
    save_schedule,
    settings_path,
    update_job,
)
from eis_analytics_db import (
    count_competitor_variant_points,
    load_competitor_ktru,
    replace_competitor_ktru_snapshot,
)
from pp_core_catalog import demand_key, find_selection_pairs, locked_code_set
from rts_customer_pp_api import (
    OWN_PARTICIPANT_INN,
    RegistryDeliveryPlace,
    RegistrySearchItem,
    RtsCustomerPpApiError,
    parse_search_item,
)
from rts_pp_monitor_settings import parse_ktru_codes

CODE = "31.01.12.122-00000004"
OTHER = "31.01.11.150-00000003"
TEMPLATE = {
    "code": CODE,
    "characteristics": [
        {
            "code": "type-code",
            "name": "Тип",
            "isActual": True,
            "characteristicValues": [
                {"id": 1, "valueCode": "two-seat", "qualityDescription": "Двухместный"},
            ],
        }
    ],
}
CATALOG = {"03000000": "Краснодарский край", "45000000": "Москва"}


def _chars() -> list[dict]:
    return [
        {
            "code": "type-code",
            "name": "Тип",
            "characteristicValues": [{"qualityDescription": "Двухместный", "valueCode": "two-seat"}],
        }
    ]


def _item(oid: str, inn: str, prices: list[float]) -> RegistrySearchItem:
    codes = ["03000000", "45000000"]
    names = ["Краснодарский край", "Москва"]
    places = [
        RegistryDeliveryPlace(
            oktmo_code=codes[index],
            oktmo_name=names[index],
            unit_price=price,
            min_delivery_days=30 - index,
            min_quantity=5.0,
        )
        for index, price in enumerate(prices)
    ]
    return RegistrySearchItem(
        offer_price_list_item_id=oid,
        price_list_item_id=1,
        ktru_code=CODE,
        name="Стол ученический",
        participant_inn=inn,
        participant_name="ИП Соколова" if inn == OWN_PARTICIPANT_INN else "ООО Чужой",
        participant_id="9" if inn == OWN_PARTICIPANT_INN else "1",
        is_own=inn == OWN_PARTICIPANT_INN,
        link_date="2026-10-29T06:21:25",
        validity_date="2026-11-29T20:59:59",
        countries="Российская Федерация",
        characteristics=_chars(),
        places=places,
    )


def _raw_offer(inn: str) -> dict:
    return {
        "id": 401181,
        "name": "Стол ученический",
        "ktru": {"code": CODE, "name": "Стол ученический"},
        "ktruCharacteristics": _chars(),
        "deliveryPlaces": [
            {
                "oktmo": {"code": "03000000", "name": "Краснодарский край"},
                "unitPrice": "23000,0",
                "minQuantity": "5,0",
                "minDeliveryDays": 30,
            }
        ],
        "countriesOfOrigin": [{"code": "643", "name": "Российская Федерация"}],
        "offerPriceListItemProtectedId": "card-1",
        "offerPriceListItemLinkDate": "2026-10-29T06:21:25.647",
        "validityDate": "2026-11-29T20:59:59",
        "participant": {"id": 7, "name": "Поставщик", "inn": inn},
    }


def test_parse_search_item_price_participant_and_own_inn():
    foreign = parse_search_item(_raw_offer("7717691587"))
    assert foreign is not None
    assert foreign.participant_inn == "7717691587"
    assert foreign.participant_name == "Поставщик"
    assert foreign.is_own is False
    assert foreign.places[0].unit_price == 23000.0
    assert foreign.places[0].min_quantity == 5.0
    assert foreign.places[0].min_delivery_days == 30
    assert foreign.places[0].oktmo_code == "03000000"
    assert foreign.countries == "Российская Федерация"
    own = parse_search_item(_raw_offer(OWN_PARTICIPANT_INN))
    assert own is not None and own.is_own is True


def test_variant_key_matches_demand_key_and_own_price_is_excluded():
    key, label = variant_identity(_chars(), TEMPLATE)
    expected = demand_key(
        find_selection_pairs({"ktruCharacteristics": _chars()}, TEMPLATE),
        locked=locked_code_set(TEMPLATE),
    )
    assert key == variant_key_text(expected)
    assert key == "type-code=two-seat"
    assert "Тип=Двухместный" in label
    offers, regions, points = snapshot_rows_from_items(
        [_item("comp", "7717691587", [10000.0, 30000.0]), _item("own", OWN_PARTICIPANT_INN, [9000.0])],
        TEMPLATE,
        CATALOG,
    )
    comp = next(row for row in offers if row["offer_price_list_item_id"] == "comp")
    assert comp["median_price"] == 20000.0
    assert comp["is_own"] is False
    view = build_ktru_view({"state": None, "offers": offers, "regions": regions, "points": points})
    assert view["offer_count"] == 1
    assert view["supplier_count"] == 1
    assert view["own_offer_count"] == 1
    assert view["price_min"] == view["price_median"] == view["price_max"] == "20 000,00"
    krasnodar = next(row for row in view["subjects"] if row["code"] == "03000000")
    assert krasnodar["median"] == "10 000,00"
    assert krasnodar["own"] == "9 000,00"
    assert points[0]["price_median"] == 20000.0
    assert points[0]["offer_count"] == 1


def test_compute_next_run_uses_msk_weekday():
    before_hour = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
    nxt = compute_next_run({"weekday": 0, "hour_msk": 6}, now=before_hour)
    assert nxt == datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
    after_hour = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)
    later = compute_next_run({"weekday": 0, "hour_msk": 6, "interval_days": 14}, now=after_hour)
    assert later == datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)
    stepped = advance_next_run(
        {"next_run_at_utc": "2026-10-26T03:00:00Z", "interval_days": 14},
        now=datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc),
    )
    assert stepped == datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)


def _install_fetch(monkeypatch, pages: dict[int, tuple[list[RegistrySearchItem], int]]):
    monkeypatch.setattr("competitor_monitor.PAGE_DELAY_SEC", 0)
    monkeypatch.setattr("competitor_monitor.KTRU_DELAY_SEC", 0)
    monkeypatch.setattr("competitor_monitor.load_customer_cookie", lambda _meta: "cookie=1")
    monkeypatch.setattr("competitor_monitor.resolve_ktru_template", lambda *_a, **_k: TEMPLATE)
    monkeypatch.setattr("competitor_monitor.subject_catalog", lambda: CATALOG)

    def _search(_cookie, payload, timeout=60.0):
        del timeout
        skip = int(payload["skip"])
        if skip not in pages:
            raise RtsCustomerPpApiError("page failed", reason="api_error")
        return pages[skip]

    monkeypatch.setattr("competitor_monitor.search_registry_items", _search)


def test_successful_poll_replaces_rows_and_adds_variant_point(monkeypatch, tmp_path):
    db = tmp_path / "analytics.sqlite3"
    replace_competitor_ktru_snapshot(
        db,
        ktru_code=CODE,
        snapshot_at_utc="2026-10-01T00:00:00Z",
        offers=[
            {
                "offer_price_list_item_id": "old-offer",
                "name": "Старое",
                "participant_inn": "7717691587",
                "participant_name": "Чужой",
                "is_own": False,
                "variant_key": "old",
                "variant_label": "Старое",
                "median_price": 1,
            }
        ],
        regions=[],
        points=[{"variant_key": "old", "variant_label": "Старое", "offer_count": 1, "price_min": 1, "price_median": 1, "price_max": 1}],
    )
    _install_fetch(monkeypatch, {0: ([_item("new-offer", "7717691587", [15000.0])], 1)})
    result = run_competitor_poll(tmp_path, db, codes=[CODE], trigger="manual_list")
    assert result["ok"] is True
    loaded = load_competitor_ktru(db, CODE)
    ids = [row["offer_price_list_item_id"] for row in loaded["offers"]]
    assert ids == ["new-offer"]
    assert count_competitor_variant_points(db, CODE) == 2
    saved = load_settings(settings_path(tmp_path))
    assert saved["job_status"] == "ok"


def test_failed_second_page_keeps_previous_snapshot(monkeypatch, tmp_path):
    db = tmp_path / "analytics.sqlite3"
    replace_competitor_ktru_snapshot(
        db,
        ktru_code=CODE,
        snapshot_at_utc="2026-10-01T00:00:00Z",
        offers=[
            {
                "offer_price_list_item_id": "old-offer",
                "name": "Старое",
                "participant_inn": "7717691587",
                "is_own": False,
                "variant_key": "old",
                "variant_label": "Старое",
                "median_price": 1,
            }
        ],
        regions=[],
        points=[{"variant_key": "old", "variant_label": "Старое", "offer_count": 1, "price_median": 1}],
    )
    _install_fetch(
        monkeypatch,
        {0: ([_item("new-offer", "7717691587", [15000.0])], 60)},
    )
    result = run_competitor_poll(tmp_path, db, codes=[CODE], trigger="schedule")
    assert result["ok"] is False
    loaded = load_competitor_ktru(db, CODE)
    ids = [row["offer_price_list_item_id"] for row in loaded["offers"]]
    assert ids == ["old-offer"]
    assert count_competitor_variant_points(db, CODE) == 1
    assert load_settings(settings_path(tmp_path))["job_status"] == "error"


def test_remove_ktru_keeps_snapshot_and_manual_code_stays_off_schedule(monkeypatch, tmp_path):
    db = tmp_path / "analytics.sqlite3"
    path = settings_path(tmp_path)
    save_schedule(path, enabled=True, interval_days=14, weekday=0, hour_msk=6)
    for code in list(load_settings(path)["ktru_codes"]):
        remove_ktru_code(path, code)
    add_ktru_code(path, CODE)
    update_job(path, next_run_at_utc="2099-01-05T03:00:00Z")
    replace_competitor_ktru_snapshot(
        db,
        ktru_code=CODE,
        snapshot_at_utc="2026-10-01T00:00:00Z",
        offers=[{"offer_price_list_item_id": "keep", "name": "Есть", "participant_inn": "1", "is_own": False}],
        regions=[],
        points=[],
    )
    remove_ktru_code(path, CODE)
    assert CODE not in load_settings(path)["ktru_codes"]
    assert [row["offer_price_list_item_id"] for row in load_competitor_ktru(db, CODE)["offers"]] == ["keep"]

    _install_fetch(monkeypatch, {0: ([_item("outside", "7717691587", [11000.0])], 1)})
    before = load_settings(path)["next_run_at_utc"]
    result = run_competitor_poll(tmp_path, db, codes=[OTHER], trigger="manual_ktru")
    assert result["ok"] is True
    after = load_settings(path)
    assert OTHER not in after["ktru_codes"]
    assert after["next_run_at_utc"] == before
    assert [row["offer_price_list_item_id"] for row in load_competitor_ktru(db, OTHER)["offers"]] == ["outside"]
    parse_ktru_codes([CODE])

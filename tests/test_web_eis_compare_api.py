"""HTTP API сравнения характеристик ЕИС / поставщика."""

from __future__ import annotations

from fastapi.testclient import TestClient

from web_ui import main as web_main


def _minimal_snap() -> dict:
    return {
        "customer_characteristics": [
            {
                "line_key": "lk-one",
                "purchase_number": "0373200008325000008",
                "ktru_code": "32.97.11.160-00000001",
                "position_display_name": "Товар тестовый полное имя",
                "price_per_unit_display": "123,45 RUB",
                "notice_total_display": "99 999 ₽",
                "customer_organization_display": "ЗАО «КлиентДемо»\nИНН 7830001234\nАдрес поставки: Санкт-Петербург",
                "characteristics": [
                    {
                        "match_key": "m1",
                        "display_label": "Масса",
                        "value_display": "1 кг",
                    }
                ],
            },
            {
                "line_key": "lk-two",
                "purchase_number": "PN-DUP",
                "ktru_code": "K-DUP",
                "position_display_name": "",
                "price_per_unit_display": "",
                "notice_total_display": "",
                "characteristics": [],
            },
            {
                "line_key": "lk-three",
                "purchase_number": "PN-DUP",
                "ktru_code": "K-DUP",
                "characteristics": [{"match_key": "z", "display_label": "Z", "value_display": "2"}],
            },
        ],
        "parse_phase2_note": "Примечание фазы 2",
    }


def test_compare_by_line_key_and_modal_header(monkeypatch):
    monkeypatch.setattr(web_main, "read_last_eis_poll", lambda: _minimal_snap())
    client = TestClient(web_main.app)
    r = client.get("/api/eis-compare-rows", params={"line_key": "lk-one"})
    assert r.status_code == 200
    data = r.json()
    assert data["ktru_code"] == "32.97.11.160-00000001"
    mh = data["modal_header"]
    assert mh["position_display_name"] == "Товар тестовый полное имя"
    assert mh["price_per_unit"] == "123,45 RUB"
    assert mh["notice_total_max_price"] == "99 999 ₽"
    assert mh["customer_organization_display"] == "ЗАО «КлиентДемо»\nИНН 7830001234\nАдрес поставки: Санкт-Петербург"
    rows = data["comparison_rows"]
    assert len(rows) == 1
    assert rows[0]["label"] == "Масса"


def test_compare_by_purchase_ktru_picks_first_duplicate(monkeypatch):
    monkeypatch.setattr(web_main, "read_last_eis_poll", lambda: _minimal_snap())
    client = TestClient(web_main.app)
    r = client.get("/api/eis-compare-rows", params={"purchase": "PN-DUP", "ktru": "K-DUP"})
    assert r.status_code == 200
    data = r.json()
    assert data["line_key"] == "lk-two"
    assert data["comparison_rows"] == []


def test_compare_requires_line_or_pair(monkeypatch):
    monkeypatch.setattr(web_main, "read_last_eis_poll", lambda: _minimal_snap())
    client = TestClient(web_main.app)
    r = client.get("/api/eis-compare-rows")
    assert r.status_code == 400


def test_compare_missing_snap(monkeypatch):
    monkeypatch.setattr(web_main, "read_last_eis_poll", lambda: None)
    client = TestClient(web_main.app)
    assert client.get("/api/eis-compare-rows", params={"line_key": "x"}).status_code == 404

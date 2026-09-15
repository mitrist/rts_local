from __future__ import annotations

from rts_preoffers import (
    delete_preoffer,
    disable_preoffer,
    import_preoffers_from_ktru_csv,
    load_preoffers,
    load_preoffers_raw,
    parse_ktru_csv_rows,
    upsert_preoffer,
)


def test_load_preoffers_normalizes_match_keys(tmp_path):
    path = tmp_path / "rts_preoffers.json"
    path.write_text(
        """
        {
          "offers": [
            {
              "offer_id": "offer-1",
              "title": "Тест",
              "rts_url": "https://example.test/offer",
              "sku": "SKU-1",
              "ktru_code": "08.12.12.140-00000005",
              "enabled": true,
              "characteristics": [
                {"label": "Вид", "value": "Гранитный"},
                {"match_key": " NAME:Марка  ", "label": "Марка", "value": "М1200"}
              ]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    offers = load_preoffers(path)

    assert len(offers) == 1
    assert offers[0].characteristics[0].match_key == "name:вид"
    assert offers[0].characteristics[1].match_key == "name:марка"


def test_upsert_and_disable_preoffer_write_json(tmp_path):
    path = tmp_path / "rts_preoffers.json"

    saved = upsert_preoffer(
        path,
        {
            "title": "Щебень",
            "ktru_code": "08.12.12.140-00000005",
            "sku": "SKU-1",
            "rts_url": "https://example.test/offer",
            "enabled": True,
            "characteristics": [{"label": "Вид", "value": "Гранитный"}],
        },
    )
    upsert_preoffer(path, {**saved, "title": "Щебень обновленный"})

    raw = load_preoffers_raw(path)
    offers = load_preoffers(path)

    assert len(raw["offers"]) == 1
    assert offers[0].title == "Щебень обновленный"
    assert offers[0].characteristics[0].match_key == "name:вид"

    assert disable_preoffer(path, saved["offer_id"]) is True
    assert load_preoffers(path) == []


def test_parse_ktru_csv_rows(tmp_path):
    csv_path = tmp_path / "ktru.csv"
    csv_path.write_text(
        "КТРУ;Название предложения;SKU\n"
        "31.09.12.110-00000004;Диван;Диван\n"
        "26.20.16.120-00000104;3D-принтер FDM;3D-принтер FDM\n",
        encoding="utf-8",
    )
    rows = parse_ktru_csv_rows(csv_path)
    assert len(rows) == 2
    assert rows[0]["ktru_code"] == "31.09.12.110-00000004"
    assert rows[0]["title"] == "Диван"
    assert rows[0]["sku"] == "Диван"


def test_import_preoffers_from_ktru_csv_merges(tmp_path):
    csv_path = tmp_path / "ktru.csv"
    csv_path.write_text(
        "КТРУ;Название предложения;SKU\n"
        "31.09.12.110-00000004;Диван;Диван\n"
        "26.20.16.120-00000104;3D-принтер FDM;3D-принтер FDM\n",
        encoding="utf-8",
    )
    catalog = tmp_path / "rts_preoffers.json"
    catalog.write_text(
        '{"offers":[{"offer_id":"old","ktru_code":"08.12.12.140-00000005","title":"X","sku":"","rts_url":"","enabled":true,"characteristics":[]}]}',
        encoding="utf-8",
    )
    stats = import_preoffers_from_ktru_csv(csv_path, catalog_path=catalog)
    assert stats["added"] == 2
    offers = load_preoffers_raw(catalog)["offers"]
    assert len(offers) == 3
    divan = next(x for x in offers if x["ktru_code"] == "31.09.12.110-00000004")
    assert divan["title"] == "Диван"
    assert divan["sku"] == "Диван"
    assert divan["offer_id"] == "rts-31.09.12.110-00000004"
    assert divan["enabled"] is True


def test_delete_preoffer_removes_from_json(tmp_path):
    path = tmp_path / "rts_preoffers.json"
    saved = upsert_preoffer(
        path,
        {
            "title": "Удаляемое",
            "ktru_code": "08.12.12.140-00000005",
            "enabled": True,
            "characteristics": [{"label": "Вид", "value": "X"}],
        },
    )
    assert delete_preoffer(path, saved["offer_id"]) is True
    assert load_preoffers_raw(path)["offers"] == []
    assert delete_preoffer(path, saved["offer_id"]) is False

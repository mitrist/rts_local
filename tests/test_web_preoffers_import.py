from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from web_ui import main as web_main

FIXTURE = Path(__file__).parent / "fixtures" / "rts_registry_mini.json"


def test_preoffers_import_page(monkeypatch, tmp_path):
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: tmp_path / "rts_preoffers.json")
    client = TestClient(web_main.app)

    r = client.get("/preoffers/import")

    assert r.status_code == 200
    assert "Импорт из JSON RTS" in r.text
    assert "Предпросмотр" in r.text


def test_registry_preview_and_import_flow(monkeypatch, tmp_path):
    db = tmp_path / "rts_preoffers.json"
    monkeypatch.setattr(web_main, "_META", tmp_path)
    monkeypatch.setattr(web_main, "_rts_preoffers_path", lambda: db)
    client = TestClient(web_main.app)
    json_text = FIXTURE.read_text(encoding="utf-8")

    preview = client.post(
        "/action/preoffers-registry-preview",
        data={"registry_json_text": json_text, "only_published": "1"},
        follow_redirects=False,
    )
    assert preview.status_code == 303
    assert "/preoffers/import" in (preview.headers.get("location") or "")

    page = client.get("/preoffers/import")
    assert page.status_code == 200
    assert "31.01.11.150-00000003" in page.text
    assert "Импортировать в справочник" in page.text

    imp = client.post("/action/preoffers-registry-import", follow_redirects=False)
    assert imp.status_code == 303
    assert imp.headers["location"].endswith("/preoffers")

    catalog = client.get("/preoffers")
    assert catalog.status_code == 200
    assert "31.01.11.150-00000003" in catalog.text
    assert "offer-item/create/335155" in catalog.text

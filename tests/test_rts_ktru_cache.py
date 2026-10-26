from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from rts_ktru_cache import (
    extend_ktru_cache_validity,
    extend_all_ktru_cache_validity,
    get_ktru_template,
    import_ktru_search_response,
    import_ktru_template,
    list_ktru_cache_status,
    load_cached_template,
    save_cached_template,
    search_cached_ktru,
)
from rts_pp_api import RtsPpApiError


def test_save_and_load_cached_template(tmp_path):
    item = {"code": "32.50.13.110-00005820", "name": "Test", "version": 1, "characteristics": []}
    save_cached_template(tmp_path, item["code"], item)
    loaded = load_cached_template(tmp_path, item["code"])
    assert loaded is not None
    assert loaded["code"] == item["code"]
    assert loaded["name"] == "Test"


def test_load_cached_template_ttl_expired(tmp_path):
    item = {"code": "X-1", "name": "X", "version": 1}
    path = save_cached_template(tmp_path, "X-1", item)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["_cache_meta"]["cached_at_unix"] = int(time.time()) - 10_000
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_cached_template(tmp_path, "X-1", ttl_sec=3600) is None


def test_import_ktru_search_response_multi_page(tmp_path):
    payload = {
        "page": [
            {"code": "08.12.12.140-00000005", "name": "A", "version": 1, "characteristics": [{"code": "a"}]},
            {"code": "08.12.12.140-00000006", "name": "B", "version": 1, "characteristics": []},
        ]
    }
    items = import_ktru_search_response(tmp_path, payload)
    assert len(items) == 2
    assert load_cached_template(tmp_path, "08.12.12.140-00000005") is not None
    assert load_cached_template(tmp_path, "08.12.12.140-00000006") is not None


def test_import_ktru_template_from_page(tmp_path):
    payload = {
        "page": [
            {"code": "08.12.12.140-00000005", "name": "Щебень", "version": 2, "characteristics": []},
        ]
    }
    item = import_ktru_template(tmp_path, "08.12.12.140-00000005", payload)
    assert item["name"] == "Щебень"
    assert load_cached_template(tmp_path, "08.12.12.140-00000005") is not None


def test_get_ktru_template_cache_hit(tmp_path, monkeypatch):
    item = {"code": "K-1", "name": "Cached", "version": 1, "characteristics": [{"code": "a"}]}
    save_cached_template(tmp_path, "K-1", item)

    def fail_search(*args, **kwargs):
        raise AssertionError("search_ktru must not be called on cache hit")

    monkeypatch.setattr("rts_ktru_cache.search_ktru", fail_search)
    got = get_ktru_template("cookie=x", "K-1", tmp_path)
    assert got["name"] == "Cached"


def test_get_ktru_template_uses_stale_cache_when_fresh_ttl_expired(tmp_path, monkeypatch):
    item = {"code": "K-1", "name": "Stale ok", "version": 1, "characteristics": [{"code": "a"}]}
    path = save_cached_template(tmp_path, "K-1", item)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["_cache_meta"]["cached_at_unix"] = int(time.time()) - 10 * 86400
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_cached_template(tmp_path, "K-1", ttl_sec=3600) is None

    def fail_search(*args, **kwargs):
        raise AssertionError("search_ktru must not be called when stale cache exists")

    monkeypatch.setattr("rts_ktru_cache.search_ktru", fail_search)
    got = get_ktru_template("cookie=x", "K-1", tmp_path, ttl_sec=3600)
    assert got["name"] == "Stale ok"


def test_get_ktru_template_unavailable_without_cache(tmp_path, monkeypatch):
    def fake_search(cookie, code, timeout=60.0):
        raise RtsPpApiError("not found", reason="ktru_not_found")

    monkeypatch.setattr("rts_ktru_cache.search_ktru", fake_search)
    with pytest.raises(RtsPpApiError) as exc:
        get_ktru_template("cookie=x", "MISSING", tmp_path, allow_stub=False)
    assert exc.value.reason == "ktru_catalog_unavailable"


def test_get_ktru_template_allow_stub(tmp_path, monkeypatch):
    def fake_search(cookie, code, timeout=60.0):
        raise RtsPpApiError("not found", reason="ktru_not_found")

    monkeypatch.setattr("rts_ktru_cache.search_ktru", fake_search)
    item = get_ktru_template("cookie=x", "STUB-1", tmp_path, allow_stub=True, ktru_name="Stub name")
    assert item["code"] == "STUB-1"
    assert item["characteristics"] == []


def test_list_ktru_cache_status_and_extend(tmp_path):
    item = {"code": "K-1", "name": "Printer", "version": 3, "characteristics": []}
    save_cached_template(tmp_path, "K-1", item)
    path = tmp_path / "K-1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["_cache_meta"]["cached_at_unix"] = int(time.time()) - 8 * 86400
    path.write_text(json.dumps(raw), encoding="utf-8")

    rows = list_ktru_cache_status(tmp_path)
    assert len(rows) == 1
    assert rows[0]["ktru_code"] == "K-1"
    assert rows[0]["expired"] is True
    assert rows[0]["days_remaining"] < 0

    updated = extend_ktru_cache_validity(tmp_path, "K-1")
    assert updated["expired"] is False
    assert updated["days_remaining"] >= 6.9
    assert load_cached_template(tmp_path, "K-1") is not None


def test_extend_all_ktru_cache_validity(tmp_path):
    for code in ("K-1", "K-2"):
        item = {"code": code, "name": code, "version": 1, "characteristics": []}
        save_cached_template(tmp_path, code, item)
        path = tmp_path / f"{code}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["_cache_meta"]["cached_at_unix"] = int(time.time()) - 10 * 86400
        path.write_text(json.dumps(raw), encoding="utf-8")

    updated = extend_all_ktru_cache_validity(tmp_path)
    assert len(updated) == 2
    for code in ("K-1", "K-2"):
        assert load_cached_template(tmp_path, code) is not None


def test_search_cached_ktru_by_name_and_code(tmp_path):
    save_cached_template(
        tmp_path,
        "31.01.11.150-00000003",
        {"code": "31.01.11.150-00000003", "name": "Стул на металлическом каркасе", "version": 5},
    )
    save_cached_template(
        tmp_path,
        "22.22.10.000-00000005",
        {"code": "22.22.10.000-00000005", "name": "Мешок", "version": 6},
    )
    hits = search_cached_ktru(tmp_path, "стул")
    assert len(hits) == 1
    assert hits[0]["code"] == "31.01.11.150-00000003"
    exact = search_cached_ktru(tmp_path, "22.22.10.000-00000005")
    assert exact[0]["name"] == "Мешок"

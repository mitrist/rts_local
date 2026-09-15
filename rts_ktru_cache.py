"""File cache for RTS Ktru/search templates (dictionary API)."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rts_pp_api import RtsPpApiError, search_ktru

DEFAULT_KTRU_CACHE_TTL_SEC = 7 * 24 * 3600
_DEFAULT_TTL_SEC = DEFAULT_KTRU_CACHE_TTL_SEC
_SAFE_CODE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def default_ktru_cache_ttl_sec() -> int:
    raw = os.environ.get("RTS_KTRU_CACHE_TTL_DAYS", "").strip()
    if raw:
        try:
            days = float(raw.replace(",", "."))
            if days > 0:
                return int(days * 86400)
        except ValueError:
            pass
    return DEFAULT_KTRU_CACHE_TTL_SEC


def cache_dir_default(meta_root: Path) -> Path:
    return meta_root / "ktru_rts_cache"


def _cache_path(cache_dir: Path, ktru_code: str) -> Path:
    safe = _SAFE_CODE_RE.sub("_", str(ktru_code or "").strip()) or "unknown"
    return cache_dir / f"{safe}.json"


def _cache_meta(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "cached_at_unix": int(time.time()),
        "ktru_code": item.get("code"),
        "version": item.get("version"),
    }


def is_ktru_template_cached(cache_dir: Path, ktru_code: str) -> bool:
    """True if ktru_rts_cache has a template for the code (imported from draft_ktru_dict)."""
    return load_cached_template(cache_dir, ktru_code, ttl_sec=0) is not None


def load_cached_template(cache_dir: Path, ktru_code: str, *, ttl_sec: int = _DEFAULT_TTL_SEC) -> dict[str, Any] | None:
    path = _cache_path(cache_dir, ktru_code)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    meta = raw.get("_cache_meta") if isinstance(raw.get("_cache_meta"), dict) else {}
    cached_at = int(meta.get("cached_at_unix") or 0)
    if cached_at and ttl_sec > 0 and (time.time() - cached_at) > ttl_sec:
        return None
    item = raw.get("item")
    if isinstance(item, dict) and str(item.get("code") or "").strip() == str(ktru_code).strip():
        return item
    return None


def _read_cache_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def ktru_cache_status_entry(
    path: Path,
    *,
    ttl_sec: int | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    raw = _read_cache_file(path)
    if raw is None:
        return None
    ttl = ttl_sec if ttl_sec is not None else default_ktru_cache_ttl_sec()
    ts = now if now is not None else time.time()
    meta = raw.get("_cache_meta") if isinstance(raw.get("_cache_meta"), dict) else {}
    item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    cached_at = int(meta.get("cached_at_unix") or 0)
    ktru_code = str(item.get("code") or meta.get("ktru_code") or path.stem).strip()
    expires_at = (cached_at + ttl) if cached_at else 0
    seconds_left = (expires_at - ts) if expires_at else 0
    days_remaining = seconds_left / 86400 if expires_at else None
    valid_item = (
        isinstance(item, dict)
        and ktru_code
        and str(item.get("code") or "").strip() == ktru_code
    )
    return {
        "ktru_code": ktru_code,
        "name": str(item.get("name") or "").strip(),
        "version": meta.get("version") if meta.get("version") is not None else item.get("version"),
        "cached_at_unix": cached_at,
        "cached_at_utc": (
            datetime.fromtimestamp(cached_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            if cached_at
            else ""
        ),
        "expires_at_unix": expires_at,
        "expires_at_utc": (
            datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            if expires_at
            else ""
        ),
        "days_remaining": round(days_remaining, 1) if days_remaining is not None else None,
        "expired": bool(expires_at and seconds_left <= 0),
        "valid_item": valid_item,
        "file_name": path.name,
        "file_size": path.stat().st_size if path.is_file() else 0,
    }


def list_ktru_cache_status(cache_dir: Path, *, ttl_sec: int | None = None) -> list[dict[str, Any]]:
    if not cache_dir.is_dir():
        return []
    ttl = ttl_sec if ttl_sec is not None else default_ktru_cache_ttl_sec()
    now = time.time()
    rows: list[dict[str, Any]] = []
    for path in sorted(cache_dir.glob("*.json")):
        entry = ktru_cache_status_entry(path, ttl_sec=ttl, now=now)
        if entry and entry.get("ktru_code"):
            rows.append(entry)
    rows.sort(key=lambda row: (row.get("days_remaining") is None, row.get("days_remaining") or 0.0))
    return rows


def extend_ktru_cache_validity(
    cache_dir: Path,
    ktru_code: str,
    *,
    extend_sec: int | None = None,
    ttl_sec: int | None = None,
) -> dict[str, Any]:
    """Extend cache expiry by *extend_sec* from max(current expiry, now)."""
    code = str(ktru_code or "").strip()
    if not code:
        raise ValueError("Код КТРУ не задан.")
    path = _cache_path(cache_dir, code)
    raw = _read_cache_file(path)
    if raw is None:
        raise ValueError(f"Кэш КТРУ {code} не найден.")
    ttl = ttl_sec if ttl_sec is not None else default_ktru_cache_ttl_sec()
    extend = extend_sec if extend_sec is not None else DEFAULT_KTRU_CACHE_TTL_SEC
    now = time.time()
    meta = raw.get("_cache_meta") if isinstance(raw.get("_cache_meta"), dict) else {}
    cached_at = int(meta.get("cached_at_unix") or 0)
    if not cached_at:
        cached_at = int(now)
    expires_at = cached_at + ttl
    new_expires_at = max(expires_at, now) + extend
    meta = dict(meta)
    meta["cached_at_unix"] = int(new_expires_at - ttl)
    if not meta.get("ktru_code"):
        item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
        meta["ktru_code"] = str(item.get("code") or code).strip()
    raw["_cache_meta"] = meta
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    entry = ktru_cache_status_entry(path, ttl_sec=ttl, now=now)
    if entry is None:
        raise ValueError(f"Не удалось обновить кэш КТРУ {code}.")
    return entry


def extend_all_ktru_cache_validity(
    cache_dir: Path,
    *,
    extend_sec: int | None = None,
    ttl_sec: int | None = None,
) -> list[dict[str, Any]]:
    """Extend every KTRU cache file in *cache_dir*."""
    codes = [str(row.get("ktru_code") or "").strip() for row in list_ktru_cache_status(cache_dir, ttl_sec=ttl_sec)]
    codes = [code for code in codes if code]
    updated: list[dict[str, Any]] = []
    for code in codes:
        updated.append(
            extend_ktru_cache_validity(cache_dir, code, extend_sec=extend_sec, ttl_sec=ttl_sec)
        )
    return updated


def save_cached_template(cache_dir: Path, ktru_code: str, item: dict[str, Any]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, ktru_code)
    payload = {"_cache_meta": _cache_meta(item), "item": item}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _items_from_ktru_search(data: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(data.get("code"), str) and str(data["code"]).strip():
        return [data]
    page = data.get("page")
    if not isinstance(page, list):
        return []
    return [
        row
        for row in page
        if isinstance(row, dict) and str(row.get("code") or "").strip()
    ]


def import_ktru_search_response(cache_dir: Path, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Import every KTRU item from a Ktru/search response (page[] or single item)."""
    items = _items_from_ktru_search(data)
    if not items:
        raise ValueError("В JSON нет шаблонов КТРУ")
    imported: list[dict[str, Any]] = []
    for item in items:
        code = str(item["code"]).strip()
        save_cached_template(cache_dir, code, item)
        imported.append(item)
    return imported


def import_ktru_template(cache_dir: Path, ktru_code: str, data: dict[str, Any] | list[Any]) -> dict[str, Any]:
    """Import from DevTools Ktru/search response or a single template item."""
    if not isinstance(data, dict):
        raise ValueError(f"В JSON нет шаблона КТРУ {ktru_code}")
    want = str(ktru_code).strip()
    for item in _items_from_ktru_search(data):
        if str(item.get("code") or "").strip() == want:
            save_cached_template(cache_dir, want, item)
            return item
    raise ValueError(f"В JSON нет шаблона КТРУ {ktru_code}")


def get_ktru_template(
    cookie: str,
    ktru_code: str,
    cache_dir: Path,
    *,
    timeout: float = 60.0,
    ttl_sec: int = _DEFAULT_TTL_SEC,
    allow_stub: bool = False,
    ktru_name: str | None = None,
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    if not code:
        raise RtsPpApiError("Код КТРУ не задан.", reason="ktru_not_found")

    cached = load_cached_template(cache_dir, code, ttl_sec=ttl_sec)
    if cached:
        return cached

    stale = load_cached_template(cache_dir, code, ttl_sec=0)
    if stale:
        return stale

    try:
        item = search_ktru(cookie, code, timeout=timeout)
    except RtsPpApiError as exc:
        if exc.reason != "ktru_not_found":
            raise
        if allow_stub:
            from rts_pp_api import stub_ktru_item

            return stub_ktru_item(code, ktru_name=ktru_name)
        raise RtsPpApiError(
            f"КТРУ {code} недоступен в справочнике РТС (поиск и кэш пусты). "
            "Импортируйте JSON Ktru/search в кэш или обновите cookie.",
            reason="ktru_catalog_unavailable",
            detail={"ktru_code": code},
        ) from exc

    save_cached_template(cache_dir, code, item)
    return item

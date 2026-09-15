"""Settings for RTS competitor preliminary offer monitor."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SETTINGS_VERSION = 2
MIN_POLL_INTERVAL_MIN = 5
MAX_POLL_INTERVAL_MIN = 120
DEFAULT_POLL_INTERVAL_MIN = 20
DEFAULT_SEARCH_TAKE = 10
_KTRU_CODE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{8,11}$")
_POLL_META_KEYS = (
    "last_new_poll_started_at_utc",
    "last_new_poll_finished_at_utc",
    "last_new_poll_items_new",
)


def settings_path(meta_dir: Path) -> Path:
    return meta_dir / "rts_pp_monitor.json"


def default_filters() -> dict[str, Any]:
    return {
        "price_min": None,
        "price_max": None,
        "region_ids": [],
        "skip_expired": True,
    }


def default_settings() -> dict[str, Any]:
    return {
        "version": SETTINGS_VERSION,
        "enabled": False,
        "poll_interval_min": DEFAULT_POLL_INTERVAL_MIN,
        "ktru_codes": [],
        "filters": default_filters(),
        "notify_telegram": True,
        "notify_max": True,
        "telegram_chat_id": "",
        "max_chat_id": "",
        "initial_seed_done": False,
        "registry_backfill_done": False,
        "registry_backfill": {},
        "link_date_watermarks": {},
        "search_take": DEFAULT_SEARCH_TAKE,
    }


def search_take_from_env() -> int:
    try:
        return max(1, min(int(os.environ.get("RTS_PP_MONITOR_SEARCH_TAKE", str(DEFAULT_SEARCH_TAKE))), 100))
    except ValueError:
        return DEFAULT_SEARCH_TAKE


def backfill_page_delay_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("RTS_PP_MONITOR_BACKFILL_PAGE_DELAY_SEC", "3")))
    except ValueError:
        return 3.0


def backfill_ktru_delay_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("RTS_PP_MONITOR_BACKFILL_KTRU_DELAY_SEC", "30")))
    except ValueError:
        return 30.0


def backfill_pages_per_run() -> int:
    try:
        return max(0, int(os.environ.get("RTS_PP_MONITOR_BACKFILL_PAGES_PER_RUN", "0")))
    except ValueError:
        return 0


def registry_highlight_since(settings: dict[str, Any]) -> str | None:
    """UTC timestamp: highlight registry rows last seen at/after last poll with new items."""
    try:
        if int(settings.get("last_new_poll_items_new") or 0) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    since = str(settings.get("last_new_poll_started_at_utc") or "").strip()
    return since or None


def notify_find_enabled() -> bool:
    raw = os.environ.get("RTS_PP_MONITOR_NOTIFY_FIND", "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _parse_registry_backfill(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        code = str(key or "").strip()
        if not code or not isinstance(value, dict):
            continue
        skip = 0
        total: int | None = None
        try:
            skip = max(0, int(value.get("skip") or 0))
        except (TypeError, ValueError):
            skip = 0
        if value.get("total") is not None:
            try:
                total = max(0, int(value.get("total")))
            except (TypeError, ValueError):
                total = None
        out[code] = {
            "skip": skip,
            "total": total,
            "completed": bool(value.get("completed")),
        }
    return out


def _parse_link_date_watermarks(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        code = str(key or "").strip()
        text = str(value or "").strip()
        if code and text:
            out[code] = text
    return out


def _parse_optional_float(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _parse_region_ids(raw: object) -> list[int]:
    out: list[int] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
    elif isinstance(raw, str):
        for part in raw.replace(",", "\n").splitlines():
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                continue
    return out


def parse_ktru_codes(raw: object) -> list[str]:
    codes: list[str] = []
    if isinstance(raw, list):
        source = raw
    elif isinstance(raw, str):
        source = [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]
    else:
        return []
    seen: set[str] = set()
    for item in source:
        code = str(item or "").strip()
        if not code or code in seen:
            continue
        if not _KTRU_CODE_RE.match(code):
            raise ValueError(f"Некорректный код КТРУ: {code}")
        seen.add(code)
        codes.append(code)
    return codes


def parse_filters(raw: object) -> dict[str, Any]:
    base = default_filters()
    if not isinstance(raw, dict):
        return base
    price_min = _parse_optional_float(raw.get("price_min"))
    price_max = _parse_optional_float(raw.get("price_max"))
    if price_min is not None and price_max is not None and price_min > price_max:
        price_min, price_max = price_max, price_min
    return {
        "price_min": price_min,
        "price_max": price_max,
        "region_ids": _parse_region_ids(raw.get("region_ids")),
        "skip_expired": bool(raw.get("skip_expired", base["skip_expired"])),
    }


def parse_settings(raw: dict[str, Any]) -> dict[str, Any]:
    base = default_settings()
    try:
        interval = int(raw.get("poll_interval_min", base["poll_interval_min"]))
    except (TypeError, ValueError):
        interval = base["poll_interval_min"]
    interval = max(MIN_POLL_INTERVAL_MIN, min(interval, MAX_POLL_INTERVAL_MIN))
    try:
        search_take = int(raw.get("search_take", base.get("search_take", DEFAULT_SEARCH_TAKE)))
    except (TypeError, ValueError):
        search_take = base.get("search_take", DEFAULT_SEARCH_TAKE)
    search_take = max(1, min(search_take, 100))
    return {
        "version": SETTINGS_VERSION,
        "enabled": bool(raw.get("enabled", base["enabled"])),
        "poll_interval_min": interval,
        "ktru_codes": parse_ktru_codes(raw.get("ktru_codes") or []),
        "filters": parse_filters(raw.get("filters")),
        "notify_telegram": bool(raw.get("notify_telegram", base["notify_telegram"])),
        "notify_max": bool(raw.get("notify_max", base["notify_max"])),
        "telegram_chat_id": str(raw.get("telegram_chat_id") or base["telegram_chat_id"]).strip(),
        "max_chat_id": str(raw.get("max_chat_id") or base["max_chat_id"]).strip(),
        "initial_seed_done": bool(raw.get("initial_seed_done", base["initial_seed_done"])),
        "registry_backfill_done": bool(raw.get("registry_backfill_done", base.get("registry_backfill_done", False))),
        "registry_backfill": _parse_registry_backfill(raw.get("registry_backfill", base.get("registry_backfill"))),
        "link_date_watermarks": _parse_link_date_watermarks(
            raw.get("link_date_watermarks", base.get("link_date_watermarks"))
        ),
        "search_take": search_take,
    }


def _merge_poll_meta(parsed: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(parsed)
    for key in _POLL_META_KEYS:
        if key in raw:
            out[key] = raw[key]
    return out


def load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return default_settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_settings()
    if not isinstance(data, dict):
        return default_settings()
    try:
        return _merge_poll_meta(parse_settings(data), data)
    except ValueError:
        merged = {**default_settings(), **data}
        return _merge_poll_meta(parse_settings(merged), merged)


def save_settings(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = payload if isinstance(payload, dict) else {}
    parsed = parse_settings(raw)
    out = {
        **_merge_poll_meta(parsed, raw),
        "saved_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def effective_ktru_codes(settings: dict[str, Any]) -> list[str]:
    return list(settings.get("ktru_codes") or [])


def _parse_validity_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    if len(text) >= 10:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def passes_validity_filter(summary: dict[str, Any], *, skip_expired: bool) -> bool:
    if not skip_expired:
        return True
    validity = _parse_validity_dt(str(summary.get("validity_date") or ""))
    if validity is None:
        return True
    return validity >= datetime.now(timezone.utc)


def passes_price_filter(summary: dict[str, Any], filters: dict[str, Any]) -> bool:
    price = summary.get("unit_price")
    if price is None:
        return True
    try:
        amount = float(price)
    except (TypeError, ValueError):
        return True
    price_min = filters.get("price_min")
    price_max = filters.get("price_max")
    if price_min is not None and amount < float(price_min):
        return False
    if price_max is not None and amount > float(price_max):
        return False
    return True


def passes_region_filter(_summary: dict[str, Any], filters: dict[str, Any]) -> bool:
    region_ids = filters.get("region_ids") or []
    if not region_ids:
        return True
    return True


def passes_monitor_filters(summary: dict[str, Any], settings: dict[str, Any]) -> bool:
    filters = settings.get("filters") if isinstance(settings.get("filters"), dict) else default_filters()
    if not passes_validity_filter(summary, skip_expired=bool(filters.get("skip_expired", True))):
        return False
    if not passes_price_filter(summary, filters):
        return False
    if not passes_region_filter(summary, filters):
        return False
    return True


def search_filter_overrides(settings: dict[str, Any]) -> dict[str, Any]:
    """Map monitor filters to search API price/validity fields."""
    filters = settings.get("filters") if isinstance(settings.get("filters"), dict) else default_filters()
    out: dict[str, Any] = {}
    price_min = filters.get("price_min")
    price_max = filters.get("price_max")
    if price_min is not None:
        out["price_from"] = float(price_min)
    if price_max is not None:
        out["price_to"] = float(price_max)
    if bool(filters.get("skip_expired", True)):
        out["validity_date_from"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return out


def publish_date_from_watermark(settings: dict[str, Any], ktru_code: str) -> str:
    watermarks = settings.get("link_date_watermarks")
    if not isinstance(watermarks, dict):
        return ""
    raw = str(watermarks.get(ktru_code) or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            dt = datetime.strptime(raw[:26], fmt).replace(tzinfo=timezone.utc) - timedelta(hours=2)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return ""


def registry_backfill_state(settings: dict[str, Any], ktru_code: str) -> dict[str, Any]:
    backfill = settings.get("registry_backfill")
    if not isinstance(backfill, dict):
        backfill = {}
    state = backfill.get(ktru_code)
    if not isinstance(state, dict):
        return {"skip": 0, "total": None, "completed": False}
    return {
        "skip": int(state.get("skip") or 0),
        "total": state.get("total"),
        "completed": bool(state.get("completed")),
    }


def update_link_date_watermark(settings: dict[str, Any], ktru_code: str, link_date: str) -> dict[str, Any]:
    updated = deepcopy(settings)
    watermarks = dict(updated.get("link_date_watermarks") or {})
    current = str(watermarks.get(ktru_code) or "").strip()
    new_date = str(link_date or "").strip()
    if new_date and (not current or new_date > current):
        watermarks[ktru_code] = new_date
    updated["link_date_watermarks"] = watermarks
    return updated


def apply_admin_runtime_settings(
    existing: dict[str, Any],
    *,
    enabled: bool | None = None,
    poll_interval_min: int | str | None = None,
) -> dict[str, Any]:
    """Обновляет enabled / poll_interval_min из админки, сохраняя остальные поля."""
    form = form_from_settings(existing)
    if enabled is not None:
        form["enabled"] = "1" if enabled else ""
    if poll_interval_min is not None:
        form["poll_interval_min"] = str(poll_interval_min)
    return parse_form_payload(form, existing=existing)


def poll_interval_sec_from_settings(settings: dict[str, Any]) -> int | None:
    try:
        interval_min = int(settings.get("poll_interval_min") or 0)
    except (TypeError, ValueError):
        return None
    if interval_min < MIN_POLL_INTERVAL_MIN:
        return None
    return max(MIN_POLL_INTERVAL_MIN * 60, min(interval_min * 60, MAX_POLL_INTERVAL_MIN * 60))


def parse_admin_notify_form(form: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Обновляет поля уведомлений/фильтров из админки, сохраняя enabled и интервал."""
    base = deepcopy(existing) if isinstance(existing, dict) else default_settings()
    current = form_from_settings(base)
    for key in (
        "ktru_codes_text",
        "price_min",
        "price_max",
        "skip_expired",
        "notify_telegram",
        "notify_max",
        "telegram_chat_id",
        "max_chat_id",
    ):
        if key in form:
            current[key] = form[key]
    current["enabled"] = "1" if base.get("enabled") else ""
    current["poll_interval_min"] = str(base.get("poll_interval_min") or DEFAULT_POLL_INTERVAL_MIN)
    return parse_form_payload(current, existing=base)


def form_from_settings(settings: dict[str, Any]) -> dict[str, str]:
    filters = settings.get("filters") if isinstance(settings.get("filters"), dict) else default_filters()
    region_ids = filters.get("region_ids") or []
    return {
        "enabled": "1" if settings.get("enabled") else "",
        "poll_interval_min": str(settings.get("poll_interval_min") or DEFAULT_POLL_INTERVAL_MIN),
        "ktru_codes_text": "\n".join(effective_ktru_codes(settings)),
        "price_min": "" if filters.get("price_min") is None else str(filters.get("price_min")),
        "price_max": "" if filters.get("price_max") is None else str(filters.get("price_max")),
        "region_ids_text": "\n".join(str(x) for x in region_ids),
        "skip_expired": "1" if filters.get("skip_expired", True) else "",
        "notify_telegram": "1" if settings.get("notify_telegram", True) else "",
        "notify_max": "1" if settings.get("notify_max", True) else "",
        "telegram_chat_id": str(settings.get("telegram_chat_id") or "").strip(),
        "max_chat_id": str(settings.get("max_chat_id") or "").strip(),
    }


def parse_form_payload(form: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    base = deepcopy(existing) if isinstance(existing, dict) else default_settings()
    ktru_codes = parse_ktru_codes(form.get("ktru_codes_text") or "")
    filters = {
        "price_min": _parse_optional_float(form.get("price_min")),
        "price_max": _parse_optional_float(form.get("price_max")),
        "region_ids": _parse_region_ids(form.get("region_ids_text") or ""),
        "skip_expired": str(form.get("skip_expired") or "").strip().lower() in ("1", "on", "true", "yes"),
    }
    if filters["price_min"] is not None and filters["price_max"] is not None and filters["price_min"] > filters["price_max"]:
        filters["price_min"], filters["price_max"] = filters["price_max"], filters["price_min"]
    try:
        interval = int(str(form.get("poll_interval_min") or base["poll_interval_min"]))
    except (TypeError, ValueError):
        interval = base["poll_interval_min"]
    return parse_settings(
        {
            **base,
            "enabled": str(form.get("enabled") or "").strip().lower() in ("1", "on", "true", "yes"),
            "poll_interval_min": interval,
            "ktru_codes": ktru_codes,
            "filters": filters,
            "notify_telegram": str(form.get("notify_telegram") or "").strip().lower() in ("1", "on", "true", "yes"),
            "notify_max": str(form.get("notify_max") or "").strip().lower() in ("1", "on", "true", "yes"),
            "telegram_chat_id": str(form.get("telegram_chat_id") or base.get("telegram_chat_id") or "").strip(),
            "max_chat_id": str(form.get("max_chat_id") or base.get("max_chat_id") or "").strip(),
            "initial_seed_done": bool(base.get("initial_seed_done")),
            "registry_backfill_done": bool(base.get("registry_backfill_done")),
            "registry_backfill": deepcopy(base.get("registry_backfill") or {}),
            "link_date_watermarks": deepcopy(base.get("link_date_watermarks") or {}),
            "search_take": int(base.get("search_take") or DEFAULT_SEARCH_TAKE),
        }
    )

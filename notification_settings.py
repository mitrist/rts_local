"""Persistent settings for Telegram/MAX preoffer watch notifications."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eis_analytics_db import _parse_rub_amount


def settings_path(meta_dir: Path) -> Path:
    return meta_dir / "notification_settings.json"


def load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_min_purchase_amount(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    value = _parse_rub_amount(text)
    if value is None:
        raise ValueError("Укажите неотрицательное число, например 100000 или 100000,50.")
    return value


def min_purchase_amount_from_settings(data: dict[str, Any]) -> float | None:
    return _amount_from_settings_key(data, "min_purchase_amount")


def max_purchase_amount_from_settings(data: dict[str, Any]) -> float | None:
    return _amount_from_settings_key(data, "max_purchase_amount")


def _amount_from_settings_key(data: dict[str, Any], key: str) -> float | None:
    raw = data.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return None
    return amount if amount >= 0 else None


def notify_flag_from_settings(data: dict[str, Any], key: str, *, default: bool = True) -> bool:
    if key not in data:
        return default
    return bool(data.get(key))


def chat_id_from_settings(data: dict[str, Any], key: str) -> str:
    return str(data.get(key) or "").strip()


def admin_form_from_settings(data: dict[str, Any]) -> dict[str, str]:
    return {
        "min_purchase_amount": _format_amount(min_purchase_amount_from_settings(data)),
        "max_purchase_amount": _format_amount(max_purchase_amount_from_settings(data)),
        "telegram_chat_id": chat_id_from_settings(data, "telegram_chat_id"),
        "max_chat_id": chat_id_from_settings(data, "max_chat_id"),
        "notify_telegram": "1" if notify_flag_from_settings(data, "notify_telegram") else "",
        "notify_max": "1" if notify_flag_from_settings(data, "notify_max") else "",
    }


def _format_amount(amount: float | None) -> str:
    if amount is None:
        return ""
    if amount == int(amount):
        return str(int(amount))
    return str(amount).replace(".", ",")


def parse_admin_form(form: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict(existing or {})
    if "min_purchase_amount" in form:
        base["min_purchase_amount"] = parse_min_purchase_amount(form.get("min_purchase_amount"))
    if "max_purchase_amount" in form:
        base["max_purchase_amount"] = parse_min_purchase_amount(form.get("max_purchase_amount"))
    min_amount = min_purchase_amount_from_settings(base)
    max_amount = max_purchase_amount_from_settings(base)
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        base["min_purchase_amount"], base["max_purchase_amount"] = max_amount, min_amount
    if "telegram_chat_id" in form:
        base["telegram_chat_id"] = str(form.get("telegram_chat_id") or "").strip()
    if "max_chat_id" in form:
        base["max_chat_id"] = str(form.get("max_chat_id") or "").strip()
    if "notify_telegram" in form:
        base["notify_telegram"] = str(form.get("notify_telegram") or "").strip().lower() in ("1", "on", "true", "yes")
    if "notify_max" in form:
        base["notify_max"] = str(form.get("notify_max") or "").strip().lower() in ("1", "on", "true", "yes")
    return base


def watch_row_purchase_amount(row: dict[str, Any]) -> float | None:
    for key in ("line_sum_text", "price_text"):
        value = _parse_rub_amount(str(row.get(key) or ""))
        if value is not None:
            return value
    return None


def passes_min_purchase_filter(row: dict[str, Any], min_amount: float | None) -> bool:
    return passes_purchase_amount_filter(row, min_amount, None)


def passes_purchase_amount_filter(
    row: dict[str, Any],
    min_amount: float | None,
    max_amount: float | None,
) -> bool:
    if min_amount is None and max_amount is None:
        return True
    amount = watch_row_purchase_amount(row)
    if amount is None:
        return False
    if min_amount is not None and amount < min_amount:
        return False
    if max_amount is not None and amount > max_amount:
        return False
    return True

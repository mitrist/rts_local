"""Settings for automatic PP draft creation from competitor monitor PPs."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from notification_settings import parse_min_purchase_amount
from rts_cookie_profiles import normalize_rts_profile

SETTINGS_VERSION = 1
DEFAULT_MAX_PER_HOUR = 10
DEFAULT_MIN_PP_AMOUNT = 200000.0
DEFAULT_RTS_PROFILE = "IPSokolova"


def settings_path(meta_dir: Path) -> Path:
    return meta_dir / "auto_monitor_draft.json"


def default_settings() -> dict[str, Any]:
    return {
        "version": SETTINGS_VERSION,
        "enabled": False,
        "auto_publish": False,
        "ktru_codes": [],
        "min_pp_amount": DEFAULT_MIN_PP_AMOUNT,
        "max_per_hour": DEFAULT_MAX_PER_HOUR,
        "rts_profile": DEFAULT_RTS_PROFILE,
    }


def parse_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = default_settings()
    if not isinstance(raw, dict):
        return base
    codes_raw = raw.get("ktru_codes")
    codes: list[str] = []
    if isinstance(codes_raw, list):
        codes = [str(x).strip() for x in codes_raw if str(x).strip()]
    elif isinstance(codes_raw, str):
        codes = [x.strip() for x in codes_raw.replace(",", "\n").splitlines() if x.strip()]
    try:
        min_amount = float(raw.get("min_pp_amount", base["min_pp_amount"]))
        if min_amount < 0:
            min_amount = float(base["min_pp_amount"])
    except (TypeError, ValueError):
        min_amount = float(base["min_pp_amount"])
    try:
        max_hour = int(raw.get("max_per_hour", base["max_per_hour"]))
        max_hour = max(1, min(max_hour, 100))
    except (TypeError, ValueError):
        max_hour = int(base["max_per_hour"])
    profile = normalize_rts_profile(raw.get("rts_profile")) or DEFAULT_RTS_PROFILE
    return {
        "version": SETTINGS_VERSION,
        "enabled": bool(raw.get("enabled", False)),
        "auto_publish": bool(raw.get("auto_publish", False)),
        "ktru_codes": codes,
        "min_pp_amount": min_amount,
        "max_per_hour": max_hour,
        "rts_profile": profile,
        "saved_at_utc": raw.get("saved_at_utc"),
    }


def load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return default_settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_settings()
    return parse_settings(data if isinstance(data, dict) else None)


def save_settings(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parsed = parse_settings(payload)
    out = {
        "version": SETTINGS_VERSION,
        "enabled": bool(parsed.get("enabled")),
        "auto_publish": bool(parsed.get("auto_publish")),
        "ktru_codes": list(parsed.get("ktru_codes") or []),
        "min_pp_amount": float(parsed["min_pp_amount"]),
        "max_per_hour": int(parsed["max_per_hour"]),
        "rts_profile": str(parsed.get("rts_profile") or DEFAULT_RTS_PROFILE),
        "saved_at_utc": payload.get("saved_at_utc"),
    }
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def global_enabled(settings: dict[str, Any]) -> bool:
    return bool(settings.get("enabled", False))


def parse_form_settings(form: dict[str, Any]) -> dict[str, Any]:
    codes_text = str(form.get("ktru_codes_text") or "").strip()
    codes = [x.strip() for x in codes_text.replace(",", "\n").splitlines() if x.strip()]
    min_raw = str(form.get("min_pp_amount") or "").strip()
    if not min_raw:
        raise ValueError("Укажите минимальную сумму ПП.")
    min_amount = parse_min_purchase_amount(min_raw)
    if min_amount is None or min_amount < 0:
        raise ValueError("Некорректная минимальная сумма ПП.")
    try:
        max_hour = int(str(form.get("max_per_hour") or DEFAULT_MAX_PER_HOUR).strip())
    except ValueError as exc:
        raise ValueError("Некорректный лимит черновиков в час.") from exc
    max_hour = max(1, min(max_hour, 100))
    profile = normalize_rts_profile(form.get("rts_profile")) or DEFAULT_RTS_PROFILE
    return parse_settings(
        {
            "enabled": form.get("enabled") in (True, "1", "on", "true", "yes"),
            "auto_publish": form.get("auto_publish") in (True, "1", "on", "true", "yes"),
            "ktru_codes": codes,
            "min_pp_amount": min_amount,
            "max_per_hour": max_hour,
            "rts_profile": profile,
        }
    )


def form_from_settings(settings: dict[str, Any]) -> dict[str, str]:
    amount = settings.get("min_pp_amount")
    amount_str = ""
    if amount is not None:
        if float(amount) == int(float(amount)):
            amount_str = str(int(amount))
        else:
            amount_str = str(amount).replace(".", ",")
    codes = settings.get("ktru_codes") or []
    return {
        "enabled": "1" if settings.get("enabled") else "",
        "auto_publish": "1" if settings.get("auto_publish") else "",
        "min_pp_amount": amount_str,
        "ktru_codes_text": "\n".join(codes) if isinstance(codes, list) else "",
        "max_per_hour": str(int(settings.get("max_per_hour") or DEFAULT_MAX_PER_HOUR)),
        "rts_profile": str(settings.get("rts_profile") or DEFAULT_RTS_PROFILE),
    }


def apply_enabled(settings: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    out = deepcopy(settings)
    out["enabled"] = bool(enabled)
    return parse_settings(out)

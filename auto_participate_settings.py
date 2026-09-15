"""Persistent rules for automatic RTS PP draft creation per profile."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from notification_settings import parse_min_purchase_amount
from rts_cookie_profiles import list_known_profiles, normalize_rts_profile

SETTINGS_VERSION = 1
DEFAULT_KTRU_MODE = "catalog"
DEFAULT_MAX_PER_HOUR = 10


def settings_path(meta_dir: Path) -> Path:
    return meta_dir / "auto_participate.json"


def default_profile_rules() -> dict[str, Any]:
    return {
        "enabled": False,
        "min_purchase_amount": 200000,
        "ktru_mode": DEFAULT_KTRU_MODE,
        "ktru_codes": [],
        "min_match_ratio": 0.0,
        "max_per_hour": DEFAULT_MAX_PER_HOUR,
    }


def default_settings() -> dict[str, Any]:
    profiles = {name: default_profile_rules() for name in list_known_profiles()}
    return {"version": SETTINGS_VERSION, "enabled": False, "profiles": profiles}


def global_enabled(settings: dict[str, Any]) -> bool:
    return bool(settings.get("enabled", False))


def count_enabled_profiles(settings: dict[str, Any]) -> int:
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    return sum(
        1
        for name in list_known_profiles()
        if isinstance(profiles.get(name), dict) and profiles.get(name, {}).get("enabled")
    )


def load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return default_settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_settings()
    if not isinstance(data, dict):
        return default_settings()
    base = default_settings()
    raw_profiles = data.get("profiles")
    if isinstance(raw_profiles, dict):
        for name in list_known_profiles():
            raw = raw_profiles.get(name)
            if isinstance(raw, dict):
                merged = {**base["profiles"][name], **raw}
                base["profiles"][name] = parse_profile_rules(merged, profile=name)
    base["version"] = SETTINGS_VERSION
    base["enabled"] = bool(data.get("enabled", False))
    return base


def save_settings(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "version": SETTINGS_VERSION,
        "enabled": bool(payload.get("enabled", False)),
        "profiles": {},
        "saved_at_utc": payload.get("saved_at_utc"),
    }
    profiles = payload.get("profiles")
    if isinstance(profiles, dict):
        for name in list_known_profiles():
            raw = profiles.get(name)
            if isinstance(raw, dict):
                out["profiles"][name] = parse_profile_rules(raw, profile=name)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_profile_rules(raw: dict[str, Any], *, profile: str = "") -> dict[str, Any]:
    del profile
    base = default_profile_rules()
    enabled = raw.get("enabled", base["enabled"])
    ktru_mode = str(raw.get("ktru_mode") or base["ktru_mode"]).strip().lower()
    if ktru_mode not in ("catalog", "whitelist"):
        ktru_mode = DEFAULT_KTRU_MODE
    codes_raw = raw.get("ktru_codes")
    codes: list[str] = []
    if isinstance(codes_raw, list):
        codes = [str(x).strip() for x in codes_raw if str(x).strip()]
    elif isinstance(codes_raw, str):
        codes = [x.strip() for x in codes_raw.replace(",", "\n").splitlines() if x.strip()]
    try:
        min_amount = float(raw.get("min_purchase_amount", base["min_purchase_amount"]))
        if min_amount < 0:
            min_amount = base["min_purchase_amount"]
    except (TypeError, ValueError):
        min_amount = base["min_purchase_amount"]
    try:
        min_ratio = float(raw.get("min_match_ratio", base["min_match_ratio"]))
        min_ratio = max(0.0, min(min_ratio, 1.0))
    except (TypeError, ValueError):
        min_ratio = base["min_match_ratio"]
    try:
        max_hour = int(raw.get("max_per_hour", base["max_per_hour"]))
        max_hour = max(1, min(max_hour, 100))
    except (TypeError, ValueError):
        max_hour = base["max_per_hour"]
    return {
        "enabled": bool(enabled),
        "min_purchase_amount": min_amount,
        "ktru_mode": ktru_mode,
        "ktru_codes": codes,
        "min_match_ratio": min_ratio,
        "max_per_hour": max_hour,
    }


def profile_rules(settings: dict[str, Any], profile: str) -> dict[str, Any]:
    name = normalize_rts_profile(profile)
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    raw = profiles.get(name) if isinstance(profiles, dict) else None
    if isinstance(raw, dict):
        return parse_profile_rules(raw, profile=name)
    return default_profile_rules()


def effective_min_amount(rules: dict[str, Any]) -> float | None:
    raw = rules.get("min_purchase_amount")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def parse_form_profile_rules(form: dict[str, Any]) -> dict[str, Any]:
    codes_text = str(form.get("ktru_codes_text") or "").strip()
    codes = [x.strip() for x in codes_text.replace(",", "\n").splitlines() if x.strip()]
    min_amount_raw = str(form.get("min_purchase_amount") or "").strip()
    if not min_amount_raw:
        raise ValueError("Укажите минимальную сумму позиции.")
    min_amount = parse_min_purchase_amount(min_amount_raw)
    if min_amount is None or min_amount < 0:
        raise ValueError("Некорректная минимальная сумма позиции.")
    ratio_raw = str(form.get("min_match_ratio") or "").strip().replace(",", ".")
    min_ratio = float(ratio_raw) / 100.0 if ratio_raw else 0.0
    min_ratio = max(0.0, min(min_ratio, 1.0))
    try:
        max_hour = int(str(form.get("max_per_hour") or DEFAULT_MAX_PER_HOUR).strip())
    except ValueError:
        max_hour = DEFAULT_MAX_PER_HOUR
    max_hour = max(1, min(max_hour, 100))
    ktru_mode = str(form.get("ktru_mode") or DEFAULT_KTRU_MODE).strip().lower()
    if ktru_mode not in ("catalog", "whitelist"):
        ktru_mode = DEFAULT_KTRU_MODE
    return parse_profile_rules(
        {
            "enabled": form.get("enabled") in (True, "1", "on", "true", "yes"),
            "min_purchase_amount": min_amount,
            "ktru_mode": ktru_mode,
            "ktru_codes": codes,
            "min_match_ratio": min_ratio,
            "max_per_hour": max_hour,
        }
    )


def form_from_profile_rules(rules: dict[str, Any]) -> dict[str, str]:
    amount = rules.get("min_purchase_amount")
    amount_str = ""
    if amount is not None:
        if float(amount) == int(float(amount)):
            amount_str = str(int(amount))
        else:
            amount_str = str(amount).replace(".", ",")
    ratio = float(rules.get("min_match_ratio") or 0) * 100.0
    if ratio == int(ratio):
        ratio_str = str(int(ratio))
    else:
        ratio_str = f"{ratio:.1f}".replace(".", ",")
    codes = rules.get("ktru_codes") or []
    return {
        "enabled": "1" if rules.get("enabled") else "",
        "min_purchase_amount": amount_str,
        "ktru_mode": str(rules.get("ktru_mode") or DEFAULT_KTRU_MODE),
        "ktru_codes_text": "\n".join(codes) if isinstance(codes, list) else "",
        "min_match_ratio": ratio_str,
        "max_per_hour": str(int(rules.get("max_per_hour") or DEFAULT_MAX_PER_HOUR)),
    }


def update_profile_in_settings(settings: dict[str, Any], profile: str, rules: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(settings)
    if not isinstance(out.get("profiles"), dict):
        out["profiles"] = {}
    out["profiles"][normalize_rts_profile(profile)] = parse_profile_rules(rules, profile=profile)
    return out


def apply_admin_global_enabled(settings: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    """Включить/выключить автоучастие для всех профилей из админки."""
    out = deepcopy(settings)
    out["enabled"] = enabled
    profiles = out.get("profiles") if isinstance(out.get("profiles"), dict) else {}
    for name in list_known_profiles():
        raw = profiles.get(name)
        if not isinstance(raw, dict):
            raw = default_profile_rules()
        merged = {**raw, "enabled": enabled}
        profiles[name] = parse_profile_rules(merged, profile=name)
    out["profiles"] = profiles
    return out

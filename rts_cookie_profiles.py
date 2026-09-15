"""Cookie РТС и state опроса приглашений — отдельно на каждого веб-оператора."""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_RTS_PROFILE = ""
_CUSTOMER_MONITOR_PROFILE = "customer_monitor"
_DEFAULT_PROFILES = ("IPSokolova", "Nacpro")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def list_known_profiles() -> tuple[str, ...]:
    raw = os.environ.get("RTS_COOKIE_PROFILES", "").strip()
    if raw:
        parts = [x.strip() for x in raw.split(",") if x.strip()]
        return tuple(parts)
    return _DEFAULT_PROFILES


def normalize_rts_profile(profile: str | None) -> str:
    return (profile or "").strip()


def rts_profile_for_web_user(login: str | None) -> str:
    """Веб-логин = id профиля РТС (IPSokolova, Nacpro, …)."""
    return normalize_rts_profile(login)


def validate_rts_profile(profile: str | None) -> str:
    p = normalize_rts_profile(profile)
    if not p:
        raise ValueError("Укажите profile (логин оператора: IPSokolova, Nacpro, …).")
    if not _PROFILE_RE.match(p):
        raise ValueError(f"Недопустимый profile: {p!r}")
    if p == _CUSTOMER_MONITOR_PROFILE:
        return p
    known = list_known_profiles()
    if known and p not in known:
        raise ValueError(f"Неизвестный profile {p!r}. Допустимо: {', '.join(known)}")
    return p


def is_customer_monitor_profile(profile: str | None) -> bool:
    return normalize_rts_profile(profile) == _CUSTOMER_MONITOR_PROFILE


def cookies_dir(meta_dir: Path) -> Path:
    raw = os.environ.get("RTS_COOKIES_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (meta_dir / "cookies").resolve()


def cookie_path_for_profile(profile: str, meta_dir: Path, project_root: Path) -> Path:
    p = normalize_rts_profile(profile)
    if p == _CUSTOMER_MONITOR_PROFILE:
        from rts_customer_pp_api import customer_cookie_path

        return customer_cookie_path(meta_dir)
    if not p:
        legacy = os.environ.get("RTS_COOKIE_FILE", "").strip()
        if legacy:
            return Path(legacy).expanduser().resolve()
        return (project_root / "cookies_rts.txt").resolve()
    return cookies_dir(meta_dir) / f"{p}.txt"


def invites_state_path_for_profile(profile: str, meta_dir: Path, default_state_name: str) -> Path:
    p = normalize_rts_profile(profile)
    if not p:
        return meta_dir / default_state_name
    return meta_dir / f"rts_invites_state_{p}.json"


def ingest_meta_filename(profile: str) -> str:
    p = normalize_rts_profile(profile)
    if not p:
        return "rts_cookie_ingest_meta.json"
    return f"rts_cookie_ingest_meta_{p}.json"

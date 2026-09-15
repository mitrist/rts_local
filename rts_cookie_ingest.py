"""Приём cookie РТС от браузерного расширения и проверка сессии."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from rts_cookie_profiles import ingest_meta_filename, is_customer_monitor_profile, normalize_rts_profile
from rts_invites_poll import load_cookie_header, looks_like_login_or_error_html, save_cookie_to_file

_COOKIE_PAIR_RE = re.compile(r"^[^;\s]+=.+")
_JQGRID_PROBE_URL = "https://app.rts-tender.ru/supplier/lk/api/lot-items/search/paged"
_AUTH_SSO = ".Auth.Sso"
_AUTH_RTS_SUPPLIER = ".Auth.Rts.P"
_AUTH_RTS_CUSTOMER = ".Auth.Rts.C"


def cookie_ingest_api_key() -> str:
    return os.environ.get("RTS_COOKIE_INGEST_KEY", "").strip()


def cookie_ingest_enabled() -> bool:
    return bool(cookie_ingest_api_key())


def validate_ingest_api_key(provided: str | None) -> bool:
    expected = cookie_ingest_api_key()
    if not expected:
        return False
    return (provided or "").strip() == expected


def meta_path(meta_dir: Path, *, rts_profile: str = "") -> Path:
    return meta_dir / ingest_meta_filename(normalize_rts_profile(rts_profile))


def read_ingest_meta(meta_dir: Path, *, rts_profile: str = "") -> dict[str, Any]:
    path = meta_path(meta_dir, rts_profile=rts_profile)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_ingest_meta(meta_dir: Path, payload: dict[str, Any], *, rts_profile: str = "") -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    path = meta_path(meta_dir, rts_profile=rts_profile)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cookies_from_items(items: list[dict[str, Any]]) -> str:
    pairs: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if name and value is not None:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def parse_cookie_payload(body: dict[str, Any]) -> str:
    """Нормализует тело POST (cookie / output / cookies[]) в строку Cookie."""
    if not isinstance(body, dict):
        raise ValueError("Ожидается JSON-объект.")

    raw_cookie = body.get("cookie")
    if isinstance(raw_cookie, str) and raw_cookie.strip():
        return _normalize_cookie_string(raw_cookie)

    output = body.get("output")
    if isinstance(output, str) and output.strip():
        return _normalize_cookie_string(output)

    cookies = body.get("cookies")
    if isinstance(cookies, list):
        text = cookies_from_items(cookies)
        if text:
            return text

    raise ValueError("Укажите поле cookie, output или cookies[].")


def _normalize_cookie_string(raw: str) -> str:
    text = raw.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    # Убрать обёртку document.cookie = "..." если расширение прислало JS.
    if "document.cookie" in text:
        m = re.search(r'document\.cookie\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            text = m.group(1)
    parts = [p.strip() for p in text.split(";") if p.strip()]
    valid = [p for p in parts if _COOKIE_PAIR_RE.match(p)]
    if not valid:
        raise ValueError("Не удалось разобрать строку cookie.")
    return "; ".join(valid)


def _cookie_contains_name(cookie: str, name: str) -> bool:
    return name.lower() in cookie.lower()


def cookie_has_auth_markers(cookie: str) -> bool:
    """Сессия поставщика: .Auth.Sso + .Auth.Rts.P."""
    return _cookie_contains_name(cookie, _AUTH_SSO) and _cookie_contains_name(cookie, _AUTH_RTS_SUPPLIER)


def cookie_has_customer_auth_markers(cookie: str) -> bool:
    """Сессия заказчика: .Auth.Sso + .Auth.Rts.C (или .Auth.Rts.P)."""
    if not _cookie_contains_name(cookie, _AUTH_SSO):
        return False
    return _cookie_contains_name(cookie, _AUTH_RTS_CUSTOMER) or _cookie_contains_name(
        cookie, _AUTH_RTS_SUPPLIER
    )


def cookie_auth_ok_for_profile(cookie: str, *, rts_profile: str = "") -> bool:
    if is_customer_monitor_profile(rts_profile):
        return cookie_has_customer_auth_markers(cookie)
    return cookie_has_auth_markers(cookie)


def _probe_lk_session(cookie: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Проверка jqGrid app.rts-tender.ru (опрос приглашений)."""
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Cookie": cookie,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://app.rts-tender.ru/supplier/lk/Private/MessagesList.aspx",
    }
    params = {
        "auto_SendingDateTimeFrom": "01.01.2020",
        "auto_SendingDateTimeTo": "31.12.2030",
        "auto_IsOnlyNotRead": "false",
        "nd": "1",
        "rows": "1",
        "page": "1",
        "sidx": "SendingDateTime",
        "sord": "desc",
    }
    try:
        r = requests.get(_JQGRID_PROBE_URL, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "channel": "lk", "reason": "network_error", "detail": str(exc)[:500]}
    text = (r.text or "").strip()
    if r.status_code in (401, 403):
        return {"ok": False, "channel": "lk", "reason": "http_auth", "http_status": r.status_code}
    if looks_like_login_or_error_html(text):
        return {"ok": False, "channel": "lk", "reason": "login_html", "http_status": r.status_code}
    if r.status_code != 200:
        return {"ok": False, "channel": "lk", "reason": "http_error", "http_status": r.status_code}
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "channel": "lk", "reason": "not_json", "http_status": r.status_code}
    return {"ok": True, "channel": "lk", "http_status": r.status_code}


def probe_rts_session(cookie: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Сначала offers (ПП), затем ЛК — для отображения статуса cookie."""
    from rts_pp_api import probe_offers_session

    offers = probe_offers_session(cookie, timeout=timeout)
    if offers.get("ok"):
        return offers
    lk = _probe_lk_session(cookie, timeout=timeout)
    if lk.get("ok"):
        return lk
    return offers


def probe_session_for_profile(
    cookie: str,
    *,
    rts_profile: str = "",
    meta_dir: Path | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    if is_customer_monitor_profile(rts_profile):
        from rts_customer_pp_api import probe_customer_session

        return probe_customer_session(cookie, meta_dir=meta_dir, timeout=timeout)
    return probe_rts_session(cookie, timeout=timeout)


def save_cookie_from_ingest(
    *,
    cookie_path: Path,
    meta_dir: Path,
    body: dict[str, Any],
    source: str = "extension",
    probe: bool = True,
    rts_profile: str = "",
) -> dict[str, Any]:
    cookie = parse_cookie_payload(body)
    profile = normalize_rts_profile(rts_profile)
    if not cookie_auth_ok_for_profile(cookie, rts_profile=profile):
        if is_customer_monitor_profile(profile):
            raise ValueError(
                "В cookie нет .Auth.Sso и .Auth.Rts.C (сессия заказчика). "
                "Скопируйте заголовок Cookie из DevTools → Network (запрос ktru/code или compare "
                "на platform.rts-tender.ru)."
            )
        raise ValueError(
            "В cookie нет .Auth.Sso или .Auth.Rts.P — войдите на rts-tender.ru по ЭП и повторите."
        )
    save_cookie_to_file(cookie_path, cookie)
    probe_result = (
        probe_session_for_profile(cookie, rts_profile=profile, meta_dir=meta_dir)
        if probe
        else {"ok": None, "skipped": True}
    )
    meta = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "cookie_chars": len(cookie),
        "has_auth_markers": True,
        "probe": probe_result,
        "rts_profile": profile,
    }
    write_ingest_meta(meta_dir, meta, rts_profile=profile)
    return {
        "ok": True,
        "message": "Cookie сохранён.",
        "cookie_chars": len(cookie),
        "probe": probe_result,
        "updated_at_utc": meta["updated_at_utc"],
        "rts_profile": profile,
    }


def cookie_file_status(cookie_path: Path, meta_dir: Path, *, rts_profile: str = "") -> dict[str, Any]:
    profile = normalize_rts_profile(rts_profile)
    meta = read_ingest_meta(meta_dir, rts_profile=profile)
    if not cookie_path.is_file():
        return {
            "file_exists": False,
            "ingest_enabled": cookie_ingest_enabled(),
            "last_updated_at_utc": meta.get("updated_at_utc"),
            "probe_ok": None,
            "rts_profile": profile,
        }
    try:
        cookie = load_cookie_header(cookie_path)
    except ValueError:
        cookie = ""
    return {
        "file_exists": True,
        "cookie_chars": len(cookie),
        "has_auth_markers": cookie_auth_ok_for_profile(cookie, rts_profile=profile) if cookie else False,
        "ingest_enabled": cookie_ingest_enabled(),
        "last_updated_at_utc": meta.get("updated_at_utc"),
        "last_source": meta.get("source"),
        "probe_ok": (meta.get("probe") or {}).get("ok"),
        "probe_reason": (meta.get("probe") or {}).get("reason"),
        "rts_profile": profile,
    }

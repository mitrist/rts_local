"""
Веб-консоль RTS: дашборд опроса IncomingInvite и обновление Cookie.

Запуск из корня проекта (по умолчанию порт 8765 — см. RTS_WEB_PORT):
  python -m web_ui

Или напрямую:
  uvicorn web_ui.main:app --host 0.0.0.0 --port 8765

Опционально: RTS_WEB_USERS_FILE (логин:пароль на строку) или RTS_WEB_USER + RTS_WEB_PASSWORD — форма /login и сессия.

Метаданные ЕИС: last_eis_poll.json и eis_ezt_last_run.json в RTS_META_DIR.
Переменные: EIS_ENV_FILE (guid ЕРУЗ), EIS_STATE_FILE, EIS_GET_DOCS_IP_URL.
Плановый опрос ЕИС из веба: кнопка «Запустить cron задачу» (каждые 120 с по умолчанию) или EIS_SCHEDULE_POLL=1, EIS_SCHEDULE_ORG_REGION=72 [, EIS_SCHEDULE_INTERVAL_SEC=120, EIS_SCHEDULE_KTRU=... ]. Кнопка имеет приоритет над ENV в одном процессе.
Журнал КТРУ: eis_ktru_changelog.json в RTS_META_DIR (хранение ~EIS_CHANGELOG_RETENTION_DAYS дней).
TLS к ЕИС: EIS_WEB_SSL_LEGACY=1 и/или EIS_WEB_TLS_MAX_12=1; int44: EIS_LEGACY_INT44=1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote_plus, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from pp_stack import stack_key as pp_stack_key
from auto_participate import run_auto_participate_after_html
from auto_participate_settings import (
    apply_admin_global_enabled,
    count_enabled_profiles,
    form_from_profile_rules,
    global_enabled,
    load_settings as load_auto_participate_settings,
    parse_form_profile_rules,
    profile_rules,
    save_settings as save_auto_participate_settings,
    settings_path as auto_participate_settings_path,
    update_profile_in_settings,
)
from eis_analytics_db import (
    DEFAULT_DB_NAME,
    analytics_db_path,
    enrich_notice_characteristics,
    delete_pp_monitor_draft,
    delete_rts_pp_draft,
    is_monitor_draft_line_key,
    list_pp_monitor_drafts,
    load_pp_monitor_draft_created_at,
    monitor_draft_line_key,
    monitor_draft_offer_id_from_line_key,
    get_auto_participate_attempt,
    get_rts_pp_draft,
    list_auto_participate_attempts,
    clear_pp_monitor_notifications,
    clear_pp_monitor_registry,
    backfill_pp_characteristics_hashes,
    count_pp_monitor_stacks,
    get_pp_monitor_draft,
    get_pp_monitor_finding,
    get_pp_monitor_notification_latest,
    get_pp_monitor_registry_row,
    list_pp_monitor_findings,
    count_pp_monitor_registry,
    count_pp_monitor_registry_groups,
    count_pp_eis_matches,
    enrich_pp_eis_matches_with_unit_prices,
    pp_winner_offer_ids,
    list_pp_eis_matches,
    get_pp_eis_match,
    set_pp_eis_match_status,
    pp_eis_match_stats,
    list_pp_monitor_registry,
    list_pp_monitor_registry_groups,
    list_pp_monitor_registry_ktru_codes,
    list_pp_monitor_registry_names,
    list_pp_monitor_stack_members,
    list_pp_monitor_stacks,
    list_pp_monitor_journal_with_findings,
    list_pp_monitor_recent,
    list_rts_pp_drafts,
    history_analytics_v2_summary,
    history_dashboard_summary,
    last_rss_poll_run,
    notice_html_fetch_candidates,
    notice_html_payload,
    notice_line_for_pp,
    msk_day_bounds_utc,
    offer_matching_ktru_count,
    offer_matching_ktru_rows,
    offer_watch_candidates,
    record_offer_watch_notification,
    record_notice_html_page,
    rss_vs_getdocs_recent,
    save_rts_pp_draft,
    display_compare_datetime_msk,
    upsert_pp_monitor_finding,
    shelf_purchase_rows,
)
from eis_history_config import HISTORY_PERIOD_FROM, HISTORY_PERIOD_TO
from eis_history_db import (
    HISTORY_DB_DEFAULT,
    get_history_coverage_period,
    get_latest_backfill_job,
    history_db_path,
)
from eis_changelog import (
    enrich_changelog_rows_from_customer_characteristics,
    format_utc_iso_msk_clean,
    read_changelog_rows_for_dashboard,
)
from eis_ezt_poll import EisPollConfig, load_eis_state, parse_date_iso, run_eis_poll
from eis_notice_html_fetch import fetch_notice_common_info_html
from eis_notice_html_parse import parse_notice_common_info_html
from eis_rss_poll import DEFAULT_QUERY_LABEL, DEFAULT_RSS_URL, EisRssConfig, run_eis_rss_poll
from eis_supplier_characteristics import NullSupplierCharacteristicsSource, build_comparison_rows
from eis_pp_link import format_lag_minutes
from eis_pp_event_chain import build_pp_eis_event_chain
from eis_offer_matching_outcome import sync_offer_matching_lines
from eis_pp_scenario_tags import tagging_ui_context
from pp_prehistory import enrich_matches_with_prehistory, find_predecessors
from eis_pp_link_service import pilot_ktru_codes, pp_eis_link_window_min
from notice_to_rts_pp import compare_notice_to_draft
from offer_matcher import rank_offer_matches
from notification_settings import (
    admin_form_from_settings,
    load_settings as load_notification_settings,
    max_purchase_amount_from_settings,
    min_purchase_amount_from_settings,
    notify_flag_from_settings,
    parse_admin_form,
    parse_min_purchase_amount,
    passes_min_purchase_filter,
    passes_purchase_amount_filter,
    save_settings as save_notification_settings,
    settings_path as notification_settings_path,
)
from notify import (
    html_bold,
    html_link,
    markdown_bold,
    markdown_link,
    max_creds_from_env_file,
    resolve_max_chat_id,
    resolve_telegram_chat_id,
    send_max_markdown_long,
    send_telegram_html_long,
    telegram_creds_from_env_file,
)
from rts_customer_pp_api import (
    CUSTOMER_MONITOR_PROFILE,
    RtsCustomerPpApiError,
    characteristic_preview_lines,
    customer_cookie_path,
    find_offer_item,
    list_characteristic_rows,
    load_customer_cookie,
    probe_customer_session,
)
from rts_pp_monitor import (
    MAX_PP_MONITOR_CHANNEL,
    TELEGRAM_PP_MONITOR_CHANNEL,
    run_rts_pp_monitor_backfill,
    run_rts_pp_monitor_poll,
)
from rts_pp_monitor_settings import (
    apply_admin_runtime_settings,
    form_from_settings as pp_monitor_form_from_settings,
    MIN_POLL_INTERVAL_MIN,
    MAX_POLL_INTERVAL_MIN,
    load_settings as load_pp_monitor_settings,
    parse_admin_notify_form,
    parse_form_payload as parse_pp_monitor_form,
    save_settings as save_pp_monitor_settings,
    settings_path as pp_monitor_settings_path,
)
from rts_pp_monitor_cron import (
    PollFileLock,
    cron_lock_path,
    cron_mode_enabled,
    cron_status_payload,
    load_cron_state,
    poll_interval_sec as pp_monitor_poll_interval_sec,
    resolve_poll_running,
    stale_running_state,
    run_poll_once as run_pp_monitor_cron_poll,
)
from rts_cookie_profiles import (
    cookie_path_for_profile,
    cookies_dir,
    invites_state_path_for_profile,
    list_known_profiles,
    normalize_rts_profile,
    rts_profile_for_web_user,
    validate_rts_profile,
)
from rts_cookie_ingest import (
    cookie_file_status,
    cookie_ingest_enabled,
    save_cookie_from_ingest,
    validate_ingest_api_key,
)
from rts_invites_poll import PollConfig, load_state, run_poll, save_cookie_to_file
from rts_preoffers import (
    RtsPreoffer,
    delete_preoffer,
    disable_preoffer,
    load_preoffers,
    load_preoffers_raw,
    normalize_preoffer_payload,
    preoffer_by_id,
    preoffers_by_ktru,
    upsert_preoffer,
)
from ktru_country_origin import (
    COUNTRY_CHOICES,
    CountryOriginRow,
    default_country_origin_path,
    delete_country_origin_row,
    load_country_origin_rows,
    upsert_country_origin_row,
)
from rts_ktru_cache import (
    cache_dir_default,
    default_ktru_cache_ttl_sec,
    extend_ktru_cache_validity,
    extend_all_ktru_cache_validity,
    import_ktru_template,
    is_ktru_template_cached,
    list_ktru_cache_status,
    load_cached_template,
)
from rts_pp_draft_service import create_pp_draft_from_monitor, create_pp_draft_from_notice
from rts_pp_publish_service import (
    cancel_publish_job,
    get_publish_job_status,
    publish_enabled as pp_publish_enabled,
    start_publish_job,
    submit_publish_signatures,
)
from rts_pp_api import (
    RtsPpApiError,
    draft_edit_url,
    get_draft,
    load_offers_cookie,
    validate_cookie_session,
)
from rts_registry_import import (
    apply_registry_import,
    delete_registry_import_draft,
    items_from_payload,
    load_registry_import_draft,
    parse_registry_json,
    payload_from_items,
    preview_rows_from_items,
    save_registry_import_draft,
)
from participation_price import (
    compute_participation_economics,
    get_price_ord_catalog,
    parse_money_amount,
    participation_economics_disabled,
)
from web_ui.day_summary_links import build_day_summary_compare_url, summary_date_from_row
from web_ui.health_monitor import heartbeat_loop, monitor as health_monitor, router as health_router
from web_ui.health_monitor import request_timing_middleware
from web_ui.auth import (
    SESSION_USER_KEY,
    auth_enabled,
    configure_web_auth,
    delete_web_user,
    list_web_users_for_admin,
    login_user,
    logout_user,
    require_session,
    require_settings_admin,
    safe_next_path,
    session_username,
    is_settings_admin,
    ui_context as web_auth_ui_context,
    upsert_web_user,
    verify_credentials,
    web_users_file_path,
)

_app_dir = Path(__file__).resolve().parent
_project_root = Path(os.environ.get("RTS_PROJECT_ROOT", str(_app_dir.parent))).resolve()

_COOKIE = Path(os.environ.get("RTS_COOKIE_FILE", str(_project_root / "cookies_rts.txt"))).resolve()


def _rts_profile_for_request(request: Request) -> str:
    return rts_profile_for_web_user(session_username(request))


def _require_operator_rts_profile(request: Request) -> str:
    """Supplier draft/publish actions require login as IPSokolova/Nacpro (not admin)."""
    profile = _rts_profile_for_request(request)
    known = list_known_profiles()
    if auth_enabled() and (not profile or profile not in known):
        names = ", ".join(known) if known else "IPSokolova, Nacpro"
        raise HTTPException(
            status_code=401,
            detail=(
                f"Войдите в консоль под оператором ({names}). "
                f"Сейчас сессия: «{session_username(request) or '—'}» — для неё нет cookie ЛК поставщика."
            ),
        )
    return profile


def _pp_draft_profiles_for_request(request: Request) -> tuple[list[str], bool]:
    """Profiles whose PP drafts are visible; admin sees all operator profiles."""
    user = session_username(request)
    admin_view = is_settings_admin(user)
    if admin_view:
        return list(list_known_profiles()), True
    profile = _rts_profile_for_request(request)
    if profile:
        return [profile], False
    return [], False


def _draft_rts_profile_for_action(request: Request, explicit: str | None = None) -> str:
    user = session_username(request)
    if is_settings_admin(user):
        profile = normalize_rts_profile(explicit)
        if not profile or profile not in list_known_profiles():
            raise HTTPException(
                status_code=400,
                detail="Укажите rts_profile оператора (IPSokolova или Nacpro).",
            )
        return profile
    profile = _rts_profile_for_request(request)
    if not profile:
        raise HTTPException(
            status_code=401,
            detail="Требуется вход под оператором (IPSokolova или Nacpro).",
        )
    return profile


def _cookie_path_for_request(request: Request) -> Path:
    return cookie_path_for_profile(_rts_profile_for_request(request), _META, _project_root)


def _state_path_for_request(request: Request) -> Path:
    return invites_state_path_for_profile(_rts_profile_for_request(request), _META, _STATE_DEFAULT_NAME)


def _cookie_path_for_profile(profile: str) -> Path:
    return cookie_path_for_profile(profile, _META, _project_root)
_STATE = Path(os.environ.get("RTS_STATE_FILE", str(_project_root / "rts_invites_state.json"))).resolve()
_STATE_DEFAULT_NAME = _STATE.name
_META = Path(os.environ.get("RTS_META_DIR", str(_STATE.parent))).resolve()
_KTRU_CACHE = cache_dir_default(_META)
_EIS_ENV = Path(os.environ.get("EIS_ENV_FILE", str(_project_root / "env.txt"))).resolve()
def _web_users_path() -> Path:
    return web_users_file_path(_project_root)


_WEB_USERS = _web_users_path()
configure_web_auth(_WEB_USERS)
_EIS_STATE = Path(os.environ.get("EIS_STATE_FILE", str(_project_root / "eis_ezt_state.json"))).resolve()
_LAST_EIS_POLL = "last_eis_poll.json"
_LAST_BODIES = "last_invite_bodies.json"
_EIS_RSS_SETTINGS = "eis_rss_cron_settings.json"
_LOG_WEB = logging.getLogger("web_ui.eis_schedule")
_CURRENT_RSS_URL_DEFAULT = (
    "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html?"
    "morphology=on&sortBy=UPDATE_DATE&pageNumber=1&sortDirection=false&recordsPerPage=_10&"
    "showLotsInfoHidden=false&fz44=on&af=on&ca=on&pc=on&pa=on&placingWayList=EZT20%2CEZTP20&"
    "selectedLaws=FZ44&etp_7360768=on&etp=7360768&priceContractAdvantages44IdNameHidden=%7B%7D&"
    "currencyIdGeneral=-1&selectedSubjectsIdNameHidden=%7B%7D&koksIdsIdNameHidden=%7B%7D&"
    "gws=Выберите+тип+закупки"
)

_eis_supervisor_task: asyncio.Task[None] | None = None
_eis_rss_supervisor_task: asyncio.Task[None] | None = None
_eis_html_supervisor_task: asyncio.Task[None] | None = None
_eis_polling_now: bool = False
_eis_next_poll_unix: float | None = None
_eis_rss_polling_now: bool = False
_eis_rss_next_poll_unix: float | None = None
_eis_html_auto_fetch_lock = threading.Lock()
_eis_html_db_backoff_until: float = 0.0
_eis_html_supervisor_sleep_sec: int = 5
_rts_pp_monitor_lock = threading.Lock()
_rts_pp_monitor_supervisor_task: asyncio.Task[None] | None = None
_rts_pp_monitor_polling_now: bool = False
_rts_pp_monitor_next_poll_unix: float | None = None
_customer_monitor_probe_cache: dict[str, Any] | None = None
_customer_monitor_probe_cached_at: float = 0.0
_CUSTOMER_PROBE_CACHE_SEC = 900.0


@dataclass
class EisUiCronState:
    enabled: bool = False
    interval_sec: int = 120
    org_region: str = "35"
    document_type_44: str = "epNotificationEZT2020"
    ktru_csv: str | None = None
    exact_date: str | None = None


_eis_ui_cron = EisUiCronState()


def _eis_rss_settings_path() -> Path:
    return _META / _EIS_RSS_SETTINGS


def _read_eis_rss_settings() -> dict[str, Any]:
    path = _eis_rss_settings_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_eis_rss_settings() -> None:
    _META.mkdir(parents=True, exist_ok=True)
    payload = {
        "rss_url": _eis_rss_cron.rss_url,
        "query_label": _eis_rss_cron.query_label,
        "interval_sec": int(_eis_rss_cron.interval_sec),
        "enabled": bool(_eis_rss_cron.enabled),
        "saved_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    path = _eis_rss_settings_path()
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass
class EisRssCronState:
    enabled: bool = False
    interval_sec: int = 30
    rss_url: str = _CURRENT_RSS_URL_DEFAULT
    query_label: str = DEFAULT_QUERY_LABEL


_eis_rss_saved_settings = _read_eis_rss_settings()


def _rss_saved_int(name: str, default: int) -> int:
    try:
        return int(_eis_rss_saved_settings.get(name) or default)
    except (TypeError, ValueError):
        return default


def _rss_saved_bool(name: str, default: bool) -> bool:
    raw = _eis_rss_saved_settings.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


_eis_rss_cron = EisRssCronState(
    enabled=_rss_saved_bool(
        "enabled",
        os.environ.get("EIS_RSS_CRON_ENABLED", "").strip().lower() in ("1", "true", "yes"),
    ),
    interval_sec=max(30, int(os.environ.get("EIS_RSS_CRON_INTERVAL_SEC", str(_rss_saved_int("interval_sec", 30))))),
    rss_url=(
        os.environ.get("EIS_RSS_URL")
        or (
            ""
            if str(_eis_rss_saved_settings.get("rss_url") or "").strip() == DEFAULT_RSS_URL
            else str(_eis_rss_saved_settings.get("rss_url") or "")
        )
        or _CURRENT_RSS_URL_DEFAULT
    ).strip(),
    query_label=(
        os.environ.get("EIS_RSS_QUERY_LABEL")
        or str(_eis_rss_saved_settings.get("query_label") or "")
        or DEFAULT_QUERY_LABEL
    ).strip(),
)

_eis_supplier_chars_stub = NullSupplierCharacteristicsSource()


def _eis_changelog_path() -> Path:
    raw = os.environ.get("EIS_CHANGELOG_FILE", "eis_ktru_changelog.json").strip() or "eis_ktru_changelog.json"
    return _META / Path(raw).name


def _eis_analytics_db_path() -> Path:
    return analytics_db_path(_META, os.environ.get("EIS_ANALYTICS_DB", DEFAULT_DB_NAME))


def _eis_history_db_path() -> Path:
    raw = os.environ.get("EIS_HISTORY_DB", HISTORY_DB_DEFAULT).strip() or HISTORY_DB_DEFAULT
    return history_db_path(raw)


def _default_history_period() -> tuple[date, date]:
    coverage = get_history_coverage_period(_eis_history_db_path())
    if coverage:
        return coverage
    job = get_latest_backfill_job(_eis_history_db_path())
    if job:
        try:
            return date.fromisoformat(str(job["date_from"])), date.fromisoformat(str(job["date_to"]))
        except ValueError:
            pass
    return HISTORY_PERIOD_FROM, HISTORY_PERIOD_TO


def _rts_preoffers_path() -> Path:
    raw = os.environ.get("RTS_PREOFFERS_FILE", "rts_preoffers.json").strip() or "rts_preoffers.json"
    p = Path(raw)
    return p if p.is_absolute() else _project_root / p.name


def _country_origin_path() -> Path:
    return default_country_origin_path(_project_root)


def _country_origin_empty_form() -> dict[str, Any]:
    return {
        "row_id": "",
        "ktru_code": "",
        "title": "",
        "country_name": COUNTRY_CHOICES[0][1],
        "enabled": True,
    }


def _country_origin_form_from_row(row: CountryOriginRow) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "ktru_code": row.ktru_code,
        "title": row.title,
        "country_name": row.country_name,
        "enabled": row.enabled,
    }


def _country_origin_row_payload(row: CountryOriginRow) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "ktru_code": row.ktru_code,
        "title": row.title,
        "country_name": row.country_name,
        "enabled": row.enabled,
    }


_templates = Jinja2Templates(directory=str(_app_dir / "templates"))
_templates.env.filters["utc_iso_web"] = format_utc_iso_msk_clean

Auth = Annotated[str, Depends(require_session)]
SettingsAdmin = Annotated[str, Depends(require_settings_admin)]


def _auto_participate_settings_path() -> Path:
    return auto_participate_settings_path(_META)


def _pp_monitor_settings_path() -> Path:
    return pp_monitor_settings_path(_META)


def _customer_monitor_cookie_path() -> Path:
    return customer_cookie_path(_META)


def _customer_monitor_cookie_status() -> dict[str, Any]:
    global _customer_monitor_probe_cache, _customer_monitor_probe_cached_at
    cookie_path = _customer_monitor_cookie_path()
    status = cookie_file_status(cookie_path, _META, rts_profile=CUSTOMER_MONITOR_PROFILE)
    if not status.get("file_exists"):
        return status
    try:
        from rts_invites_poll import load_cookie_header

        cookie = load_cookie_header(cookie_path).strip()
        if cookie:
            now = time.time()
            if (
                _customer_monitor_probe_cache is not None
                and now - _customer_monitor_probe_cached_at < _CUSTOMER_PROBE_CACHE_SEC
            ):
                probe = _customer_monitor_probe_cache
            else:
                settings = load_pp_monitor_settings(_pp_monitor_settings_path())
                ktru_codes = settings.get("ktru_codes") or []
                probe_ktru = str(ktru_codes[0]) if ktru_codes else "26.20.13.000-00000002"
                probe = probe_customer_session(cookie, ktru_code=probe_ktru, meta_dir=_META)
                _customer_monitor_probe_cache = probe
                _customer_monitor_probe_cached_at = now
            status["probe_ok"] = probe.get("ok")
            status["probe_reason"] = probe.get("reason")
            status["probe_items_count"] = probe.get("registry_total", probe.get("items_count"))
    except Exception as exc:  # noqa: BLE001
        status["probe_ok"] = False
        status["probe_reason"] = str(exc)[:200]
    return status


def _pp_monitor_senders(timeout: float) -> list[tuple[str, str, Any, str, str, float, str]]:
    try:
        tg_token, _ = telegram_creds_from_env_file(_EIS_ENV)
        max_token, _ = max_creds_from_env_file(_EIS_ENV)
        monitor = load_pp_monitor_settings(_pp_monitor_settings_path())
        tg_chat = resolve_telegram_chat_id(_EIS_ENV, str(monitor.get("telegram_chat_id") or ""))
        max_chat = resolve_max_chat_id(_EIS_ENV, str(monitor.get("max_chat_id") or ""))
    except Exception:
        _LOG_WEB.exception("PP monitor: cannot load messenger credentials")
        return []
    return [
        (
            TELEGRAM_PP_MONITOR_CHANNEL,
            "Telegram PP monitor",
            send_telegram_html_long,
            tg_token,
            tg_chat,
            timeout,
            "html",
        ),
        (
            MAX_PP_MONITOR_CHANNEL,
            "MAX PP monitor",
            send_max_markdown_long,
            max_token,
            max_chat,
            timeout,
            "markdown",
        ),
    ]


def _run_rts_pp_monitor_backfill_once(*, force: bool = False) -> dict[str, Any]:
    if not _rts_pp_monitor_lock.acquire(blocking=False):
        return {"ok": False, "skipped": True, "reason": "busy"}
    try:
        return run_rts_pp_monitor_backfill(
            _META,
            _eis_analytics_db_path(),
            force=force,
        )
    finally:
        _rts_pp_monitor_lock.release()


def _pp_monitor_poll_busy_message() -> str | None:
    if cron_mode_enabled():
        state = cron_status_payload()
        if state.get("polling"):
            return "Опрос уже выполняется."
        lock = PollFileLock(cron_lock_path())
        if not lock.acquire():
            return "Опрос уже выполняется."
        lock.release()
        return None
    if not _rts_pp_monitor_lock.acquire(blocking=False):
        return "Опрос уже выполняется."
    _rts_pp_monitor_lock.release()
    return None


_pp_monitor_overdue_trigger_at: float = 0.0
_pp_monitor_overdue_trigger_lock = threading.Lock()
_pp_monitor_bg_poll_lock = threading.Lock()
_pp_monitor_bg_poll_active = False


def _start_pp_monitor_poll_background(*, force: bool = False) -> None:
    global _pp_monitor_bg_poll_active
    with _pp_monitor_bg_poll_lock:
        if _pp_monitor_bg_poll_active:
            return
        _pp_monitor_bg_poll_active = True

    def _worker() -> None:
        global _pp_monitor_bg_poll_active
        try:
            _run_rts_pp_monitor_poll_once(force=force)
        except Exception:
            _LOG_WEB.exception("RTS PP monitor background poll")
        finally:
            with _pp_monitor_bg_poll_lock:
                _pp_monitor_bg_poll_active = False

    threading.Thread(target=_worker, daemon=True, name="pp-monitor-poll").start()


def _maybe_trigger_overdue_pp_monitor_poll() -> None:
    """Start poll from web when interval elapsed but systemd timer has not fired yet."""
    if not cron_mode_enabled() or not _env_bool("RTS_PP_MONITOR", False):
        return
    settings = load_pp_monitor_settings(_pp_monitor_settings_path())
    if not settings.get("enabled"):
        return
    now_ts = time.time()
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    state = load_cron_state()
    if resolve_poll_running(state, now=now_dt):
        return
    stale_running = stale_running_state(state, now=now_dt)
    finished_raw = str(state.get("finished_at_utc") or "").strip()
    if stale_running or not finished_raw:
        due = True
    else:
        try:
            finished_dt = datetime.fromisoformat(finished_raw.replace("Z", "+00:00"))
        except ValueError:
            return
        interval_sec = pp_monitor_poll_interval_sec()
        due = now_dt >= finished_dt + timedelta(seconds=interval_sec)
    if not due:
        return
    global _pp_monitor_overdue_trigger_at
    with _pp_monitor_overdue_trigger_lock:
        if now_ts - _pp_monitor_overdue_trigger_at < 30:
            return
        _pp_monitor_overdue_trigger_at = now_ts
    _LOG_WEB.info("PP monitor poll overdue — starting from web status poll")
    _start_pp_monitor_poll_background(force=False)


def _start_pp_monitor_backfill_background(*, force: bool = False) -> None:
    def _worker() -> None:
        try:
            _run_rts_pp_monitor_backfill_once(force=force)
        except Exception:
            _LOG_WEB.exception("RTS PP monitor background backfill")

    threading.Thread(target=_worker, daemon=True, name="pp-monitor-backfill").start()


def _run_rts_pp_monitor_poll_once(*, force: bool = False) -> dict[str, Any]:
    if cron_mode_enabled():
        return run_pp_monitor_cron_poll(force=force)
    if not _rts_pp_monitor_lock.acquire(blocking=False):
        return {"ok": False, "skipped": True, "reason": "busy"}
    try:
        try:
            timeout = max(5.0, min(float(os.environ.get("TELEGRAM_TIMEOUT_SEC", "20")), 120.0))
        except ValueError:
            timeout = 20.0
        senders = _pp_monitor_senders(timeout)
        monitor = load_pp_monitor_settings(_pp_monitor_settings_path())
        tg_on = bool(monitor.get("notify_telegram", True))
        max_on = bool(monitor.get("notify_max", True))
        if not tg_on and not max_on:
            senders = []
        elif not tg_on:
            senders = [s for s in senders if s[0] != TELEGRAM_PP_MONITOR_CHANNEL]
        elif not max_on:
            senders = [s for s in senders if s[0] != MAX_PP_MONITOR_CHANNEL]
        return run_rts_pp_monitor_poll(
            _META,
            _eis_analytics_db_path(),
            force=force,
            senders=senders or None,
        )
    finally:
        _rts_pp_monitor_lock.release()


def paths_dict(request: Request | None = None) -> dict[str, str]:
    profile = _rts_profile_for_request(request) if request is not None else ""
    cookie_path = _cookie_path_for_profile(profile) if profile else _COOKIE
    return {
        "cookie": str(cookie_path),
        "cookies_dir": str(cookies_dir(_META)),
        "rts_profile": profile,
        "state": str(_state_path_for_request(request) if request is not None else _STATE),
        "meta": str(_META),
        "web_users": str(_web_users_path()),
        "project_root": str(_project_root),
        "eis_env": str(_EIS_ENV),
        "eis_state": str(_EIS_STATE),
        "rts_preoffers": str(_rts_preoffers_path()),
        "eis_rss_settings": str(_eis_rss_settings_path()),
        "notification_settings": str(_notification_settings_path()),
        "auto_participate_settings": str(_auto_participate_settings_path()),
        "pp_monitor_settings": str(_pp_monitor_settings_path()),
        "customer_monitor_cookie": str(_customer_monitor_cookie_path()),
        "ktru_cache": str(_KTRU_CACHE),
        "country_origin": str(_country_origin_path()),
    }


def _notification_settings_path() -> Path:
    return notification_settings_path(_META)


def _min_purchase_amount_threshold() -> float | None:
    return min_purchase_amount_from_settings(load_notification_settings(_notification_settings_path()))


def _max_purchase_amount_threshold() -> float | None:
    return max_purchase_amount_from_settings(load_notification_settings(_notification_settings_path()))


def _notification_settings_form() -> dict[str, str]:
    return admin_form_from_settings(load_notification_settings(_notification_settings_path()))


def _load_rts_preoffers() -> list[RtsPreoffer]:
    return load_preoffers(_rts_preoffers_path())


def _catalog_ktru_codes() -> list[str]:
    return sorted({o.ktru_code for o in _load_rts_preoffers() if o.enabled and o.ktru_code})


def _pp_participate_eligibility(ktru_code: str) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    in_catalog = code in set(_catalog_ktru_codes())
    in_pp_template = is_ktru_template_cached(_KTRU_CACHE, code) if code else False
    require_catalog = _pp_participate_require_preoffer_catalog()
    if require_catalog:
        can = bool(code and in_catalog and in_pp_template)
    else:
        can = bool(code and in_pp_template)
    reason = ""
    if code and require_catalog and not in_catalog:
        reason = "КТРУ нет в справочнике предложений."
    elif code and not in_pp_template:
        reason = "КТРУ нет в справочнике шаблонов ПП (draft_ktru_dict)."
    return {
        "can_participate": can,
        "participate_reason": reason,
        "in_preoffer_catalog": in_catalog,
        "in_pp_template": in_pp_template,
    }


def _parse_catalog_only_param(raw: str | None, *, default: bool = False) -> bool:
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if not s or s in ("0", "all", "no", "off", "false"):
        return False
    if s in ("1", "catalog", "yes", "on", "true"):
        return True
    return default


def _parse_msk_date_param(raw: str | None) -> date:
    if raw and str(raw).strip():
        try:
            return date.fromisoformat(str(raw).strip())
        except ValueError:
            pass
    from eis_changelog import MSK

    return datetime.now(MSK).date()


def _price_ord_catalog():
    return get_price_ord_catalog()


def _participation_economics_for_line(line: dict[str, Any]) -> dict[str, Any]:
    if not _participation_price_ord_enabled():
        return participation_economics_disabled()
    if line.get("html_status") != "loaded":
        return compute_participation_economics(
            ktru_code="",
            catalog=_price_ord_catalog(),
        )
    return compute_participation_economics(
        ktru_code=str(line.get("ktru_code") or ""),
        delivery_place=str(line.get("delivery_place") or ""),
        region=str(line.get("region") or ""),
        price_per_unit_text=str(line.get("price_per_unit_text") or ""),
        quantity_text=str(line.get("quantity_text") or ""),
        line_sum_text=str(line.get("line_sum_text") or line.get("price_text") or ""),
        catalog=_price_ord_catalog(),
    )


def _apply_profitability_summary_fields(row: dict[str, Any]) -> None:
    econ = row.get("participation_economics") or {}
    if row.get("html_status") != "loaded":
        row["profitability_text"] = "н/д"
        row["profitability_condition"] = None
        row["profitability_class"] = "offer-profit--na"
        row["profitability_is_message"] = False
        return
    if econ.get("available"):
        row["profitability_text"] = str(econ.get("profitability_text") or "—")
        row["profitability_condition"] = econ.get("condition")
        row["profitability_class"] = str(econ.get("profitability_class") or "offer-profit--na")
        row["profitability_is_message"] = False
        return
    row["profitability_text"] = str(
        econ.get("unavailable_message") or econ.get("profitability_text") or "н/д"
    )
    row["profitability_is_message"] = bool(econ.get("unavailable_message"))
    row["profitability_condition"] = None
    row["profitability_class"] = "offer-profit--na"


def _offer_match_payload_for_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    offers = _load_rts_preoffers()
    positions: list[dict[str, Any]] = []
    for line in lines:
        if line.get("html_status") != "loaded" or not line.get("line_key"):
            results = []
        else:
            matching_offers = preoffers_by_ktru(offers, str(line.get("ktru_code") or ""))
            results = rank_offer_matches(line.get("characteristics") or [], matching_offers)
        best = results[0] if results else None
        position = dict(line)
        position["matches"] = [r.as_dict() for r in results]
        position["best_match"] = best.as_dict() if best else None
        position["has_passing_offer"] = bool(best and best.passes)
        position["participation_economics"] = _participation_economics_for_line(line)
        position.update(_pp_participate_eligibility(str(line.get("ktru_code") or "")))
        positions.append(position)
    return positions


def _offer_matching_rows_with_summary(
    *,
    limit: int = 30,
    offset: int = 0,
    ktru_query: str | None = None,
    ktru_queries: list[str] | None = None,
    catalog_ktru_codes: list[str] | None = None,
    pub_from_utc: str | None = None,
    pub_to_utc: str | None = None,
) -> list[dict[str, Any]]:
    lines = offer_matching_ktru_rows(
        _eis_analytics_db_path(),
        limit=limit,
        offset=offset,
        ktru_query=ktru_query,
        ktru_queries=ktru_queries,
        catalog_ktru_codes=catalog_ktru_codes,
        pub_from_utc=pub_from_utc,
        pub_to_utc=pub_to_utc,
    )
    positions = _offer_match_payload_for_lines(lines)
    db = _eis_analytics_db_path()
    synced = sync_offer_matching_lines(
        db,
        meta_dir=_META,
        lines=positions,
        fetch_timeout=20.0,
        run_pp_match=False,
        refresh_outcome=False,
    )
    rows: list[dict[str, Any]] = []
    for position in synced:
        d = dict(position)
        _apply_profitability_summary_fields(d)
        rows.append(d)
    return rows


def _parse_ktru_queries(text: str) -> list[str]:
    return [x for x in re.split(r"[\s,;]+", str(text or "").strip()) if x]


def _parse_preoffer_characteristics_text(text: str) -> list[dict[str, str]]:
    chars: list[dict[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" in line:
            label, value = line.split("=", 1)
        elif ":" in line:
            label, value = line.split(":", 1)
        else:
            label, value = line, ""
        label = label.strip()
        value = value.strip()
        if label or value:
            chars.append({"label": label, "value": value})
    return chars


def _preoffer_characteristics_text(offer: dict[str, Any]) -> str:
    lines: list[str] = []
    for ch in offer.get("characteristics") or []:
        if not isinstance(ch, dict):
            continue
        label = str(ch.get("label") or "").strip()
        value = str(ch.get("value") or "").strip()
        if label or value:
            lines.append(f"{label}={value}")
    return "\n".join(lines)


def _preoffer_empty_form() -> dict[str, Any]:
    return {
        "offer_id": "",
        "title": "",
        "rts_url": "",
        "sku": "",
        "ktru_code": "",
        "enabled": True,
        "characteristics": [],
        "characteristics_text": "",
    }


def _preoffer_form_from_offer(offer: dict[str, Any] | None) -> dict[str, Any]:
    form = normalize_preoffer_payload(offer or _preoffer_empty_form(), existing_offer_id=str((offer or {}).get("offer_id") or ""))
    form["characteristics_text"] = _preoffer_characteristics_text(form)
    return form


def _preoffer_modal_payload(offer: dict[str, Any]) -> dict[str, Any]:
    """Compact offer dict for modal JSON (no multiline characteristics_text)."""
    normalized = normalize_preoffer_payload(
        offer,
        existing_offer_id=str(offer.get("offer_id") or ""),
    )
    return {
        "offer_id": normalized["offer_id"],
        "title": normalized["title"],
        "rts_url": normalized["rts_url"],
        "sku": normalized["sku"],
        "ktru_code": normalized["ktru_code"],
        "enabled": normalized["enabled"],
        "characteristics": normalized["characteristics"],
    }


def _preoffer_draft_from_notice_line(purchase_number: str, line_key: str) -> dict[str, Any] | None:
    rows = offer_matching_ktru_rows(
        _eis_analytics_db_path(),
        limit=1,
        purchase_number=purchase_number,
        line_key=line_key,
    )
    if not rows:
        return None
    line = rows[0]
    chars = []
    for ch in line.get("characteristics") or []:
        if not isinstance(ch, dict):
            continue
        chars.append(
            {
                "match_key": str(ch.get("match_key") or ""),
                "label": str(ch.get("display_label") or ch.get("label") or ""),
                "value": str(ch.get("value_display") or ch.get("value") or ""),
            }
        )
    offer = normalize_preoffer_payload(
        {
            "offer_id": "",
            "title": line.get("position_display_name") or "",
            "rts_url": "",
            "sku": "",
            "ktru_code": line.get("ktru_code") or "",
            "enabled": True,
            "characteristics": chars,
        }
    )
    offer["offer_id"] = ""
    offer["characteristics_text"] = _preoffer_characteristics_text(offer)
    offer["source_purchase_number"] = purchase_number
    offer["source_line_key"] = line_key
    return offer


def read_last_poll() -> dict[str, Any] | None:
    p = _META / "last_poll.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def read_last_eis_poll() -> dict[str, Any] | None:
    p = _META / _LAST_EIS_POLL
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def read_last_invite_bodies() -> tuple[str | None, list[dict[str, Any]], int | None, int | None]:
    """Метка времени, список писем и сводка jqGrid с последней загрузки текстов."""

    def as_int(val: Any) -> int | None:
        if val is None:
            return None
        if isinstance(val, bool):
            return None
        if isinstance(val, int):
            return val
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    p = _META / _LAST_BODIES
    if not p.is_file():
        return None, [], None, None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, [], None, None
    if not isinstance(data, dict):
        return None, [], None, None
    at = data.get("at") if isinstance(data.get("at"), str) else None
    items_raw = data.get("items")
    items = [x for x in items_raw if isinstance(x, dict)] if isinstance(items_raw, list) else []
    return at, items, as_int(data.get("jqgrid_records")), as_int(data.get("parsed_rows"))


def _mark_latest_eis_changelog_rows(rows: list[dict[str, Any]], last_eis: dict[str, Any] | None) -> None:
    """Выделяет строки, добавленные последним опросом ЕИС."""
    if not isinstance(last_eis, dict):
        return
    meta = last_eis.get("meta") if isinstance(last_eis.get("meta"), dict) else {}
    latest_at = str(meta.get("poll_completed_at") or last_eis.get("at") or "").strip()
    if not latest_at:
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        row["is_latest_eis_new"] = str(row.get("received_at_utc") or "").strip() == latest_at


def write_last_invite_bodies(
    items: list[dict[str, Any]],
    *,
    jqgrid_records: int | None,
    parsed_rows: int | None,
) -> None:
    _META.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    if jqgrid_records is not None:
        payload["jqgrid_records"] = jqgrid_records
    if parsed_rows is not None:
        payload["parsed_rows"] = parsed_rows
    (_META / _LAST_BODIES).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def clear_last_invite_bodies() -> None:
    p = _META / _LAST_BODIES
    if p.is_file():
        p.unlink()


def cookie_note(cookie_path: Path) -> str:
    if not cookie_path.is_file():
        return "Файл cookie ещё не создан — вставьте строку и сохраните."
    n = len(cookie_path.read_text(encoding="utf-8").strip())
    return f"Сейчас в файле {n} символов. Новая строка полностью заменит содержимое."


def _env_schedule_poll_flag() -> bool:
    return os.environ.get("EIS_SCHEDULE_POLL", "").strip().lower() in ("1", "true", "yes")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _participation_price_ord_enabled() -> bool:
    return _env_bool("RTS_PARTICIPATION_PRICE_ORD_ENABLED", default=True)


def _pp_participate_require_preoffer_catalog() -> bool:
    return _env_bool("RTS_PP_PARTICIPATE_REQUIRE_PREOFFER_CATALOG", default=True)


def _pp_drafts_enrich_rts_enabled() -> bool:
    return _env_bool("RTS_PP_DRAFTS_ENRICH_RTS", False)


def _pp_publish_enabled() -> bool:
    return pp_publish_enabled()


def _pp_drafts_get_timeout_sec() -> float:
    try:
        return max(1.0, min(float(os.environ.get("RTS_PP_DRAFTS_GET_TIMEOUT_SEC", "5")), 30.0))
    except ValueError:
        return 5.0


def _eis_poll_config_for_schedule(
    *,
    org_region: str,
    document_type_44: str,
    exact_date: str | None,
    ktru_csv: str | None,
) -> EisPollConfig:
    try:
        tout = float(os.environ.get("EIS_SCHEDULE_TIMEOUT_SEC", "120"))
    except ValueError:
        tout = 120.0
    tout = max(30.0, min(tout, 900.0))
    ssl_legacy = os.environ.get("EIS_WEB_SSL_LEGACY", "").lower() in ("1", "true", "yes")
    tls_max = os.environ.get("EIS_WEB_TLS_MAX_12", "").lower() in ("1", "true", "yes")
    legacy_int44 = os.environ.get("EIS_LEGACY_INT44", "").lower() in ("1", "true", "yes")
    service_url = os.environ.get("EIS_GET_DOCS_IP_URL", "").strip() or None
    raw_tg = os.environ.get("EIS_SCHEDULE_TELEGRAM_ENV", "eis").strip().lower()
    telegram_env_choice = raw_tg if raw_tg in ("eis", "rts") else "eis"
    try:
        retention = int(os.environ.get("EIS_CHANGELOG_RETENTION_DAYS", "7"))
    except ValueError:
        retention = 7
    ch_name = Path(os.environ.get("EIS_CHANGELOG_FILE", "eis_ktru_changelog.json")).name
    doc_t = document_type_44.strip() or "epNotificationEZT2020"
    return EisPollConfig(
        env_file=_EIS_ENV,
        state_file=_EIS_STATE,
        meta_parent=_META,
        org_region=org_region,
        document_type_44=doc_t,
        ktru_csv=ktru_csv,
        exact_date=exact_date,
        timeout=tout,
        dry_run=os.environ.get("EIS_SCHEDULE_DRY_RUN", "").lower() in ("1", "true", "yes"),
        require_ezt_xml=os.environ.get("EIS_SCHEDULE_REQUIRE_EZT", "").lower() in ("1", "true", "yes"),
        notify_telegram=os.environ.get("EIS_SCHEDULE_NOTIFY_TELEGRAM", "1").lower() not in ("0", "false", "no"),
        telegram_env=telegram_env_choice,
        no_verify=os.environ.get("EIS_SCHEDULE_NO_VERIFY", "").lower() in ("1", "true", "yes"),
        ssl_legacy=ssl_legacy,
        tls_max_12=tls_max,
        legacy_int44=legacy_int44,
        service_url=service_url,
        env_telegram_ktru_digest=os.environ.get("EIS_SCHEDULE_ENV_DIGEST", "1").lower()
        not in ("0", "false", "no"),
        telegram_incremental_only=True,
        changelog_retention_days=max(1, retention),
        changelog_filename=ch_name or "eis_ktru_changelog.json",
        analytics_db_name=os.environ.get("EIS_ANALYTICS_DB", DEFAULT_DB_NAME),
    )


def _scheduled_eis_poll_once() -> None:
    org = os.environ.get("EIS_SCHEDULE_ORG_REGION", "").strip()
    if not org:
        return
    ktru_s = os.environ.get("EIS_SCHEDULE_KTRU", "").strip() or None
    doc_t = (
        os.environ.get("EIS_SCHEDULE_DOCUMENT_TYPE44", "epNotificationEZT2020").strip() or "epNotificationEZT2020"
    )
    date_s = os.environ.get("EIS_SCHEDULE_EXACT_DATE", "").strip() or None
    cfg = _eis_poll_config_for_schedule(
        org_region=org,
        document_type_44=doc_t,
        exact_date=date_s,
        ktru_csv=ktru_s,
    )
    run_eis_poll(cfg)


def _run_eis_ui_cron_poll() -> None:
    cfg = _eis_poll_config_for_schedule(
        org_region=_eis_ui_cron.org_region.strip() or "35",
        document_type_44=_eis_ui_cron.document_type_44,
        exact_date=_eis_ui_cron.exact_date,
        ktru_csv=_eis_ui_cron.ktru_csv,
    )
    run_eis_poll(cfg)


def _fetch_and_store_notice_html(purchase_number: str, source_url: str, *, timeout: float) -> tuple[int, int]:
    html, status = fetch_notice_common_info_html(source_url, timeout=timeout)
    lines = parse_notice_common_info_html(html, purchase_number=purchase_number, source_url=source_url)
    record_notice_html_page(
        _eis_analytics_db_path(),
        purchase_number=purchase_number,
        source_url=source_url,
        html_text=html,
        lines=lines,
        fetched_at_utc=datetime.now(timezone.utc),
        ok=True,
        http_status=status,
    )
    return len(lines), sum(len(x.characteristics) for x in lines)


MAX_PREOFFER_CHANNEL = "max_preoffer_watch"
TELEGRAM_PREOFFER_CHANNEL = "telegram_preoffer_watch"
RTS_CUSTOMER_PP_CREATE_URL = (
    "https://offers.rts-tender.ru/supplier/price-lists/positions/offer-item/create"
    "?notificationNumber={purchase_number}"
)


def _env_notify_flag(*names: str, default: bool = True) -> bool:
    for name in names:
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw.lower() in ("1", "true", "yes")
    return default


def _notify_profitability_text(row: dict[str, Any]) -> str:
    econ = _participation_economics_for_line({**row, "html_status": "loaded"})
    if econ.get("available"):
        return str(econ.get("profitability_text") or "—")
    return "н/д"


def _auto_participate_created_attempts(
    purchase_number: str,
    line_key: str,
) -> list[dict[str, Any]]:
    db = _eis_analytics_db_path()
    out: list[dict[str, Any]] = []
    for profile in list_known_profiles():
        attempt = get_auto_participate_attempt(
            db,
            purchase_number,
            line_key,
            rts_profile=profile,
        )
        if attempt and attempt.get("status") == "created":
            out.append({**attempt, "rts_profile": profile})
    return out


def _auto_participate_notify_banner(
    attempts: list[dict[str, Any]],
    *,
    links: str,
) -> list[str]:
    if not attempts:
        return []
    profiles = [str(a.get("rts_profile") or "").strip() for a in attempts if str(a.get("rts_profile") or "").strip()]
    if len(profiles) == 1:
        text = f"🟢 Авто-участие ({profiles[0]}): создан черновик"
    elif profiles:
        text = f"🟢 Авто-участие ({', '.join(profiles)}): создан черновик"
    else:
        text = "🟢 Авто-участие: создан черновик"
    bold = html_bold if links == "html" else markdown_bold
    return [bold(text), ""]


def _preoffer_notify_message(
    row: dict[str, Any],
    best: dict[str, Any] | None,
    *,
    links: str,
) -> str:
    """links: 'html' для Telegram, 'markdown' для MAX."""
    link_fn = html_link if links == "html" else markdown_link
    ktru = str(row.get("ktru_code") or "—")
    purchase_number = str(row.get("purchase_number") or "").strip()
    line_key = str(row.get("line_key") or "").strip()
    delivery = str(row.get("delivery_days") or row.get("contract_execution_term") or "").strip() or "—"
    auto_attempts = (
        _auto_participate_created_attempts(purchase_number, line_key)
        if purchase_number and line_key
        else []
    )
    lines = [
        *_auto_participate_notify_banner(auto_attempts, links=links),
        "Новая закупка",
        "",
        f"Наименование: {row.get('position_display_name') or '—'}",
        f"Наименование заказчика: {row.get('customer_name') or '—'}",
        f"Код КТРУ: {ktru}",
        f"Сумма позиции: {row.get('line_sum_text') or '—'}",
        f"Рентабельность, %: {_notify_profitability_text(row)}",
        f"Регион: {row.get('region') or '—'}",
        f"Срок поставки: {delivery}",
    ]
    if purchase_number and line_key:
        compare_url = build_day_summary_compare_url(
            purchase_number=purchase_number,
            line_key=line_key,
            summary_date=summary_date_from_row(row),
        )
        if compare_url:
            lines.append(link_fn(compare_url, "Экономика процедуры"))
    rts_url = str((best or {}).get("rts_url") or "").strip()
    if rts_url:
        lines.append(link_fn(rts_url, "Ссылка на ПП РТС"))
    if purchase_number:
        customer_pp_url = RTS_CUSTOMER_PP_CREATE_URL.format(purchase_number=purchase_number)
        lines.append(link_fn(customer_pp_url, "Ссылка на ПП Заказчика"))
    source_url = str(row.get("source_url") or "").strip()
    if source_url:
        lines.append(link_fn(source_url, "Закупка на ЕИС"))
    for attempt in auto_attempts:
        edit_url = str(attempt.get("edit_url") or "").strip()
        profile = str(attempt.get("rts_profile") or "").strip()
        if edit_url:
            label = f"Черновик ПП ({profile})" if profile else "Черновик ПП"
            lines.append(link_fn(edit_url, label))
    return "\n".join(lines)


def _notify_preoffer_watch_channel(
    *,
    channel: str,
    enabled: bool,
    token: str,
    chat_id: str,
    send_long,
    log_label: str,
    message_links: str,
    offers: list[RtsPreoffer],
    watched_ktru: set[str],
    limit: int,
    timeout: float,
    min_purchase_amount: float | None = None,
    max_purchase_amount: float | None = None,
) -> tuple[int, int]:
    if not enabled:
        return 0, 0
    if not token or not chat_id:
        _LOG_WEB.warning("%s: credentials are not configured", log_label)
        return 0, 0

    sent = 0
    failed = 0
    for row in offer_watch_candidates(_eis_analytics_db_path(), limit=limit, channel=channel):
        ktru = str(row.get("ktru_code") or "").strip()
        if ktru not in watched_ktru:
            continue
        matching_offers = preoffers_by_ktru(offers, ktru)
        if not matching_offers:
            continue
        if not passes_purchase_amount_filter(row, min_purchase_amount, max_purchase_amount):
            continue
        results = rank_offer_matches(row.get("characteristics") or [], matching_offers)
        best = results[0].as_dict() if results else None
        purchase_number = str(row.get("purchase_number") or "").strip()
        line_key = str(row.get("line_key") or "").strip()
        if not purchase_number or not line_key:
            continue
        ratio = 0.0
        matched = 0
        required = 0
        if best:
            required = int(best.get("required") or 0)
            matched = int(best.get("matched") or 0)
            ratio = (matched / required) if required > 0 else 0.0
        message = _preoffer_notify_message(row, best, links=message_links)
        try:
            send_long(message, token, chat_id, timeout)
            record_offer_watch_notification(
                _eis_analytics_db_path(),
                purchase_number=purchase_number,
                line_key=line_key,
                ktru_code=ktru,
                offer_id=str((best or {}).get("offer_id") or ""),
                match_ratio=ratio,
                matched=matched,
                required=required,
                channel=channel,
                status="sent",
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            record_offer_watch_notification(
                _eis_analytics_db_path(),
                purchase_number=purchase_number,
                line_key=line_key,
                ktru_code=ktru,
                offer_id=str((best or {}).get("offer_id") or ""),
                match_ratio=ratio,
                matched=matched,
                required=required,
                channel=channel,
                status="error",
                error_text=str(exc),
            )
            _LOG_WEB.warning("%s: error for %s/%s: %s", log_label, purchase_number, line_key, exc)
    if sent or failed:
        _LOG_WEB.info("%s: sent=%s failed=%s", log_label, sent, failed)
    return sent, failed


def _notify_preoffer_watch_matches() -> tuple[int, int]:
    offers = _load_rts_preoffers()
    watched_ktru = {offer.ktru_code for offer in offers if offer.enabled and offer.ktru_code}
    if not watched_ktru:
        return 0, 0
    try:
        limit = max(1, min(int(os.environ.get("RTS_PREOFFER_NOTIFY_MAX_PER_RUN", "50")), 200))
    except ValueError:
        limit = 50
    try:
        timeout = max(5.0, min(float(os.environ.get("TELEGRAM_TIMEOUT_SEC", "20")), 120.0))
    except ValueError:
        timeout = 20.0

    try:
        tg_token, _ = telegram_creds_from_env_file(_EIS_ENV)
        max_token, _ = max_creds_from_env_file(_EIS_ENV)
    except Exception:
        _LOG_WEB.exception("Preoffer watch: cannot load messenger credentials")
        return 0, 0

    notify_settings = load_notification_settings(_notification_settings_path())
    tg_chat = resolve_telegram_chat_id(_EIS_ENV, str(notify_settings.get("telegram_chat_id") or ""))
    max_chat = resolve_max_chat_id(_EIS_ENV, str(notify_settings.get("max_chat_id") or ""))
    min_purchase_amount = min_purchase_amount_from_settings(notify_settings)
    max_purchase_amount = max_purchase_amount_from_settings(notify_settings)
    tg_enabled = notify_flag_from_settings(
        notify_settings,
        "notify_telegram",
        default=_env_notify_flag("RTS_PREOFFER_TELEGRAM_NOTIFY", "EIS_RSS_TELEGRAM_NOTIFY"),
    )
    max_enabled = notify_flag_from_settings(
        notify_settings,
        "notify_max",
        default=_env_notify_flag("RTS_PREOFFER_MAX_NOTIFY", default=True),
    )

    tg_sent, tg_failed = _notify_preoffer_watch_channel(
        channel=TELEGRAM_PREOFFER_CHANNEL,
        enabled=tg_enabled,
        token=tg_token,
        chat_id=tg_chat,
        send_long=send_telegram_html_long,
        log_label="Telegram preoffer watch",
        message_links="html",
        offers=offers,
        watched_ktru=watched_ktru,
        limit=limit,
        timeout=timeout,
        min_purchase_amount=min_purchase_amount,
        max_purchase_amount=max_purchase_amount,
    )
    max_sent, max_failed = _notify_preoffer_watch_channel(
        channel=MAX_PREOFFER_CHANNEL,
        enabled=max_enabled,
        token=max_token,
        chat_id=max_chat,
        send_long=send_max_markdown_long,
        log_label="MAX preoffer watch",
        message_links="markdown",
        offers=offers,
        watched_ktru=watched_ktru,
        limit=limit,
        timeout=timeout,
        min_purchase_amount=min_purchase_amount,
        max_purchase_amount=max_purchase_amount,
    )
    return tg_sent + max_sent, tg_failed + max_failed


def _draft_source_added_display_msk(db_path: Path, record: dict[str, Any]) -> str:
    """Registry «Дата добавления» for monitor-sourced drafts (competitor sample)."""
    if str(record.get("draft_source") or "") != "monitor":
        return "—"
    oid = str(record.get("offer_price_list_item_id") or "").strip()
    if not oid:
        return "—"
    reg = get_pp_monitor_registry_row(db_path, oid)
    link_raw = str(reg.get("link_date") or "").strip() if reg else ""
    if not link_raw:
        finding = get_pp_monitor_finding(db_path, oid)
        if finding:
            find_item = finding.get("find") if isinstance(finding.get("find"), dict) else {}
            link_raw = str(
                finding.get("link_date") or find_item.get("offerPriceListItemLinkDate") or ""
            ).strip()
    if not link_raw:
        return "—"
    return display_compare_datetime_msk(link_raw)


def _draft_item_name(db_path: Path, record: dict[str, Any]) -> str:
    if str(record.get("draft_source") or "") == "monitor":
        oid = str(record.get("offer_price_list_item_id") or "").strip()
        if oid:
            reg = get_pp_monitor_registry_row(db_path, oid)
            if reg:
                return str(reg.get("name") or "").strip()
        return ""
    pn = str(record.get("purchase_number") or "").strip()
    lk = str(record.get("line_key") or "").strip()
    if pn and lk:
        line = notice_line_for_pp(db_path, pn, lk)
        if line:
            return str(
                line.get("position_display_name") or line.get("item_name") or line.get("name") or ""
            ).strip()
    return ""


def _pp_monitor_draft_as_record(row: dict[str, Any]) -> dict[str, Any]:
    oid = str(row.get("offer_price_list_item_id") or "").strip()
    return {
        "purchase_number": oid,
        "line_key": monitor_draft_line_key(oid),
        "rts_profile": row.get("rts_profile") or "",
        "ktru_code": row.get("ktru_code") or "",
        "rts_draft_id": row.get("rts_draft_id"),
        "protected_id": row.get("protected_id") or "",
        "edit_url": row.get("edit_url") or "",
        "offer_id": row.get("offer_id") or "",
        "status": row.get("status") or "",
        "error_text": row.get("error_text") or "",
        "created_at_utc": row.get("created_at_utc") or "",
        "created_by": row.get("created_by") or "",
        "fill_report_json": row.get("fill_report_json") or "",
        "verify_json": row.get("verify_json") or "",
        "draft_source": "monitor",
        "offer_price_list_item_id": oid,
    }


def _pp_draft_display_row(record: dict[str, Any], *, rts_live: dict[str, Any] | None = None) -> dict[str, Any]:
    draft_id = record.get("rts_draft_id")
    protected_id = str(record.get("protected_id") or "").strip()
    edit_url = draft_edit_url(protected_id, draft_id=int(draft_id) if draft_id else None)
    if not edit_url:
        edit_url = str(record.get("edit_url") or "").strip()
    created_raw = str(record.get("created_at_utc") or "").strip()
    draft_source = str(record.get("draft_source") or "notice").strip() or "notice"
    row = {
        "purchase_number": record.get("purchase_number") or "",
        "line_key": record.get("line_key") or "",
        "ktru_code": record.get("ktru_code") or "",
        "rts_draft_id": draft_id,
        "protected_id": protected_id,
        "edit_url": edit_url,
        "offer_id": record.get("offer_id") or "",
        "status": record.get("status") or "",
        "error_text": record.get("error_text") or "",
        "created_at_utc": created_raw,
        "created_display_msk": format_utc_iso_msk_clean(created_raw) if created_raw else "—",
        "source_added_display_msk": str(record.get("source_added_display_msk") or "—"),
        "created_by": record.get("created_by") or "",
        "fill_report_json": record.get("fill_report_json") or "",
        "fill_summary": "",
        "fill_warnings": [],
        "rts_status": None,
        "price_list_item_id": None,
        "rts_live_ok": False,
        "draft_source": draft_source,
        "offer_price_list_item_id": str(record.get("offer_price_list_item_id") or "").strip(),
        "rts_profile": str(record.get("rts_profile") or "").strip(),
        "item_name": str(record.get("item_name") or "").strip(),
    }
    fill_raw = str(record.get("fill_report_json") or "").strip()
    if fill_raw:
        try:
            fill_data = json.loads(fill_raw)
            if isinstance(fill_data, dict):
                matched = int(fill_data.get("matched") or 0)
                unmatched = fill_data.get("unmatched_notice") or []
                total = matched + len(unmatched) if isinstance(unmatched, list) else matched
                row["fill_summary"] = f"{matched}/{total}" if total else ""
                warnings: list[str] = []
                if isinstance(unmatched, list) and unmatched:
                    warnings.append(f"не сопоставлено: {len(unmatched)}")
                unfilled = fill_data.get("unfilled_required") or []
                if isinstance(unfilled, list) and unfilled:
                    warnings.append(f"обязательные без значения: {len(unfilled)}")
                row["fill_warnings"] = warnings
        except json.JSONDecodeError:
            pass
    if not row["fill_summary"]:
        verify_raw = str(record.get("verify_json") or "").strip()
        if verify_raw:
            try:
                verify_data = json.loads(verify_raw)
                if isinstance(verify_data, dict):
                    ok = int(verify_data.get("ok") or 0)
                    total = int(verify_data.get("total") or 0)
                    if total:
                        row["fill_summary"] = f"{ok}/{total}"
                    issues = int(verify_data.get("issues") or 0)
                    if issues:
                        row["fill_warnings"].append(f"расхождений: {issues}")
            except json.JSONDecodeError:
                pass
    if isinstance(rts_live, dict):
        row["rts_live_ok"] = True
        row["rts_status"] = rts_live.get("status")
        row["price_list_item_id"] = rts_live.get("priceListItemId")
        if not row["ktru_code"]:
            row["ktru_code"] = rts_live.get("ktruCode") or ""
    return row


_PP_DRAFTS_PER_PAGE = 30


def _load_pp_drafts_for_page(
    request: Request,
    *,
    page: int = 1,
    enrich_rts: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    profiles, admin_view = _pp_draft_profiles_for_request(request)
    db_path = _eis_analytics_db_path()
    notice_rows: list[dict[str, Any]] = []
    monitor_rows: list[dict[str, Any]] = []
    for profile in profiles:
        notice_rows.extend(list_rts_pp_drafts(db_path, rts_profile=profile))
        monitor_rows.extend(list_pp_monitor_drafts(db_path, rts_profile=profile))
    merged: list[dict[str, Any]] = []
    for record in notice_rows:
        item = dict(record)
        item.setdefault("draft_source", "notice")
        merged.append(item)
    for record in monitor_rows:
        merged.append(_pp_monitor_draft_as_record(record))
    merged.sort(key=lambda row: str(row.get("created_at_utc") or ""), reverse=True)
    total_rows = len(merged)
    per_page = _PP_DRAFTS_PER_PAGE
    total_pages = max(1, (total_rows + per_page - 1) // per_page)
    page_num = max(1, min(int(page), total_pages))
    offset = (page_num - 1) * per_page
    page_records = merged[offset : offset + per_page]
    meta: dict[str, Any] = {
        "cookie_ok": False,
        "cookie_message": "",
        "rts_profile": _rts_profile_for_request(request),
        "admin_view": admin_view,
        "view_profiles": profiles,
        "pagination": {
            "page": page_num,
            "per_page": per_page,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "has_prev": page_num > 1,
            "has_next": page_num < total_pages,
            "prev_page": max(1, page_num - 1),
            "next_page": min(total_pages, page_num + 1),
            "first_row": offset + 1 if total_rows else 0,
            "last_row": min(offset + per_page, total_rows),
        },
    }
    cookies_by_profile: dict[str, str] = {}
    cookie: str | None = None
    if enrich_rts:
        if admin_view:
            for profile in profiles:
                try:
                    cp = _cookie_path_for_profile(profile)
                    c = load_offers_cookie(cp)
                    validate_cookie_session(c, probe=False)
                    cookies_by_profile[profile] = c
                except (RtsPpApiError, Exception):
                    continue
            meta["cookie_ok"] = bool(cookies_by_profile)
            if not cookies_by_profile:
                meta["cookie_message"] = "Нет активных cookie операторов для проверки статуса в РТС."
        else:
            cookie_path = _cookie_path_for_request(request)
            try:
                cookie = load_offers_cookie(cookie_path)
                validate_cookie_session(cookie, probe=False)
                meta["cookie_ok"] = True
            except RtsPpApiError as exc:
                meta["cookie_message"] = str(exc)
            except Exception as exc:
                meta["cookie_message"] = str(exc)[:300]
    display: list[dict[str, Any]] = []
    rts_enrich_failed = False
    for record in page_records:
        live: dict[str, Any] | None = None
        draft_id = record.get("rts_draft_id")
        row_profile = str(record.get("rts_profile") or "").strip()
        row_cookie = cookies_by_profile.get(row_profile) if admin_view else cookie
        if row_cookie and draft_id and not rts_enrich_failed:
            try:
                live = get_draft(row_cookie, int(draft_id), timeout=_pp_drafts_get_timeout_sec())
            except RtsPpApiError as exc:
                live = None
                if exc.reason == "api_error" and "Сеть РТС" in str(exc):
                    rts_enrich_failed = True
                    meta["cookie_message"] = "РТС API недоступен — статусы не обновлены для страницы."
            except Exception:
                live = None
        enriched = dict(record)
        enriched["source_added_display_msk"] = _draft_source_added_display_msk(db_path, record)
        if not str(enriched.get("item_name") or "").strip():
            enriched["item_name"] = _draft_item_name(db_path, enriched)
        display.append(_pp_draft_display_row(enriched, rts_live=live))
    return display, meta


def _verify_summary_from_json(verify_raw: str) -> dict[str, int] | None:
    raw = str(verify_raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "total": int(data.get("total") or 0),
        "ok": int(data.get("ok") or 0),
        "issues": int(data.get("issues") or 0),
    }


def _rts_pp_draft_api_payload(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record or record.get("status") != "created":
        return None
    draft_id = record.get("rts_draft_id")
    edit_url = draft_edit_url(
        str(record.get("protected_id") or ""),
        draft_id=int(draft_id) if draft_id else None,
    ) or str(record.get("edit_url") or "")
    payload: dict[str, Any] = {
        "rts_draft_id": draft_id,
        "protected_id": record.get("protected_id") or "",
        "edit_url": edit_url,
        "offer_id": record.get("offer_id") or "",
        "created_at_utc": record.get("created_at_utc"),
    }
    summary = _verify_summary_from_json(str(record.get("verify_json") or ""))
    if summary:
        payload["verify_summary"] = summary
    return payload


def _draft_verify_for_line(
    *,
    purchase_number: str,
    line_key: str,
    rts_profile: str,
    cookie_path: Path,
    draft_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pn = purchase_number.strip()
    lk = line_key.strip()
    profile = rts_profile_for_web_user(rts_profile)
    db = _eis_analytics_db_path()
    record = get_rts_pp_draft(db, pn, lk, rts_profile=profile)
    if not record or record.get("status") != "created" or not record.get("rts_draft_id"):
        raise HTTPException(status_code=404, detail="Черновик ПП для позиции не найден.")

    line = notice_line_for_pp(db, pn, lk)
    if not line:
        raise HTTPException(status_code=404, detail="Позиция не найдена в базе.")
    if line.get("html_status") != "loaded":
        raise HTTPException(status_code=400, detail="Карточка ЕИС не загружена.")

    ktru_code = str(line.get("ktru_code") or record.get("ktru_code") or "").strip()
    if not ktru_code:
        raise HTTPException(status_code=400, detail="У позиции нет кода КТРУ.")

    notice_chars = enrich_notice_characteristics(
        db,
        purchase_number=pn,
        ktru_code=ktru_code,
        html_chars=line.get("characteristics") or [],
        history_path=_eis_history_db_path(),
    )
    ktru_item = load_cached_template(_KTRU_CACHE, ktru_code, ttl_sec=0)

    item = draft_item
    if item is None:
        try:
            cookie = load_offers_cookie(cookie_path)
            validate_cookie_session(cookie)
            item = get_draft(cookie, int(record["rts_draft_id"]), timeout=20)
        except RtsPpApiError as exc:
            if exc.reason in ("no_cookie", "session_expired"):
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return compare_notice_to_draft(
        notice_chars,
        item,
        ktru_item=ktru_item,
    )


def _rts_pp_draft_from_notice(
    *,
    purchase_number: str,
    line_key: str,
    rts_profile: str,
    cookie_path: Path,
    created_by: str = "",
) -> dict[str, Any]:
    result = create_pp_draft_from_notice(
        analytics_db=_eis_analytics_db_path(),
        history_db=_eis_history_db_path(),
        meta_dir=_META,
        preoffers_path=_rts_preoffers_path(),
        cookie_path=cookie_path,
        purchase_number=purchase_number,
        line_key=line_key,
        rts_profile=rts_profile,
        created_by=created_by,
        price_ord_enabled=_participation_price_ord_enabled(),
    )
    if not result.ok and not result.already_existed:
        raise HTTPException(status_code=result.http_status, detail=result.error)
    return result.as_api_dict()


def _rts_pp_draft_from_monitor(
    *,
    offer_price_list_item_id: str,
    rts_profile: str,
    cookie_path: Path,
    created_by: str = "",
) -> dict[str, Any]:
    result = create_pp_draft_from_monitor(
        analytics_db=_eis_analytics_db_path(),
        meta_dir=_META,
        preoffers_path=_rts_preoffers_path(),
        cookie_path=cookie_path,
        offer_price_list_item_id=offer_price_list_item_id,
        rts_profile=rts_profile,
        created_by=created_by,
        price_ord_enabled=_participation_price_ord_enabled(),
    )
    if not result.ok and not result.already_existed:
        raise HTTPException(status_code=result.http_status, detail=result.error)
    return result.as_api_dict()


def _notify_auto_cookie_alert(_profile: str, message: str) -> None:
    from notify import send_telegram

    try:
        tg_token, tg_chat = telegram_creds_from_env_file(_EIS_ENV)
    except Exception:
        _LOG_WEB.exception("Auto participate: cannot load Telegram credentials")
        return
    if not tg_token or not tg_chat:
        return
    try:
        timeout = max(5.0, min(float(os.environ.get("TELEGRAM_TIMEOUT_SEC", "20")), 120.0))
    except ValueError:
        timeout = 20.0
    send_telegram(message, tg_token, tg_chat, timeout)


def _run_auto_participate_after_html() -> None:
    summary = run_auto_participate_after_html(
        analytics_db=_eis_analytics_db_path(),
        history_db=_eis_history_db_path(),
        meta_dir=_META,
        project_root=_project_root,
        preoffers_path=_rts_preoffers_path(),
        price_ord_enabled=_participation_price_ord_enabled(),
        notify_cookie_alert=_notify_auto_cookie_alert,
    )
    if (
        summary.profiles_checked
        or summary.created
        or summary.errors
        or summary.cookie_expired
    ):
        _LOG_WEB.info(
            "Auto participate: profiles=%s created=%s existed=%s errors=%s cookie_expired=%s "
            "skipped_rules=%s skipped_blocked=%s",
            summary.profiles_checked,
            summary.created,
            summary.already_existed,
            summary.errors,
            summary.cookie_expired,
            summary.skipped_rules,
            summary.skipped_blocked,
        )


def _auto_fetch_notice_html_after_rss_poll() -> None:
    global _eis_html_db_backoff_until
    if not _env_bool("EIS_HTML_AUTO_FETCH", True):
        return
    if time.time() < _eis_html_db_backoff_until:
        return
    if not _eis_html_auto_fetch_lock.acquire(blocking=False):
        _LOG_WEB.info("ЕИС HTML auto-fetch: previous run is still active")
        return
    try:
        try:
            limit = max(1, min(int(os.environ.get("EIS_HTML_AUTO_FETCH_MAX_PER_POLL", "10")), 100))
        except ValueError:
            limit = 10
        try:
            timeout = max(5.0, min(float(os.environ.get("EIS_HTML_FETCH_TIMEOUT_SEC", "30")), 120.0))
        except ValueError:
            timeout = 30.0
        try:
            delay = max(0.0, min(float(os.environ.get("EIS_HTML_AUTO_FETCH_DELAY_SEC", "1.0")), 10.0))
        except ValueError:
            delay = 1.0
        retry_errors = _env_bool("EIS_HTML_AUTO_FETCH_RETRY_ERRORS", False)
        try:
            candidates = notice_html_fetch_candidates(_eis_analytics_db_path(), limit=limit, retry_errors=retry_errors)
        except sqlite3.OperationalError as exc:
            _eis_html_db_backoff_until = time.time() + max(
                30.0,
                min(float(os.environ.get("EIS_HTML_DB_BACKOFF_SEC", "120")), 600.0),
            )
            _LOG_WEB.warning(
                "ЕИС HTML auto-fetch: БД недоступна (%s), пауза до %s",
                exc,
                datetime.fromtimestamp(_eis_html_db_backoff_until, tz=timezone.utc).isoformat(),
            )
            return
        _eis_html_db_backoff_until = 0.0
        if not candidates:
            return
        fetched = 0
        failed = 0
        for idx, row in enumerate(candidates):
            pn = str(row.get("purchase_number") or "").strip()
            url_s = str(row.get("source_url") or "").strip()
            if not pn or not url_s:
                continue
            if idx > 0 and delay > 0:
                time.sleep(delay)
            try:
                lines_count, chars_count = _fetch_and_store_notice_html(pn, url_s, timeout=timeout)
                fetched += 1
                _LOG_WEB.info(
                    "ЕИС HTML auto-fetch: карточка %s загружена, позиций=%s характеристик=%s",
                    pn,
                    lines_count,
                    chars_count,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                record_notice_html_page(
                    _eis_analytics_db_path(),
                    purchase_number=pn,
                    source_url=url_s,
                    html_text="",
                    lines=[],
                    fetched_at_utc=datetime.now(timezone.utc),
                    ok=False,
                    http_status=None,
                    error_text=str(exc)[:2000],
                )
                _LOG_WEB.warning("ЕИС HTML auto-fetch: ошибка карточки %s: %s", pn, exc)
        _LOG_WEB.info("ЕИС HTML auto-fetch: done fetched=%s failed=%s candidates=%s", fetched, failed, len(candidates))
        try:
            _run_auto_participate_after_html()
        except Exception:
            _LOG_WEB.exception("Auto participate after HTML auto-fetch")
        try:
            _notify_preoffer_watch_matches()
        except Exception:
            _LOG_WEB.exception("Telegram preoffer watch after HTML auto-fetch")
    finally:
        _eis_html_auto_fetch_lock.release()


def _run_eis_rss_ui_cron_poll() -> None:
    cfg = EisRssConfig(
        rss_url=_eis_rss_cron.rss_url.strip() or DEFAULT_RSS_URL,
        timeout=float(max(10, int(os.environ.get("EIS_RSS_TIMEOUT_SEC", "30")))),
        query_label=_eis_rss_cron.query_label.strip() or DEFAULT_QUERY_LABEL,
    )
    res = run_eis_rss_poll(cfg, _eis_analytics_db_path())
    if res.ok:
        _LOG_WEB.info("ЕИС RSS: poll ok total=%s new=%s run_id=%s", res.total_items, res.new_items, res.run_id)
        try:
            _auto_fetch_notice_html_after_rss_poll()
        except Exception:
            _LOG_WEB.exception("ЕИС HTML auto-fetch after RSS poll")
    else:
        _LOG_WEB.warning("ЕИС RSS: poll errors=%s", "; ".join(res.errors))


async def _eis_supervisor_loop() -> None:
    global _eis_polling_now, _eis_next_poll_unix
    await asyncio.sleep(1)
    warned_no_region = False
    try:
        while True:
            try:
                if _eis_ui_cron.enabled:
                    interval = max(30, int(_eis_ui_cron.interval_sec))
                    _eis_next_poll_unix = None
                    _eis_polling_now = True
                    try:
                        await asyncio.to_thread(_run_eis_ui_cron_poll)
                    except Exception:
                        _LOG_WEB.exception("ЕИС: регулярный опрос (кнопка веб‑консоли)")
                    finally:
                        _eis_polling_now = False
                    if not _eis_ui_cron.enabled:
                        _eis_next_poll_unix = None
                        continue
                    deadline = time.time() + interval
                    _eis_next_poll_unix = deadline
                    while time.time() < deadline and _eis_ui_cron.enabled:
                        rem = deadline - time.time()
                        await asyncio.sleep(min(1.0, max(0.05, rem)))
                    continue

                if _env_schedule_poll_flag():
                    interval = max(30, int(os.environ.get("EIS_SCHEDULE_INTERVAL_SEC", "120")))
                    org = os.environ.get("EIS_SCHEDULE_ORG_REGION", "").strip()
                    if not org:
                        if not warned_no_region:
                            _LOG_WEB.warning(
                                "EIS_SCHEDULE_POLL включён — укажите EIS_SCHEDULE_ORG_REGION (регион заказчика)."
                            )
                            warned_no_region = True
                        _eis_next_poll_unix = None
                        await asyncio.sleep(min(30, interval))
                        continue
                    warned_no_region = False
                    _eis_next_poll_unix = None
                    _eis_polling_now = True
                    try:
                        await asyncio.to_thread(_scheduled_eis_poll_once)
                    except Exception:
                        _LOG_WEB.exception("Плановый опрос ЕИС")
                    finally:
                        _eis_polling_now = False
                    if _eis_ui_cron.enabled:
                        continue
                    deadline = time.time() + interval
                    _eis_next_poll_unix = deadline
                    while time.time() < deadline:
                        if _eis_ui_cron.enabled:
                            break
                        rem = deadline - time.time()
                        await asyncio.sleep(min(1.0, max(0.05, rem)))
                    continue

                _eis_next_poll_unix = None
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG_WEB.exception("EIS supervisor")
                await asyncio.sleep(5)
    finally:
        _eis_next_poll_unix = None
        _eis_polling_now = False


async def _eis_rss_supervisor_loop() -> None:
    global _eis_rss_polling_now, _eis_rss_next_poll_unix
    await asyncio.sleep(1)
    try:
        while True:
            try:
                if not _eis_rss_cron.enabled:
                    _eis_rss_next_poll_unix = None
                    await asyncio.sleep(0.5)
                    continue
                interval = max(30, int(_eis_rss_cron.interval_sec))
                _eis_rss_next_poll_unix = None
                _eis_rss_polling_now = True
                try:
                    await asyncio.to_thread(_run_eis_rss_ui_cron_poll)
                except Exception:
                    _LOG_WEB.exception("ЕИС RSS: регулярный опрос")
                finally:
                    _eis_rss_polling_now = False
                if not _eis_rss_cron.enabled:
                    _eis_rss_next_poll_unix = None
                    continue
                deadline = time.time() + interval
                _eis_rss_next_poll_unix = deadline
                while time.time() < deadline and _eis_rss_cron.enabled:
                    rem = deadline - time.time()
                    await asyncio.sleep(min(1.0, max(0.05, rem)))
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG_WEB.exception("EIS RSS supervisor")
                await asyncio.sleep(5)
    finally:
        _eis_rss_next_poll_unix = None
        _eis_rss_polling_now = False


async def _rts_pp_monitor_supervisor_loop() -> None:
    global _rts_pp_monitor_polling_now, _rts_pp_monitor_next_poll_unix
    await asyncio.sleep(5)
    try:
        while True:
            try:
                if cron_mode_enabled() or not _env_bool("RTS_PP_MONITOR_SUPERVISOR", True):
                    _rts_pp_monitor_next_poll_unix = None
                    _rts_pp_monitor_polling_now = False
                    await asyncio.sleep(5.0)
                    continue
                if not _env_bool("RTS_PP_MONITOR", False):
                    _rts_pp_monitor_next_poll_unix = None
                    await asyncio.sleep(5.0)
                    continue
                settings = load_pp_monitor_settings(_pp_monitor_settings_path())
                if not settings.get("enabled"):
                    _rts_pp_monitor_next_poll_unix = None
                    await asyncio.sleep(5.0)
                    continue
                interval_min = settings.get("poll_interval_min") or 20
                try:
                    env_interval = int(os.environ.get("RTS_PP_MONITOR_INTERVAL_MIN", "0"))
                    if env_interval > 0:
                        interval_min = env_interval
                except ValueError:
                    pass
                interval_sec = max(
                    MIN_POLL_INTERVAL_MIN * 60,
                    min(int(interval_min) * 60, MAX_POLL_INTERVAL_MIN * 60),
                )
                _rts_pp_monitor_next_poll_unix = None
                _rts_pp_monitor_polling_now = True
                try:
                    await asyncio.to_thread(_run_rts_pp_monitor_poll_once)
                except Exception:
                    _LOG_WEB.exception("RTS PP monitor supervisor")
                finally:
                    _rts_pp_monitor_polling_now = False
                if not _env_bool("RTS_PP_MONITOR", False):
                    _rts_pp_monitor_next_poll_unix = None
                    continue
                settings = load_pp_monitor_settings(_pp_monitor_settings_path())
                if not settings.get("enabled"):
                    _rts_pp_monitor_next_poll_unix = None
                    continue
                deadline = time.time() + interval_sec
                _rts_pp_monitor_next_poll_unix = deadline
                while time.time() < deadline:
                    if not _env_bool("RTS_PP_MONITOR", False):
                        break
                    settings = load_pp_monitor_settings(_pp_monitor_settings_path())
                    if not settings.get("enabled"):
                        break
                    rem = deadline - time.time()
                    await asyncio.sleep(min(1.0, max(0.05, rem)))
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG_WEB.exception("RTS PP monitor supervisor")
                await asyncio.sleep(30)
    finally:
        _rts_pp_monitor_next_poll_unix = None
        _rts_pp_monitor_polling_now = False


async def _eis_html_auto_fetch_supervisor_loop() -> None:
    global _eis_html_supervisor_sleep_sec
    await asyncio.sleep(2)
    try:
        while True:
            try:
                if not _env_bool("EIS_HTML_AUTO_FETCH", True):
                    await asyncio.sleep(2.0)
                    continue
                await asyncio.to_thread(_auto_fetch_notice_html_after_rss_poll)
                try:
                    _eis_html_supervisor_sleep_sec = max(
                        5, min(int(os.environ.get("EIS_HTML_AUTO_FETCH_SCAN_SEC", "5")), 300)
                    )
                except ValueError:
                    _eis_html_supervisor_sleep_sec = 5
                await asyncio.sleep(_eis_html_supervisor_sleep_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                health_monitor.record_html_supervisor_error()
                _LOG_WEB.exception("ЕИС HTML auto-fetch supervisor")
                _eis_html_supervisor_sleep_sec = min(max(_eis_html_supervisor_sleep_sec * 2, 10), 300)
                await asyncio.sleep(_eis_html_supervisor_sleep_sec)
    except asyncio.CancelledError:
        raise


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    global _eis_supervisor_task, _eis_rss_supervisor_task, _eis_html_supervisor_task, _rts_pp_monitor_supervisor_task
    app.state.analytics_db_path = str(_eis_analytics_db_path())
    _health_stop = asyncio.Event()
    _health_heartbeat_task = asyncio.create_task(heartbeat_loop(_health_stop))
    _eis_supervisor_task = asyncio.create_task(_eis_supervisor_loop())
    _eis_rss_supervisor_task = asyncio.create_task(_eis_rss_supervisor_loop())
    _eis_html_supervisor_task = asyncio.create_task(_eis_html_auto_fetch_supervisor_loop())
    _rts_pp_monitor_supervisor_task = asyncio.create_task(_rts_pp_monitor_supervisor_loop())
    yield
    _health_stop.set()
    _health_heartbeat_task.cancel()
    try:
        await _health_heartbeat_task
    except asyncio.CancelledError:
        pass
    if _eis_supervisor_task is not None:
        _eis_supervisor_task.cancel()
        try:
            await _eis_supervisor_task
        except asyncio.CancelledError:
            pass
        _eis_supervisor_task = None
    if _eis_rss_supervisor_task is not None:
        _eis_rss_supervisor_task.cancel()
        try:
            await _eis_rss_supervisor_task
        except asyncio.CancelledError:
            pass
        _eis_rss_supervisor_task = None
    if _eis_html_supervisor_task is not None:
        _eis_html_supervisor_task.cancel()
        try:
            await _eis_html_supervisor_task
        except asyncio.CancelledError:
            pass
        _eis_html_supervisor_task = None
    if _rts_pp_monitor_supervisor_task is not None:
        _rts_pp_monitor_supervisor_task.cancel()
        try:
            await _rts_pp_monitor_supervisor_task
        except asyncio.CancelledError:
            pass
        _rts_pp_monitor_supervisor_task = None


def dashboard_context(request: Request) -> dict[str, Any]:
    state_path = _state_path_for_request(request)
    cookie_path = _cookie_path_for_request(request)
    seen, st = load_state(state_path)
    last = read_last_poll()
    bodies_at, invite_bodies, invite_jqgrid_records, invite_parsed_rows = read_last_invite_bodies()
    last_eis = read_last_eis_poll()
    eis_seen = load_eis_state(_EIS_STATE)
    sched_on = os.environ.get("EIS_SCHEDULE_POLL", "").strip().lower() in ("1", "true", "yes")
    ch_rows = read_changelog_rows_for_dashboard(_eis_changelog_path())
    enrich_changelog_rows_from_customer_characteristics(ch_rows, last_eis)
    _mark_latest_eis_changelog_rows(ch_rows, last_eis)
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Приглашения — RTS",
        "active": "dash",
        "paths": paths_dict(request),
        "cookie_exists": cookie_path.is_file(),
        "rts_profile": _rts_profile_for_request(request),
        "state_count": len(seen),
        "state_updated": st.get("updated_at") if isinstance(st, dict) else None,
        "last_poll": last,
        "form_days": 90,
        "form_rows": 50,
        "form_enrich": False,
        "form_no_notify": False,
        "form_dry_run": False,
        "form_print_all": False,
        "form_fetch_bodies": False,
        "form_lot_items": False,
        "invite_bodies_at": bodies_at,
        "invite_bodies": invite_bodies,
        "invite_jqgrid_records": invite_jqgrid_records,
        "invite_parsed_rows": invite_parsed_rows,
        "last_eis_poll": last_eis,
        "eis_changelog_rows": ch_rows,
        "eis_changelog_path": str(_eis_changelog_path()),
        "eis_analytics_db_path": str(_eis_analytics_db_path()),
        "eis_schedule_enabled": sched_on,
        "eis_schedule_interval_sec": int(os.environ.get("EIS_SCHEDULE_INTERVAL_SEC", "120")),
        "eis_ui_cron_enabled": _eis_ui_cron.enabled,
        "eis_ui_cron_interval_sec": int(_eis_ui_cron.interval_sec),
        "eis_state_count": len(eis_seen),
        "eis_env_exists": _EIS_ENV.is_file(),
        "eis_org_region_default": "",
        "eis_document_type_default": "epNotificationEZT2020",
        "eis_ktru_default": "",
        "eis_date_default": "",
        "form_eis_dry_run": False,
        "form_eis_require_ezt": False,
        "form_eis_notify_tg": False,
        "form_eis_no_verify": False,
    }
    ctx.update(web_auth_ui_context(request))
    return ctx


app = FastAPI(title="RTS Tender console", docs_url=None, redoc_url=None, lifespan=_app_lifespan)
app.include_router(health_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get(
        "RTS_WEB_SESSION_SECRET",
        "dev-change-RTS_WEB_SESSION_SECRET-for-public",
    ),
    session_cookie="rts_console",
    https_only=os.environ.get("RTS_WEB_HTTPS_ONLY", "").lower() in ("1", "true", "yes"),
    same_site=os.environ.get("RTS_WEB_SESSION_SAMESITE", "lax"),
)
app.mount("/static", StaticFiles(directory=str(_app_dir / "static")), name="static")


@app.middleware("http")
async def _health_request_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    return await request_timing_middleware(request, call_next)


@app.get("/login", name="page_login")
def page_login(
    request: Request,
    next: Annotated[str, Query(alias="next")] = "/analytics",
) -> Any:
    next_path = safe_next_path(next)
    if auth_enabled() and session_username(request):
        return RedirectResponse(next_path, status_code=303)
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Вход — RTS",
        "next_path": next_path,
        "login_error": request.session.pop("login_error", None),
    }
    return _templates.TemplateResponse("login.html", ctx)


@app.post("/login", name="action_login")
def action_login(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/analytics",
) -> RedirectResponse:
    next_path = safe_next_path(next)
    if not auth_enabled():
        return RedirectResponse(next_path, status_code=303)
    if verify_credentials(username, password):
        login_user(request, username.strip())
        return RedirectResponse(next_path, status_code=303)
    request.session["login_error"] = "Неверный логин или пароль"
    return RedirectResponse(
        f"/login?next={quote_plus(next_path)}"
        if next_path not in ("/", "/analytics")
        else "/login",
        status_code=303,
    )


@app.post("/logout", name="action_logout")
def action_logout(request: Request) -> RedirectResponse:
    logout_user(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/", name="page_home")
def page_home(request: Request, _: Auth) -> RedirectResponse:
    return RedirectResponse(request.url_for("page_analytics"), status_code=303)


@app.get("/dashboard", name="page_dashboard")
def page_dashboard(request: Request, _: Auth) -> Any:
    ctx = dashboard_context(request)
    # flash + last poll log из сессии (после POST)
    for key in ("flash_ok", "flash_err", "poll_stdout", "poll_stderr", "eis_flash_ok", "eis_flash_err"):
        if key in request.session:
            ctx[key] = request.session.pop(key)
        else:
            ctx[key] = None
    ctx["form_days"] = int(request.session.pop("form_days", 90))
    ctx["form_rows"] = int(request.session.pop("form_rows", 50))
    ctx["form_enrich"] = bool(request.session.pop("form_enrich", False))
    ctx["form_no_notify"] = bool(request.session.pop("form_no_notify", False))
    ctx["form_dry_run"] = bool(request.session.pop("form_dry_run", False))
    ctx["form_print_all"] = bool(request.session.pop("form_print_all", False))
    ctx["form_fetch_bodies"] = bool(request.session.pop("form_fetch_bodies", False))
    ctx["form_lot_items"] = bool(request.session.pop("form_lot_items", False))
    ctx["eis_org_region_default"] = str(request.session.pop("eis_form_org_region", ""))
    ctx["eis_document_type_default"] = str(request.session.pop("eis_form_document_type", "epNotificationEZT2020"))
    ctx["eis_ktru_default"] = str(request.session.pop("eis_form_ktru", ""))
    ctx["eis_date_default"] = str(request.session.pop("eis_form_date", ""))
    ctx["form_eis_dry_run"] = bool(request.session.pop("form_eis_dry_run", False))
    ctx["form_eis_require_ezt"] = bool(request.session.pop("form_eis_require_ezt", False))
    ctx["form_eis_notify_tg"] = bool(request.session.pop("form_eis_notify_tg", False))
    ctx["form_eis_no_verify"] = bool(request.session.pop("form_eis_no_verify", False))
    return _templates.TemplateResponse("dashboard.html", ctx)


@app.get("/analytics", name="page_analytics")
def page_analytics(request: Request, _: Auth) -> Any:
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Извещения о закупке — RTS",
        "active": "analytics",
        "paths": paths_dict(),
        "eis_rss_cron_enabled": _eis_rss_cron.enabled,
        "eis_rss_polling_now": _eis_rss_polling_now,
        "eis_rss_interval_sec": int(_eis_rss_cron.interval_sec),
        "eis_rss_url": _eis_rss_cron.rss_url,
        "eis_rss_query_label": _eis_rss_cron.query_label,
        "eis_rss_last_run": last_rss_poll_run(_eis_analytics_db_path()),
        "eis_rss_compare_rows": rss_vs_getdocs_recent(_eis_analytics_db_path(), limit=30),
        "rss_flash_ok": request.session.pop("rss_flash_ok", None),
        "rss_flash_err": request.session.pop("rss_flash_err", None),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("analytics.html", ctx)


@app.get("/ktru-analytics", name="page_ktru_analytics")
def page_ktru_analytics(
    request: Request,
    _: Auth,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    ktru: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    tab: Annotated[str | None, Query()] = None,
) -> Any:
    hist_path = _eis_history_db_path()
    history_coverage = get_history_coverage_period(hist_path)
    if history_coverage:
        period_from, period_to = history_coverage
    else:
        period_from, period_to = HISTORY_PERIOD_FROM, HISTORY_PERIOD_TO
        job = get_latest_backfill_job(hist_path)
        if job:
            try:
                period_from = date.fromisoformat(str(job["date_from"]))
                period_to = date.fromisoformat(str(job["date_to"]))
            except ValueError:
                pass
    if (date_from or "").strip():
        try:
            period_from = parse_date_iso(date_from.strip())
        except ValueError:
            pass
    if (date_to or "").strip():
        try:
            period_to = parse_date_iso(date_to.strip())
        except ValueError:
            pass
    if period_from > period_to:
        period_from, period_to = period_to, period_from
    active_tab = "summary" if (tab or "").strip().lower() == "summary" else "analysis"
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Аналитика КТРУ — RTS",
        "active": "ktru_analytics",
        "paths": paths_dict(),
        "period_from": period_from,
        "period_to": period_to,
        "selected_ktru": (ktru or "").strip(),
        "ktru_query": (q or "").strip(),
        "history_coverage": history_coverage,
        "active_tab": active_tab,
        "analytics": None,
        "dashboard": None,
    }
    if active_tab == "summary":
        ctx["dashboard"] = history_dashboard_summary(
            hist_path,
            date_from=period_from,
            date_to=period_to,
            ktru_code=ktru,
            ktru_query=q,
        )
    else:
        ctx["analytics"] = history_analytics_v2_summary(
            hist_path,
            date_from=period_from,
            date_to=period_to,
            ktru_code=ktru,
            ktru_query=q,
        )
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("ktru_analytics.html", ctx)


@app.get("/ktru-analytics-v2", name="page_ktru_analytics_v2")
def page_ktru_analytics_v2_redirect(request: Request, _: Auth) -> RedirectResponse:
    target = request.url_for("page_ktru_analytics")
    if request.url.query:
        return RedirectResponse(f"{target}?{request.url.query}", status_code=302)
    return RedirectResponse(target, status_code=302)


@app.get("/shelf-purchases", name="page_shelf_purchases")
def page_shelf_purchases(request: Request, _: Auth) -> Any:
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Закупки ч.12 ст.93 — RTS",
        "active": "shelf_purchases",
        "paths": paths_dict(),
        "rows": shelf_purchase_rows(_eis_analytics_db_path(), limit=30),
        "html_flash_ok": request.session.pop("html_flash_ok", None),
        "html_flash_err": request.session.pop("html_flash_err", None),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("shelf_purchases.html", ctx)


def _offer_matching_list_context(
    request: Request,
    *,
    page_route: str,
    page_title: str,
    active: str,
    page_heading: str,
    page_hint: str,
    show_ktru_filter: bool,
    show_date_filter: bool,
    selected_date: str,
    ktru_query: str,
    page_num: int,
    catalog_ktru_codes: list[str] | None,
    pub_from_utc: str | None,
    pub_to_utc: str | None,
    ktru_queries: list[str] | None,
    catalog_only: bool = False,
    show_catalog_filter: bool = False,
) -> dict[str, Any]:
    preoffers = _load_rts_preoffers()
    per_page = 30
    total_rows = offer_matching_ktru_count(
        _eis_analytics_db_path(),
        ktru_queries=ktru_queries,
        catalog_ktru_codes=catalog_ktru_codes,
        pub_from_utc=pub_from_utc,
        pub_to_utc=pub_to_utc,
    )
    total_pages = max(1, (total_rows + per_page - 1) // per_page)
    page_num = max(1, min(int(page_num), total_pages))
    offset = (page_num - 1) * per_page
    rows = _offer_matching_rows_with_summary(
        limit=per_page,
        offset=offset,
        ktru_queries=ktru_queries,
        catalog_ktru_codes=catalog_ktru_codes,
        pub_from_utc=pub_from_utc,
        pub_to_utc=pub_to_utc,
    )
    query_parts: list[str] = []
    if show_date_filter and selected_date:
        query_parts.append(f"date={quote_plus(selected_date)}")
    if show_ktru_filter and ktru_query:
        query_parts.append(f"ktru={quote_plus(ktru_query)}")
    if show_catalog_filter:
        query_parts.append("catalog=1" if catalog_only else "catalog=0")
    base_query = "&".join(query_parts)
    if base_query:
        base_query += "&"
    return {
        "request": request,
        "title": page_title,
        "active": active,
        "paths": paths_dict(),
        "rows": rows,
        "preoffers_count": len(preoffers),
        "catalog_ktru_count": len(catalog_ktru_codes or []),
        "ktru_query": ktru_query,
        "selected_date": selected_date,
        "show_ktru_filter": show_ktru_filter,
        "show_date_filter": show_date_filter,
        "show_catalog_filter": show_catalog_filter,
        "catalog_only": catalog_only,
        "page_heading": page_heading,
        "page_hint": page_hint,
        "page_route": page_route,
        "html_return_to": "day_summary" if active == "day_summary" else "offer_matching",
        "offer_matching_base_query": base_query,
        "participation_price_ord_enabled": _participation_price_ord_enabled(),
        "empty_hint": (
            "В справочнике нет активных КТРУ — добавьте предложения с кодами КТРУ."
            if catalog_ktru_codes is not None and not catalog_ktru_codes
            else (
                f"За {selected_date} процедур с КТРУ из справочника не найдено."
                if show_date_filter
                else (
                    f"По КТРУ «{ktru_query}» ничего не найдено."
                    if ktru_query
                    else (
                        "Процедур с КТРУ из справочника предложений не найдено."
                        if catalog_only
                        else "RSS-наблюдений пока нет. Сначала включите монитор на странице «Извещения о закупке»."
                    )
                )
            )
        ),
        "pagination": {
            "page": page_num,
            "per_page": per_page,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "has_prev": page_num > 1,
            "has_next": page_num < total_pages,
            "prev_page": max(1, page_num - 1),
            "next_page": min(total_pages, page_num + 1),
            "first_row": offset + 1 if total_rows else 0,
            "last_row": min(offset + per_page, total_rows),
        },
        "html_flash_ok": request.session.pop("html_flash_ok", None),
        "html_flash_err": request.session.pop("html_flash_err", None),
    }


@app.get("/offer-matching", name="page_offer_matching")
def page_offer_matching(
    request: Request,
    _: Auth,
    page: Annotated[int, Query()] = 1,
    ktru: Annotated[str | None, Query()] = None,
    catalog: Annotated[str | None, Query()] = None,
) -> Any:
    ktru_query = (ktru or "").strip()
    ktru_queries = _parse_ktru_queries(ktru_query) or None
    catalog_only = _parse_catalog_only_param(catalog, default=True)
    ctx = _offer_matching_list_context(
        request,
        page_route="page_offer_matching",
        page_title="Подбор предложений — RTS",
        active="offer_matching",
        page_heading="Подбор предложений",
        page_hint="",
        show_ktru_filter=True,
        show_date_filter=False,
        show_catalog_filter=True,
        catalog_only=catalog_only,
        selected_date="",
        ktru_query=ktru_query,
        page_num=page,
        catalog_ktru_codes=_catalog_ktru_codes() if catalog_only else None,
        pub_from_utc=None,
        pub_to_utc=None,
        ktru_queries=ktru_queries,
    )
    ctx.update(web_auth_ui_context(request))
    ctx["pp_publish_enabled"] = _pp_publish_enabled()
    return _templates.TemplateResponse("offer_matching.html", ctx)


@app.get("/day-summary", name="page_day_summary")
def page_day_summary(
    request: Request,
    _: Auth,
    page: Annotated[int, Query()] = 1,
    date: Annotated[str | None, Query()] = None,
) -> Any:
    day = _parse_msk_date_param(date)
    pub_from, pub_to = msk_day_bounds_utc(day)
    catalog = _catalog_ktru_codes()
    ctx = _offer_matching_list_context(
        request,
        page_route="page_day_summary",
        page_title="Итоги дня — RTS",
        active="day_summary",
        page_heading="Итоги дня",
        page_hint="",
        show_ktru_filter=False,
        show_date_filter=True,
        selected_date=day.isoformat(),
        ktru_query="",
        page_num=page,
        catalog_ktru_codes=catalog,
        pub_from_utc=pub_from,
        pub_to_utc=pub_to,
        ktru_queries=None,
    )
    ctx.update(web_auth_ui_context(request))
    ctx["pp_publish_enabled"] = _pp_publish_enabled()
    return _templates.TemplateResponse("offer_matching.html", ctx)


@app.get("/notification-settings", name="page_notification_settings")
def page_notification_settings(request: Request, _: Auth) -> Any:
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Настройка уведомлений — RTS",
        "active": "notification_settings",
        "paths": paths_dict(),
        "form": _notification_settings_form(),
        "flash_ok": request.session.pop("notify_settings_flash_ok", None),
        "flash_err": request.session.pop("notify_settings_flash_err", None),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("notification_settings.html", ctx)


@app.post("/action/notification-settings-save", name="action_notification_settings_save")
def action_notification_settings_save(
    request: Request,
    _: Auth,
    min_purchase_amount: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = request.url_for("page_notification_settings")
    try:
        parse_min_purchase_amount(min_purchase_amount)
    except ValueError as exc:
        request.session["notify_settings_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    existing = load_notification_settings(_notification_settings_path())
    payload = parse_admin_form({"min_purchase_amount": min_purchase_amount}, existing=existing)
    payload["saved_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    save_notification_settings(_notification_settings_path(), payload)
    amount = min_purchase_amount_from_settings(payload)
    if amount is None:
        request.session["notify_settings_flash_ok"] = "Ограничение по сумме снято."
    else:
        display = int(amount) if amount == int(amount) else amount
        request.session["notify_settings_flash_ok"] = f"Минимальная сумма для уведомлений: {display} ₽."
    return RedirectResponse(back, status_code=303)


@app.get("/auto-participate", name="page_auto_participate")
def page_auto_participate(
    request: Request,
    _: SettingsAdmin,
    profile: Annotated[str | None, Query()] = None,
) -> Any:
    profiles = list(list_known_profiles())
    selected = normalize_rts_profile(profile) if profile else (profiles[0] if profiles else "")
    if selected and selected not in profiles:
        selected = profiles[0] if profiles else ""
    settings = load_auto_participate_settings(_auto_participate_settings_path())
    rules = profile_rules(settings, selected) if selected else {}
    cookie_path = _cookie_path_for_profile(selected) if selected else _COOKIE
    attempts = list_auto_participate_attempts(
        _eis_analytics_db_path(),
        limit=20,
        rts_profile=selected or None,
    )
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Авто-участие — RTS",
        "active": "auto_participate",
        "paths": paths_dict(request),
        "profiles": profiles,
        "selected_profile": selected,
        "form": form_from_profile_rules(rules),
        "cookie_status": cookie_file_status(cookie_path, _META, rts_profile=selected),
        "attempts": attempts,
        "global_enabled": global_enabled(settings),
        "env_auto_participate_enabled": _env_bool("RTS_AUTO_PARTICIPATE", False),
        "flash_ok": request.session.pop("auto_participate_flash_ok", None),
        "flash_err": request.session.pop("auto_participate_flash_err", None),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("auto_participate.html", ctx)


@app.post("/action/auto-participate-save", name="action_auto_participate_save")
def action_auto_participate_save(
    request: Request,
    _: SettingsAdmin,
    profile: Annotated[str, Form()],
    enabled: Annotated[str, Form()] = "",
    min_purchase_amount: Annotated[str, Form()] = "",
    ktru_mode: Annotated[str, Form()] = "catalog",
    ktru_codes_text: Annotated[str, Form()] = "",
    min_match_ratio: Annotated[str, Form()] = "",
    max_per_hour: Annotated[str, Form()] = "",
) -> RedirectResponse:
    selected = normalize_rts_profile(profile)
    back = str(request.url_for("page_auto_participate")) + (
        f"?profile={quote_plus(selected)}" if selected else ""
    )
    try:
        validate_rts_profile(selected)
        parsed = parse_form_profile_rules(
            {
                "enabled": enabled,
                "min_purchase_amount": min_purchase_amount,
                "ktru_mode": ktru_mode,
                "ktru_codes_text": ktru_codes_text,
                "min_match_ratio": min_match_ratio,
                "max_per_hour": max_per_hour,
            }
        )
    except ValueError as exc:
        request.session["auto_participate_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    settings = load_auto_participate_settings(_auto_participate_settings_path())
    updated = update_profile_in_settings(settings, selected, parsed)
    updated["saved_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    save_auto_participate_settings(_auto_participate_settings_path(), updated)
    request.session["auto_participate_flash_ok"] = f"Правила для {selected} сохранены."
    return RedirectResponse(back, status_code=303)


@app.get("/admin", name="page_admin")
def page_admin(
    request: Request,
    _: SettingsAdmin,
    edit_row_id: Annotated[str | None, Query()] = None,
    edit_user: Annotated[str | None, Query()] = None,
) -> Any:
    path = _country_origin_path()
    rows = load_country_origin_rows(path)
    country_form = _country_origin_empty_form()
    if edit_row_id:
        for row in rows:
            if row.row_id == edit_row_id.strip():
                country_form = _country_origin_form_from_row(row)
                break
    notices_settings = load_notification_settings(_notification_settings_path())
    pp_settings = load_pp_monitor_settings(_pp_monitor_settings_path())
    auto_pp_settings = load_auto_participate_settings(_auto_participate_settings_path())
    try:
        _, default_tg_chat = telegram_creds_from_env_file(_EIS_ENV)
        _, default_max_chat = max_creds_from_env_file(_EIS_ENV)
    except Exception:
        default_tg_chat = ""
        default_max_chat = ""
    ktru_cache_rows = list_ktru_cache_status(_KTRU_CACHE)
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Админка — RTS",
        "active": "admin",
        "paths": paths_dict(request),
        "country_rows": [_country_origin_row_payload(r) for r in rows],
        "country_form": country_form,
        "country_choices": COUNTRY_CHOICES,
        "ktru_cache_rows": ktru_cache_rows,
        "ktru_cache_ttl_days": round(default_ktru_cache_ttl_sec() / 86400, 1),
        "ktru_cache_expired_count": sum(1 for row in ktru_cache_rows if row.get("expired")),
        "notices_form": admin_form_from_settings(notices_settings),
        "notices_catalog_count": len(_catalog_ktru_codes()),
        "pp_monitor_form": pp_monitor_form_from_settings(pp_settings),
        "pp_monitor_runtime": pp_monitor_form_from_settings(pp_settings),
        "pp_monitor_cron_status": cron_status_payload(),
        "env_pp_monitor_enabled": _env_bool("RTS_PP_MONITOR", False),
        "auto_participate_global_enabled": global_enabled(auto_pp_settings),
        "auto_participate_enabled_profiles": count_enabled_profiles(auto_pp_settings),
        "auto_participate_profiles_total": len(list_known_profiles()),
        "env_auto_participate_enabled": _env_bool("RTS_AUTO_PARTICIPATE", False),
        "default_tg_chat": default_tg_chat,
        "default_max_chat": default_max_chat,
        "web_users": list_web_users_for_admin(_web_users_path()),
        "web_user_form": {"login": edit_user.strip() if edit_user else "", "password": ""},
        "edit_row_id": edit_row_id or "",
        "edit_user": edit_user or "",
        "admin_flash_ok": request.session.pop("admin_flash_ok", None),
        "admin_flash_err": request.session.pop("admin_flash_err", None),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("admin.html", ctx)


@app.post("/action/admin/country-origin-save", name="action_admin_country_origin_save")
def action_admin_country_origin_save(
    request: Request,
    _: SettingsAdmin,
    row_id: Annotated[str, Form()] = "",
    ktru_code: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    country_name: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    back = request.url_for("page_admin")
    try:
        saved = upsert_country_origin_row(
            _country_origin_path(),
            row_id=row_id,
            ktru_code=ktru_code,
            title=title,
            country_name=country_name,
            enabled=enabled == "1",
        )
        request.session["admin_flash_ok"] = f"Сохранено: {saved.title}"
    except ValueError as exc:
        request.session["admin_flash_err"] = str(exc)
    except OSError as exc:
        request.session["admin_flash_err"] = f"Не удалось сохранить справочник: {exc}"[:2000]
    return RedirectResponse(back, status_code=303)


@app.post("/action/admin/country-origin-delete", name="action_admin_country_origin_delete")
def action_admin_country_origin_delete(
    request: Request,
    _: SettingsAdmin,
    row_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = request.url_for("page_admin")
    if delete_country_origin_row(_country_origin_path(), row_id):
        request.session["admin_flash_ok"] = "Строка удалена."
    else:
        request.session["admin_flash_err"] = "Строка не найдена."
    return RedirectResponse(back, status_code=303)


@app.post("/action/admin/notices-notify-save", name="action_admin_notices_notify_save")
def action_admin_notices_notify_save(
    request: Request,
    _: SettingsAdmin,
    min_purchase_amount: Annotated[str, Form()] = "",
    max_purchase_amount: Annotated[str, Form()] = "",
    telegram_chat_id: Annotated[str, Form()] = "",
    max_chat_id: Annotated[str, Form()] = "",
    notify_telegram: Annotated[str | None, Form()] = None,
    notify_max: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    back = request.url_for("page_admin")
    existing = load_notification_settings(_notification_settings_path())
    try:
        payload = parse_admin_form(
            {
                "min_purchase_amount": min_purchase_amount,
                "max_purchase_amount": max_purchase_amount,
                "telegram_chat_id": telegram_chat_id,
                "max_chat_id": max_chat_id,
                "notify_telegram": notify_telegram or "",
                "notify_max": notify_max or "",
            },
            existing=existing,
        )
    except ValueError as exc:
        request.session["admin_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    payload["saved_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    save_notification_settings(_notification_settings_path(), payload)
    request.session["admin_flash_ok"] = "Настройки уведомлений по извещениям сохранены."
    return RedirectResponse(back, status_code=303)


@app.post("/action/admin/pp-monitor-notify-save", name="action_admin_pp_monitor_notify_save")
def action_admin_pp_monitor_notify_save(
    request: Request,
    _: SettingsAdmin,
    ktru_codes_text: Annotated[str, Form()] = "",
    price_min: Annotated[str, Form()] = "",
    price_max: Annotated[str, Form()] = "",
    skip_expired: Annotated[str | None, Form()] = None,
    telegram_chat_id: Annotated[str, Form()] = "",
    max_chat_id: Annotated[str, Form()] = "",
    notify_telegram: Annotated[str | None, Form()] = None,
    notify_max: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    back = request.url_for("page_admin")
    path = _pp_monitor_settings_path()
    existing = load_pp_monitor_settings(path)
    try:
        updated = parse_admin_notify_form(
            {
                "ktru_codes_text": ktru_codes_text,
                "price_min": price_min,
                "price_max": price_max,
                "skip_expired": skip_expired or "",
                "telegram_chat_id": telegram_chat_id,
                "max_chat_id": max_chat_id,
                "notify_telegram": notify_telegram or "",
                "notify_max": notify_max or "",
            },
            existing=existing,
        )
        save_pp_monitor_settings(path, updated)
    except ValueError as exc:
        request.session["admin_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    request.session["admin_flash_ok"] = "Настройки уведомлений монитора ПП сохранены."
    return RedirectResponse(back, status_code=303)


@app.post("/action/admin/pp-monitor-toggle", name="action_admin_pp_monitor_toggle")
def action_admin_pp_monitor_toggle(
    request: Request,
    _: SettingsAdmin,
    enabled: Annotated[str, Form()],
) -> RedirectResponse:
    back = request.url_for("page_admin")
    path = _pp_monitor_settings_path()
    existing = load_pp_monitor_settings(path)
    turn_on = str(enabled or "").strip().lower() in ("1", "on", "true", "yes")
    try:
        updated = apply_admin_runtime_settings(existing, enabled=turn_on)
        save_pp_monitor_settings(path, updated)
    except ValueError as exc:
        request.session["admin_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    request.session["admin_flash_ok"] = (
        "Монитор ПП включён." if turn_on else "Монитор ПП выключен."
    )
    return RedirectResponse(f"{back}#pp-monitor-runtime", status_code=303)


@app.post("/action/admin/pp-monitor-interval-save", name="action_admin_pp_monitor_interval_save")
def action_admin_pp_monitor_interval_save(
    request: Request,
    _: SettingsAdmin,
    poll_interval_min: Annotated[str, Form()] = "20",
) -> RedirectResponse:
    back = request.url_for("page_admin")
    path = _pp_monitor_settings_path()
    existing = load_pp_monitor_settings(path)
    try:
        updated = apply_admin_runtime_settings(existing, poll_interval_min=poll_interval_min)
        save_pp_monitor_settings(path, updated)
    except ValueError as exc:
        request.session["admin_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    interval = int(updated.get("poll_interval_min") or 20)
    request.session["admin_flash_ok"] = f"Периодичность опроса монитора ПП: {interval} мин."
    return RedirectResponse(f"{back}#pp-monitor-runtime", status_code=303)


@app.post("/action/admin/auto-participate-toggle", name="action_admin_auto_participate_toggle")
def action_admin_auto_participate_toggle(
    request: Request,
    _: SettingsAdmin,
    enabled: Annotated[str, Form()],
) -> RedirectResponse:
    back = request.url_for("page_admin")
    path = _auto_participate_settings_path()
    existing = load_auto_participate_settings(path)
    turn_on = str(enabled or "").strip().lower() in ("1", "on", "true", "yes")
    updated = apply_admin_global_enabled(existing, enabled=turn_on)
    updated["saved_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    save_auto_participate_settings(path, updated)
    request.session["admin_flash_ok"] = (
        "Автоучастие включено для всех профилей."
        if turn_on
        else "Автоучастие выключено для всех профилей."
    )
    return RedirectResponse(f"{back}#auto-participate-runtime", status_code=303)


@app.post("/action/admin/web-user-save", name="action_admin_web_user_save")
def action_admin_web_user_save(
    request: Request,
    _: SettingsAdmin,
    login: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = request.url_for("page_admin")
    try:
        upsert_web_user(_web_users_path(), login, password, allow_empty_password=True)
    except ValueError as exc:
        request.session["admin_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    request.session["admin_flash_ok"] = f"Пользователь {login.strip()} сохранён."
    return RedirectResponse(back, status_code=303)


@app.post("/action/admin/web-user-delete", name="action_admin_web_user_delete")
def action_admin_web_user_delete(
    request: Request,
    user: SettingsAdmin,
    login: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = request.url_for("page_admin")
    name = login.strip()
    if name == user.strip():
        request.session["admin_flash_err"] = "Нельзя удалить текущего пользователя."
        return RedirectResponse(back, status_code=303)
    if delete_web_user(_web_users_path(), name):
        request.session["admin_flash_ok"] = f"Пользователь {name} удалён."
    else:
        request.session["admin_flash_err"] = "Пользователь не найден."
    return RedirectResponse(back, status_code=303)


@app.post("/action/admin/ktru-cache-extend", name="action_admin_ktru_cache_extend")
def action_admin_ktru_cache_extend(
    request: Request,
    _: SettingsAdmin,
    ktru_code: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = request.url_for("page_admin")
    code = ktru_code.strip()
    if not code:
        request.session["admin_flash_err"] = "Код КТРУ не задан."
        return RedirectResponse(back, status_code=303)
    try:
        entry = extend_ktru_cache_validity(_KTRU_CACHE, code)
    except ValueError as exc:
        request.session["admin_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    except OSError as exc:
        request.session["admin_flash_err"] = f"Не удалось обновить кэш: {exc}"[:2000]
        return RedirectResponse(back, status_code=303)
    days = entry.get("days_remaining")
    request.session["admin_flash_ok"] = (
        f"Кэш {code} продлён: осталось {days} дн. (до {entry.get('expires_at_utc')} UTC)."
    )
    return RedirectResponse(back, status_code=303)


@app.post("/action/admin/ktru-cache-extend-all", name="action_admin_ktru_cache_extend_all")
def action_admin_ktru_cache_extend_all(
    request: Request,
    _: SettingsAdmin,
) -> RedirectResponse:
    back = request.url_for("page_admin")
    try:
        updated = extend_all_ktru_cache_validity(_KTRU_CACHE)
    except OSError as exc:
        request.session["admin_flash_err"] = f"Не удалось обновить кэш: {exc}"[:2000]
        return RedirectResponse(back, status_code=303)
    if not updated:
        request.session["admin_flash_err"] = "Кэш КТРУ пуст — нечего продлевать."
        return RedirectResponse(back, status_code=303)
    request.session["admin_flash_ok"] = f"Продлено {len(updated)} файлов кэша КТРУ на 7 дней."
    return RedirectResponse(back, status_code=303)


@app.get("/rts-pp-monitor", name="page_rts_pp_monitor")
def page_rts_pp_monitor(request: Request, _: SettingsAdmin) -> Any:
    settings = load_pp_monitor_settings(_pp_monitor_settings_path())
    journal = list_pp_monitor_journal_with_findings(
        _eis_analytics_db_path(),
        limit=30,
        settings=settings,
    )
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Монитор ПП РТС — RTS",
        "active": "rts_pp_monitor",
        "paths": paths_dict(request),
        "form": pp_monitor_form_from_settings(settings),
        "settings": settings,
        "cookie_status": _customer_monitor_cookie_status(),
        "customer_profile": CUSTOMER_MONITOR_PROFILE,
        "journal": journal,
        "env_monitor_enabled": _env_bool("RTS_PP_MONITOR", False),
        "flash_ok": request.session.pop("pp_monitor_flash_ok", None),
        "flash_err": request.session.pop("pp_monitor_flash_err", None),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("rts_pp_monitor.html", ctx)


@app.post("/action/rts-pp-monitor-save", name="action_rts_pp_monitor_save")
def action_rts_pp_monitor_save(
    request: Request,
    _: SettingsAdmin,
    enabled: Annotated[str, Form()] = "",
    poll_interval_min: Annotated[str, Form()] = "20",
    ktru_codes_text: Annotated[str, Form()] = "",
    price_min: Annotated[str, Form()] = "",
    price_max: Annotated[str, Form()] = "",
    region_ids_text: Annotated[str, Form()] = "",
    skip_expired: Annotated[str, Form()] = "",
    notify_telegram: Annotated[str, Form()] = "",
    notify_max: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = request.url_for("page_rts_pp_monitor")
    existing = load_pp_monitor_settings(_pp_monitor_settings_path())
    try:
        parsed = parse_pp_monitor_form(
            {
                "enabled": enabled,
                "poll_interval_min": poll_interval_min,
                "ktru_codes_text": ktru_codes_text,
                "price_min": price_min,
                "price_max": price_max,
                "region_ids_text": region_ids_text,
                "skip_expired": skip_expired,
                "notify_telegram": notify_telegram,
                "notify_max": notify_max,
            },
            existing=existing,
        )
    except ValueError as exc:
        request.session["pp_monitor_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    save_pp_monitor_settings(_pp_monitor_settings_path(), parsed)
    request.session["pp_monitor_flash_ok"] = "Настройки монитора сохранены."
    return RedirectResponse(back, status_code=303)


@app.post("/action/rts-pp-monitor-run-once", name="action_rts_pp_monitor_run_once")
def action_rts_pp_monitor_run_once(request: Request, _: SettingsAdmin) -> RedirectResponse:
    back = request.url_for("page_rts_pp_monitor")
    busy = _pp_monitor_poll_busy_message()
    if busy:
        request.session["pp_monitor_flash_err"] = busy
        return RedirectResponse(back, status_code=303)
    settings = load_pp_monitor_settings(_pp_monitor_settings_path())
    if not settings.get("registry_backfill_done"):
        request.session["pp_monitor_flash_err"] = (
            "Сначала выполните backfill реестра (кнопка ниже)."
        )
        return RedirectResponse(back, status_code=303)
    _start_pp_monitor_poll_background(force=True)
    request.session["pp_monitor_flash_ok"] = (
        "Опрос запущен в фоне. Статус — на странице «Реестр предложений»."
    )
    return RedirectResponse(back, status_code=303)


@app.post("/action/rts-pp-monitor-backfill", name="action_rts_pp_monitor_backfill")
def action_rts_pp_monitor_backfill(request: Request, _: SettingsAdmin) -> RedirectResponse:
    back = request.url_for("page_rts_pp_monitor")
    if not _rts_pp_monitor_lock.acquire(blocking=False):
        request.session["pp_monitor_flash_err"] = "Backfill уже выполняется."
        return RedirectResponse(back, status_code=303)
    _rts_pp_monitor_lock.release()
    settings = load_pp_monitor_settings(_pp_monitor_settings_path())
    if settings.get("registry_backfill_done"):
        request.session["pp_monitor_flash_ok"] = "Backfill реестра уже выполнен."
        return RedirectResponse(back, status_code=303)
    _start_pp_monitor_backfill_background(force=False)
    request.session["pp_monitor_flash_ok"] = (
        "Backfill запущен в фоне. Обновите страницу через несколько минут."
    )
    return RedirectResponse(back, status_code=303)


@app.post("/action/rts-pp-monitor-reseed", name="action_rts_pp_monitor_reseed")
def action_rts_pp_monitor_reseed(request: Request, _: SettingsAdmin) -> RedirectResponse:
    back = request.url_for("page_rts_pp_monitor")
    clear_pp_monitor_notifications(_eis_analytics_db_path())
    clear_pp_monitor_registry(_eis_analytics_db_path())
    existing = load_pp_monitor_settings(_pp_monitor_settings_path())
    save_pp_monitor_settings(
        _pp_monitor_settings_path(),
        {
            **existing,
            "initial_seed_done": False,
            "registry_backfill_done": False,
            "registry_backfill": {},
            "link_date_watermarks": {},
        },
    )
    request.session["pp_monitor_flash_ok"] = (
        "Baseline сброшен. Запустите backfill реестра перед включением опроса."
    )
    return RedirectResponse(back, status_code=303)


@app.post("/action/rts-pp-monitor-fetch-find", name="action_rts_pp_monitor_fetch_find")
def action_rts_pp_monitor_fetch_find(
    request: Request,
    _: Auth,
    offer_id: Annotated[str, Form()],
    return_to: Annotated[str, Form()] = "monitor",
    return_ktru: Annotated[str, Form()] = "",
    return_q: Annotated[str, Form()] = "",
    return_name: Annotated[list[str], Form()] = [],
    return_price_min: Annotated[str, Form()] = "",
    return_price_max: Annotated[str, Form()] = "",
    return_page: Annotated[str, Form()] = "1",
    return_sort: Annotated[str, Form()] = "link_date",
    return_dir: Annotated[str, Form()] = "desc",
    return_group: Annotated[str, Form()] = "",
) -> RedirectResponse:
    group_stacks = str(return_group or "").strip().lower() in ("1", "on", "true", "yes")
    if str(return_to or "").strip() == "registry":
        try:
            page_num = max(1, int(str(return_page or "1")))
        except ValueError:
            page_num = 1
        back = _pp_registry_list_url(
            request,
            ktru=str(return_ktru or "").strip(),
            q=str(return_q or "").strip(),
            names=[str(n or "").strip() for n in return_name if str(n or "").strip()],
            price_min=str(return_price_min or "").strip(),
            price_max=str(return_price_max or "").strip(),
            page=page_num,
            sort=str(return_sort or "link_date").strip() or "link_date",
            sort_dir=str(return_dir or "desc").strip() or "desc",
            group_stacks=group_stacks,
        )
    else:
        back = str(request.url_for("page_rts_pp_monitor"))
    oid = str(offer_id or "").strip()
    if not oid:
        request.session["pp_monitor_flash_err"] = "Не указан ID ПП."
        return RedirectResponse(back, status_code=303)
    try:
        cookie = load_customer_cookie(_META)
    except RtsCustomerPpApiError as exc:
        request.session["pp_monitor_flash_err"] = str(exc)
        return RedirectResponse(back, status_code=303)
    try:
        find_item = find_offer_item(cookie, oid)
    except RtsCustomerPpApiError as exc:
        request.session["pp_monitor_flash_err"] = f"Find не удался для {oid}: {exc}"
        return RedirectResponse(back, status_code=303)
    db_path = _eis_analytics_db_path()
    meta = get_pp_monitor_notification_latest(db_path, oid) or {}
    reg = get_pp_monitor_registry_row(db_path, oid) or {}
    ktru_obj = find_item.get("ktru")
    ktru_from_find = str(ktru_obj.get("code") or "") if isinstance(ktru_obj, dict) else ""
    unit_price = meta.get("unit_price")
    if unit_price is None:
        unit_price = reg.get("unit_price")
    upsert_pp_monitor_finding(
        db_path,
        offer_price_list_item_id=oid,
        find_json=json.dumps(find_item, ensure_ascii=False),
        ktru_code=str(meta.get("ktru_code") or reg.get("ktru_code") or ktru_from_find),
        trade_mark=str(meta.get("trade_mark") or reg.get("trade_mark") or find_item.get("tradeMark") or ""),
        link_date=str(meta.get("link_date") or reg.get("link_date") or ""),
        unit_price=unit_price,
        card_url=str(meta.get("card_url") or reg.get("card_url") or ""),
        source="admin_fetch",
    )
    request.session["pp_monitor_flash_ok"] = f"Карточка сохранена для {oid}."
    return RedirectResponse(back, status_code=303)


_PP_REGISTRY_PER_PAGE = 50
_PP_REGISTRY_SORT_DEFAULT = "link_date"
_PP_REGISTRY_DIR_DEFAULT = "desc"
_PP_REGISTRY_NAME_FILTERS_SESSION_KEY = "pp_registry_name_filters"


def _resolve_pp_registry_name_filters(
    request: Request,
    *,
    name_query_params: list[str],
    reset_names: bool,
    apply_filters: bool,
    admin_view: bool,
) -> list[str]:
    if admin_view:
        return [str(n or "").strip() for n in name_query_params if str(n or "").strip()]
    if reset_names:
        request.session.pop(_PP_REGISTRY_NAME_FILTERS_SESSION_KEY, None)
        return []
    if apply_filters or name_query_params:
        names = [str(n or "").strip() for n in name_query_params if str(n or "").strip()]
        request.session[_PP_REGISTRY_NAME_FILTERS_SESSION_KEY] = names
        return names
    stored = request.session.get(_PP_REGISTRY_NAME_FILTERS_SESSION_KEY)
    if isinstance(stored, list):
        return [str(n or "").strip() for n in stored if str(n or "").strip()]
    return []


def _resolve_pp_registry_price_filters(
    price_min: str,
    price_max: str,
) -> tuple[float | None, float | None, str, str]:
    min_raw = str(price_min or "").strip()
    max_raw = str(price_max or "").strip()
    min_val = parse_money_amount(min_raw)
    max_val = parse_money_amount(max_raw)
    if min_val is not None and max_val is not None and min_val > max_val:
        min_val, max_val = max_val, min_val
        min_raw, max_raw = max_raw, min_raw
    return min_val, max_val, min_raw, max_raw


def _pp_registry_query_pairs(
    *,
    ktru: str = "",
    q: str = "",
    names: list[str] | None = None,
    price_min: str = "",
    price_max: str = "",
    page: int = 1,
    sort: str = _PP_REGISTRY_SORT_DEFAULT,
    sort_dir: str = _PP_REGISTRY_DIR_DEFAULT,
    group_stacks: bool = False,
) -> list[tuple[str, str | int]]:
    pairs: list[tuple[str, str | int]] = []
    ktru_val = str(ktru or "").strip()
    q_val = str(q or "").strip()
    name_vals = [str(n or "").strip() for n in (names or []) if str(n or "").strip()]
    price_min_val = str(price_min or "").strip()
    price_max_val = str(price_max or "").strip()
    if ktru_val:
        pairs.append(("ktru", ktru_val))
    if name_vals:
        for name in name_vals:
            pairs.append(("name", name))
    elif q_val:
        pairs.append(("q", q_val))
    if price_min_val:
        pairs.append(("price_min", price_min_val))
    if price_max_val:
        pairs.append(("price_max", price_max_val))
    if page > 1:
        pairs.append(("page", page))
    if group_stacks:
        pairs.append(("group", 1))
    sort_val = str(sort or _PP_REGISTRY_SORT_DEFAULT).strip() or _PP_REGISTRY_SORT_DEFAULT
    dir_val = str(sort_dir or _PP_REGISTRY_DIR_DEFAULT).strip().lower() or _PP_REGISTRY_DIR_DEFAULT
    if sort_val != _PP_REGISTRY_SORT_DEFAULT:
        pairs.append(("sort", sort_val))
    if dir_val != _PP_REGISTRY_DIR_DEFAULT:
        pairs.append(("dir", dir_val))
    return pairs


def _pp_registry_list_url(
    request: Request,
    *,
    ktru: str = "",
    q: str = "",
    names: list[str] | None = None,
    price_min: str = "",
    price_max: str = "",
    page: int = 1,
    sort: str = _PP_REGISTRY_SORT_DEFAULT,
    sort_dir: str = _PP_REGISTRY_DIR_DEFAULT,
    group_stacks: bool = False,
) -> str:
    pairs = _pp_registry_query_pairs(
        ktru=ktru,
        q=q,
        names=names,
        price_min=price_min,
        price_max=price_max,
        page=page,
        sort=sort,
        sort_dir=sort_dir,
        group_stacks=group_stacks,
    )
    base = str(request.url_for("page_pp_monitor_samples"))
    return base + ("?" + urlencode(pairs) if pairs else "")


def _pp_registry_list_query(
    *,
    ktru: str = "",
    q: str = "",
    names: list[str] | None = None,
    price_min: str = "",
    price_max: str = "",
    page: int = 1,
    sort: str = _PP_REGISTRY_SORT_DEFAULT,
    sort_dir: str = _PP_REGISTRY_DIR_DEFAULT,
    group_stacks: bool = False,
) -> str:
    pairs = _pp_registry_query_pairs(
        ktru=ktru,
        q=q,
        names=names,
        price_min=price_min,
        price_max=price_max,
        page=page,
        sort=sort,
        sort_dir=sort_dir,
        group_stacks=group_stacks,
    )
    encoded = urlencode(pairs)
    return encoded + "&" if encoded else ""


def _annotate_registry_service_drafts(
    *,
    rows: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    db_path: Path,
    draft_profiles: list[str] | None,
) -> None:
    offer_ids: list[str] = []
    for row in rows:
        oid = str(row.get("offer_price_list_item_id") or "").strip()
        if oid:
            offer_ids.append(oid)
    for group in groups:
        primary = group.get("primary") or {}
        poid = str(primary.get("offer_price_list_item_id") or "").strip()
        if poid:
            offer_ids.append(poid)
        for child in group.get("children") or []:
            coid = str(child.get("offer_price_list_item_id") or "").strip()
            if coid:
                offer_ids.append(coid)
    draft_created_at = load_pp_monitor_draft_created_at(db_path, offer_ids, rts_profiles=draft_profiles)

    def _apply(row: dict[str, Any]) -> None:
        oid = str(row.get("offer_price_list_item_id") or "").strip()
        created_raw = draft_created_at.get(oid, "")
        row["has_service_draft"] = bool(created_raw)
        row["service_draft_created_display_msk"] = (
            format_utc_iso_msk_clean(created_raw) if created_raw else "—"
        )

    for row in rows:
        _apply(row)
    for group in groups:
        _apply(group.get("primary") or {})
        for child in group.get("children") or []:
            _apply(child)


def _annotate_registry_pp_winners(
    *,
    rows: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    db_path: Path,
) -> None:
    offer_ids: list[str] = []
    for row in rows:
        oid = str(row.get("offer_price_list_item_id") or "").strip()
        if oid:
            offer_ids.append(oid)
    for group in groups:
        primary = group.get("primary") or {}
        poid = str(primary.get("offer_price_list_item_id") or "").strip()
        if poid:
            offer_ids.append(poid)
        for child in group.get("children") or []:
            coid = str(child.get("offer_price_list_item_id") or "").strip()
            if coid:
                offer_ids.append(coid)
    winners = pp_winner_offer_ids(db_path, offer_ids)

    def _apply(row: dict[str, Any]) -> None:
        oid = str(row.get("offer_price_list_item_id") or "").strip()
        row["is_pp_winner"] = oid in winners

    for row in rows:
        _apply(row)
    for group in groups:
        _apply(group.get("primary") or {})
        for child in group.get("children") or []:
            _apply(child)


@app.get("/pp-monitor-samples", name="page_pp_monitor_samples")
def page_pp_monitor_samples(
    request: Request,
    _: Auth,
    ktru: Annotated[str, Query()] = "",
    q: Annotated[str, Query()] = "",
    name: Annotated[list[str], Query()] = [],
    price_min: Annotated[str, Query()] = "",
    price_max: Annotated[str, Query()] = "",
    page: Annotated[int, Query()] = 1,
    sort: Annotated[str, Query()] = _PP_REGISTRY_SORT_DEFAULT,
    sort_dir: Annotated[str, Query(alias="dir")] = _PP_REGISTRY_DIR_DEFAULT,
    group: Annotated[int, Query()] = 0,
    apply_filters: Annotated[int, Query()] = 0,
    reset_names: Annotated[int, Query()] = 0,
) -> Any:
    ktru_filter = str(ktru or "").strip()
    _, admin_view = _pp_draft_profiles_for_request(request)
    name_filters = _resolve_pp_registry_name_filters(
        request,
        name_query_params=name,
        reset_names=int(reset_names or 0) == 1,
        apply_filters=int(apply_filters or 0) == 1,
        admin_view=admin_view,
    )
    legacy_name_query = str(q or "").strip() if not name_filters else ""
    unit_price_min, unit_price_max, price_min_filter, price_max_filter = _resolve_pp_registry_price_filters(
        price_min,
        price_max,
    )
    group_stacks = int(group or 0) == 1
    sort_key = str(sort or _PP_REGISTRY_SORT_DEFAULT).strip() or _PP_REGISTRY_SORT_DEFAULT
    if sort_key not in {
        "last_seen_at_utc",
        "first_seen_at_utc",
        "link_date",
        "ktru_code",
        "name",
        "trade_mark",
        "validity_date",
        "unit_price",
        "delivery_regions_count",
        "has_find",
    }:
        sort_key = _PP_REGISTRY_SORT_DEFAULT
    direction = str(sort_dir or _PP_REGISTRY_DIR_DEFAULT).strip().lower()
    if direction not in ("asc", "desc"):
        direction = _PP_REGISTRY_DIR_DEFAULT
    db_path = _eis_analytics_db_path()
    per_page = _PP_REGISTRY_PER_PAGE
    if group_stacks:
        total_rows = count_pp_monitor_registry_groups(
            db_path,
            ktru_code=ktru_filter or None,
            name_query=legacy_name_query or None,
            name_values=name_filters or None,
            unit_price_min=unit_price_min,
            unit_price_max=unit_price_max,
        )
        total_pages = max(1, (total_rows + per_page - 1) // per_page)
        page_num = max(1, min(int(page), total_pages))
        offset = (page_num - 1) * per_page
        groups = list_pp_monitor_registry_groups(
            db_path,
            limit=per_page,
            offset=offset,
            ktru_code=ktru_filter or None,
            name_query=legacy_name_query or None,
            name_values=name_filters or None,
            unit_price_min=unit_price_min,
            unit_price_max=unit_price_max,
            sort=sort_key,
            sort_dir=direction,
        )
        rows: list[dict[str, Any]] = []
    else:
        total_rows = count_pp_monitor_registry(
            db_path,
            ktru_code=ktru_filter or None,
            name_query=legacy_name_query or None,
            name_values=name_filters or None,
            unit_price_min=unit_price_min,
            unit_price_max=unit_price_max,
        )
        total_pages = max(1, (total_rows + per_page - 1) // per_page)
        page_num = max(1, min(int(page), total_pages))
        offset = (page_num - 1) * per_page
        rows = list_pp_monitor_registry(
            db_path,
            limit=per_page,
            offset=offset,
            ktru_code=ktru_filter or None,
            name_query=legacy_name_query or None,
            name_values=name_filters or None,
            unit_price_min=unit_price_min,
            unit_price_max=unit_price_max,
            sort=sort_key,
            sort_dir=direction,
        )
        groups = []
    draft_profiles, _admin_view = _pp_draft_profiles_for_request(request)
    _annotate_registry_service_drafts(
        rows=rows,
        groups=groups,
        db_path=db_path,
        draft_profiles=None if _admin_view else draft_profiles,
    )
    _annotate_registry_pp_winners(rows=rows, groups=groups, db_path=db_path)
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Реестр предложений — RTS",
        "active": "pp_monitor_samples",
        "paths": paths_dict(request),
        "registry_rows": rows,
        "registry_groups": groups,
        "registry_grouped": group_stacks,
        "registry_total": total_rows,
        "ktru_filter": ktru_filter,
        "name_filters": name_filters,
        "legacy_name_query": legacy_name_query,
        "price_min_filter": price_min_filter,
        "price_max_filter": price_max_filter,
        "name_options": list_pp_monitor_registry_names(db_path, ktru_code=ktru_filter or None),
        "ktru_codes": list_pp_monitor_registry_ktru_codes(db_path),
        "pp_monitor_status": _rts_pp_monitor_cron_status_payload(),
        "registry_sort": sort_key,
        "registry_sort_dir": direction,
        "registry_base_query": _pp_registry_list_query(
            ktru=ktru_filter,
            q=legacy_name_query,
            names=name_filters,
            price_min=price_min_filter,
            price_max=price_max_filter,
            page=page_num,
            sort=sort_key,
            sort_dir=direction,
            group_stacks=group_stacks,
        ),
        "registry_sort_urls": {
            col: _pp_registry_list_url(
                request,
                ktru=ktru_filter,
                q=legacy_name_query,
                names=name_filters,
                price_min=price_min_filter,
                price_max=price_max_filter,
                page=1,
                sort=col,
                sort_dir=(
                    "asc"
                    if sort_key == col and direction == "desc"
                    else "desc"
                ),
                group_stacks=group_stacks,
            )
            for col in (
                "last_seen_at_utc",
                "first_seen_at_utc",
                "link_date",
                "ktru_code",
                "name",
                "trade_mark",
                "validity_date",
                "unit_price",
                "delivery_regions_count",
                "has_find",
            )
        },
        "flash_ok": request.session.pop("pp_monitor_flash_ok", None),
        "flash_err": request.session.pop("pp_monitor_flash_err", None),
        "pagination": {
            "page": page_num,
            "per_page": per_page,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "has_prev": page_num > 1,
            "has_next": page_num < total_pages,
            "prev_page": max(1, page_num - 1),
            "next_page": min(total_pages, page_num + 1),
            "first_row": offset + 1 if total_rows else 0,
            "last_row": min(offset + per_page, total_rows),
        },
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("pp_monitor_samples.html", ctx)


_PP_CHAIN_PER_PAGE = 30
_PP_CHAIN_MIN_MEMBERS_DEFAULT = 2


@app.get("/pp-monitor-chain", name="page_pp_monitor_chain")
def page_pp_monitor_chain(
    request: Request,
    _: SettingsAdmin,
    ktru: Annotated[str, Query()] = "",
    only_our: Annotated[int, Query(alias="only_our")] = 0,
    min_members: Annotated[int, Query(alias="min")] = _PP_CHAIN_MIN_MEMBERS_DEFAULT,
    page: Annotated[int, Query()] = 1,
    sort: Annotated[str, Query()] = "last_link_date",
    sort_dir: Annotated[str, Query(alias="dir")] = "desc",
) -> Any:
    ktru_filter = str(ktru or "").strip()
    min_mem = max(2, int(min_members or _PP_CHAIN_MIN_MEMBERS_DEFAULT))
    only_with_our = int(only_our or 0) == 1
    sort_key = str(sort or "last_link_date").strip()
    if sort_key not in {"last_link_date", "member_count", "ktru_code", "min_price", "max_price"}:
        sort_key = "last_link_date"
    direction = str(sort_dir or "desc").strip().lower()
    if direction not in ("asc", "desc"):
        direction = "desc"
    db_path = _eis_analytics_db_path()
    total_rows = count_pp_monitor_stacks(
        db_path,
        ktru_code=ktru_filter or None,
        min_members=min_mem,
        only_with_our=only_with_our,
    )
    per_page = _PP_CHAIN_PER_PAGE
    total_pages = max(1, (total_rows + per_page - 1) // per_page)
    page_num = max(1, min(int(page), total_pages))
    offset = (page_num - 1) * per_page
    stacks = list_pp_monitor_stacks(
        db_path,
        ktru_code=ktru_filter or None,
        min_members=min_mem,
        only_with_our=only_with_our,
        limit=per_page,
        offset=offset,
        sort=sort_key,
        sort_dir=direction,
    )
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Цепочка ПП — RTS",
        "active": "pp_chain",
        "paths": paths_dict(request),
        "stacks": stacks,
        "stack_total": total_rows,
        "ktru_filter": ktru_filter,
        "only_with_our": only_with_our,
        "min_members": min_mem,
        "ktru_codes": list_pp_monitor_registry_ktru_codes(db_path),
        "chain_sort": sort_key,
        "chain_sort_dir": direction,
        "flash_ok": request.session.pop("pp_chain_flash_ok", None),
        "flash_err": request.session.pop("pp_chain_flash_err", None),
        "pagination": {
            "page": page_num,
            "per_page": per_page,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "has_prev": page_num > 1,
            "has_next": page_num < total_pages,
            "prev_page": max(1, page_num - 1),
            "next_page": min(total_pages, page_num + 1),
        },
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("pp_monitor_chain.html", ctx)


@app.get("/pp-monitor-chain/{stack_key:path}", name="page_pp_monitor_chain_detail")
def page_pp_monitor_chain_detail(request: Request, _: SettingsAdmin, stack_key: str) -> Any:
    db_path = _eis_analytics_db_path()
    members = list_pp_monitor_stack_members(db_path, stack_key, annotate_our=True)
    if not members:
        raise HTTPException(status_code=404, detail="Стопка не найдена")
    origin_id = str(members[0].get("offer_price_list_item_id") or "").strip()
    for member in members:
        member["is_origin"] = str(member.get("offer_price_list_item_id") or "").strip() == origin_id
    copy_count = max(0, len(members) - 1)
    ctx: dict[str, Any] = {
        "request": request,
        "title": f"Цепочка ПП — RTS",
        "active": "pp_chain",
        "paths": paths_dict(request),
        "stack_key": stack_key,
        "members": members,
        "ktru_code": members[0].get("ktru_code") if members else "",
        "member_count": len(members),
        "copy_count": copy_count,
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("pp_monitor_chain_detail.html", ctx)


@app.post("/action/admin/pp-monitor-chain-backfill", name="action_admin_pp_monitor_chain_backfill")
def action_admin_pp_monitor_chain_backfill(request: Request, _: SettingsAdmin) -> RedirectResponse:
    back = request.url_for("page_pp_monitor_chain")
    try:
        updated = backfill_pp_characteristics_hashes(_eis_analytics_db_path())
        request.session["pp_chain_flash_ok"] = f"Пересчитано hash для {updated} записей find."
    except Exception as exc:
        request.session["pp_chain_flash_err"] = str(exc)
    return RedirectResponse(url=str(back), status_code=303)


@app.get("/pp-monitor-samples/{offer_id}", name="page_pp_monitor_sample_detail")
def page_pp_monitor_sample_detail(request: Request, _: Auth, offer_id: str) -> Any:
    row = get_pp_monitor_finding(_eis_analytics_db_path(), offer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Образец не найден")
    find_item = row.get("find") if isinstance(row.get("find"), dict) else {}
    ktru_obj = find_item.get("ktru") if isinstance(find_item.get("ktru"), dict) else {}
    unit_obj = find_item.get("unit") if isinstance(find_item.get("unit"), dict) else {}
    profile = _rts_profile_for_request(request)
    monitor_draft = get_pp_monitor_draft(_eis_analytics_db_path(), offer_id, rts_profile=profile)
    registry_row = get_pp_monitor_registry_row(_eis_analytics_db_path(), offer_id)
    chain_url = None
    if registry_row:
        chash = str(registry_row.get("characteristics_hash") or row.get("characteristics_hash") or "").strip()
        ktru = str(registry_row.get("ktru_code") or row.get("ktru_code") or "").strip()
        if chash and ktru and chash != "empty":
            chain_url = request.url_for("page_pp_monitor_chain_detail", stack_key=pp_stack_key(ktru, chash))
    eis_pp_link_candidates = list_pp_eis_matches(
        _eis_analytics_db_path(),
        limit=5,
        offer_price_list_item_id=offer_id,
    )
    ctx: dict[str, Any] = {
        "request": request,
        "title": f"Образец ПП {offer_id} — RTS",
        "active": "pp_monitor_samples",
        "paths": paths_dict(request),
        "sample": row,
        "find_item": find_item,
        "ktru_name": str(ktru_obj.get("name") or "").strip(),
        "unit_name": str(unit_obj.get("name") or "").strip(),
        "characteristics": list_characteristic_rows(find_item),
        "monitor_draft": monitor_draft,
        "participation_price_ord_enabled": _participation_price_ord_enabled(),
        "chain_url": chain_url,
        "eis_pp_link_candidates": eis_pp_link_candidates,
        "pp_publish_enabled": _pp_publish_enabled(),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("pp_monitor_sample_detail.html", ctx)


def _parse_eis_pp_link_lag_filter(lag: str | None) -> str | None:
    text = str(lag or "").strip().lower()
    if text in ("before", "after", "zero", "all"):
        return None if text == "all" else text
    return None


_EIS_PP_LINK_SORT_COLUMNS = frozenset({"notice_pub", "pp_link", "lag"})


def _eis_pp_link_list_url(
    request: Request,
    *,
    ktru: str = "",
    status: str = "all",
    lag: str = "all",
    page: int = 1,
    sort: str = "",
    sort_dir: str = "desc",
) -> str:
    pairs: list[tuple[str, str | int]] = []
    ktru_val = str(ktru or "").strip()
    status_val = str(status or "all").strip().lower() or "all"
    lag_val = str(lag or "all").strip().lower() or "all"
    sort_val = str(sort or "").strip()
    dir_val = str(sort_dir or "desc").strip().lower() or "desc"
    if ktru_val:
        pairs.append(("ktru", ktru_val))
    if status_val and status_val != "all":
        pairs.append(("status", status_val))
    if lag_val and lag_val != "all":
        pairs.append(("lag", lag_val))
    if page > 1:
        pairs.append(("page", page))
    if sort_val in _EIS_PP_LINK_SORT_COLUMNS:
        pairs.append(("sort", sort_val))
        if dir_val == "asc":
            pairs.append(("dir", "asc"))
    base = str(request.url_for("page_eis_pp_link"))
    return base + ("?" + urlencode(pairs) if pairs else "")


@app.get("/eis-pp-link", name="page_eis_pp_link")
def page_eis_pp_link(
    request: Request,
    _: Auth,
    page: Annotated[int, Query()] = 1,
    ktru: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    lag: Annotated[str | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
    sort_dir: Annotated[str | None, Query(alias="dir")] = None,
    purchase_number: Annotated[str | None, Query()] = None,
    line_key: Annotated[str | None, Query()] = None,
    offer_id: Annotated[str | None, Query()] = None,
) -> Any:
    db = _eis_analytics_db_path()
    per_page = 50
    page_num = max(1, int(page or 1))
    ktru_code = str(ktru or "").strip() or None
    analyst_status = str(status or "").strip().lower() or None
    if analyst_status == "all":
        analyst_status = None
    lag_direction = _parse_eis_pp_link_lag_filter(lag)
    window_min = int(pp_eis_link_window_min())
    sort_key = str(sort or "").strip()
    if sort_key not in _EIS_PP_LINK_SORT_COLUMNS:
        sort_key = ""
    direction = str(sort_dir or "desc").strip().lower()
    if direction not in ("asc", "desc"):
        direction = "desc"
    filter_ktru = ktru_code or ""
    filter_status = analyst_status or "all"
    filter_lag = lag or "all"
    if purchase_number and line_key and offer_id:
        match = get_pp_eis_match(
            db,
            purchase_number=purchase_number,
            line_key=line_key,
            offer_price_list_item_id=offer_id,
        )
        if match is None:
            raise HTTPException(status_code=404, detail="Сопоставление не найдено")
        enriched = enrich_pp_eis_matches_with_unit_prices(db, [match])
        if enriched:
            match = enriched[0]
        event_chain = build_pp_eis_event_chain(db, match)
        prehistory = find_predecessors(db, match)
        ctx: dict[str, Any] = {
            "request": request,
            "title": f"Связь ЕИС-ПП {purchase_number} ↔ {offer_id} — RTS",
            "active": "eis_pp_link",
            "paths": paths_dict(request),
            "detail_mode": True,
            "match": match,
            "event_chain": event_chain,
            "prehistory": prehistory.as_dict(),
            "pilot_ktru_codes": sorted(pilot_ktru_codes()),
            "window_min": window_min,
            "flash_ok": request.session.pop("eis_pp_link_flash_ok", None),
            "flash_err": request.session.pop("eis_pp_link_flash_err", None),
        }
        ctx.update(tagging_ui_context())
        ctx.update(web_auth_ui_context(request))
        return _templates.TemplateResponse("eis_pp_link.html", ctx)

    offset = (page_num - 1) * per_page
    pn_filter = str(purchase_number or "").strip() or None
    lk_filter = str(line_key or "").strip() or None
    oid_filter = str(offer_id or "").strip() or None
    rows = list_pp_eis_matches(
        db,
        limit=per_page,
        offset=offset,
        ktru_code=ktru_code,
        analyst_status=analyst_status,
        exact_only=True,
        lag_direction=lag_direction,
        purchase_number=pn_filter,
        line_key=lk_filter,
        offer_price_list_item_id=oid_filter,
        sort=sort_key or None,
        sort_dir=direction if sort_key else None,
    )
    rows = enrich_matches_with_prehistory(db, rows)
    rows = enrich_pp_eis_matches_with_unit_prices(db, rows)
    total_rows = count_pp_eis_matches(
        db,
        ktru_code=ktru_code,
        analyst_status=analyst_status,
        exact_only=True,
        lag_direction=lag_direction,
        purchase_number=pn_filter,
        line_key=lk_filter,
        offer_price_list_item_id=oid_filter,
    )
    total_pages = max(1, (total_rows + per_page - 1) // per_page)
    ctx = {
        "request": request,
        "title": "Связь ЕИС-ПП — RTS",
        "active": "eis_pp_link",
        "paths": paths_dict(request),
        "detail_mode": False,
        "rows": rows,
        "pilot_ktru_codes": sorted(pilot_ktru_codes()),
        "filters": {
            "ktru": ktru_code or "",
            "status": analyst_status or "all",
            "lag": lag or "all",
        },
        "list_sort": sort_key,
        "list_sort_dir": direction,
        "list_sort_urls": {
            col: _eis_pp_link_list_url(
                request,
                ktru=filter_ktru,
                status=filter_status,
                lag=filter_lag,
                page=1,
                sort=col,
                sort_dir=(
                    "asc"
                    if sort_key == col and direction == "desc"
                    else "desc"
                ),
            )
            for col in sorted(_EIS_PP_LINK_SORT_COLUMNS)
        },
        "window_min": window_min,
        "pagination": {
            "page": page_num,
            "total_pages": total_pages,
            "total_rows": total_rows,
            "per_page": per_page,
        },
        "stats": pp_eis_match_stats(db, ktru_code=ktru_code, window_min=window_min),
        "flash_ok": request.session.pop("eis_pp_link_flash_ok", None),
        "flash_err": request.session.pop("eis_pp_link_flash_err", None),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("eis_pp_link.html", ctx)


@app.post("/action/eis-pp-manual-register", name="action_eis_pp_manual_register")
def action_eis_pp_manual_register(
    request: Request,
    auth: Auth,
    purchase_number: Annotated[str, Form()],
    offer_id: Annotated[str, Form()],
    line_key: Annotated[str, Form()] = "",
    link_date_msk: Annotated[str, Form()] = "",
    oktmo: Annotated[str, Form()] = "",
    analyst_note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = str(request.url_for("page_eis_pp_link"))
    pn = str(purchase_number or "").strip()
    oid = str(offer_id or "").strip()
    if not pn or not oid:
        request.session["eis_pp_link_flash_err"] = "Укажите извещение и ID ПП."
        return RedirectResponse(back, status_code=303)
    try:
        from eis_pp_manual_register import register_manual_eis_pp_pair

        result = register_manual_eis_pp_pair(
            _eis_analytics_db_path(),
            _META,
            purchase_number=pn,
            offer_price_list_item_id=oid,
            line_key=str(line_key or "").strip(),
            link_date_msk=str(link_date_msk or "").strip(),
            expected_oktmo=str(oktmo or "").strip(),
            analyst_note=str(analyst_note or "").strip(),
            analyst_user=str(auth),
        )
        detail_url = (
            str(request.url_for("page_eis_pp_link"))
            + f"?purchase_number={pn}&line_key={result.line_key}&offer_id={result.offer_price_list_item_id}"
        )
        oktmo_hint = f", ОКТМО: {result.oktmo_detail}" if result.oktmo_detail else ""
        request.session["eis_pp_link_flash_ok"] = (
            f"Пара {pn} ↔ {oid} импортирована ({result.matched_count}/{result.total_compared}, "
            f"lag {result.lag_minutes}{oktmo_hint}). "
        )
        return RedirectResponse(detail_url, status_code=303)
    except Exception as exc:
        request.session["eis_pp_link_flash_err"] = str(exc)[:500]
        return RedirectResponse(back, status_code=303)


@app.post("/action/eis-pp-link-status", name="action_eis_pp_link_status")
def action_eis_pp_link_status(
    request: Request,
    auth: Auth,
    purchase_number: Annotated[str, Form()],
    line_key: Annotated[str, Form()],
    offer_price_list_item_id: Annotated[str, Form()],
    analyst_status: Annotated[str, Form()] = "",
    current_status: Annotated[str, Form()] = "pending",
    tag_only: Annotated[str, Form()] = "",
    analyst_note: Annotated[str, Form()] = "",
    scenario_tag: Annotated[str, Form()] = "",
    participation_intent: Annotated[str, Form()] = "",
    return_ktru: Annotated[str, Form()] = "",
    return_status: Annotated[str, Form()] = "",
    return_lag: Annotated[str, Form()] = "",
) -> Any:
    if str(tag_only or "").strip():
        status_value = str(current_status or "pending").strip().lower() or "pending"
        flash_ok = "Разметка сохранена"
    else:
        status_value = str(analyst_status or "").strip().lower()
        if status_value == "confirmed":
            flash_ok = "Гипотеза подтверждена"
        elif status_value == "rejected":
            flash_ok = "Гипотеза опровергнута"
        else:
            flash_ok = "Разметка сохранена"
    ok = set_pp_eis_match_status(
        _eis_analytics_db_path(),
        purchase_number=purchase_number,
        line_key=line_key,
        offer_price_list_item_id=offer_price_list_item_id,
        status=status_value,
        analyst_user=str(auth),
        analyst_note=analyst_note,
        scenario_tag=scenario_tag,
        participation_intent=participation_intent,
    )
    if ok:
        from eis_analytics_db import append_pp_eis_event

        append_pp_eis_event(
            _eis_analytics_db_path(),
            event_type=f"link_{status_value}",
            payload={
                "purchase_number": purchase_number,
                "line_key": line_key,
                "offer_price_list_item_id": offer_price_list_item_id,
                "scenario_tag": scenario_tag,
                "participation_intent": participation_intent,
                "tag_only": bool(str(tag_only or "").strip()),
            },
        )
        request.session["eis_pp_link_flash_ok"] = flash_ok
    else:
        request.session["eis_pp_link_flash_err"] = "Не удалось сохранить статус"
    params: dict[str, str] = {
        "purchase_number": purchase_number,
        "line_key": line_key,
        "offer_id": offer_price_list_item_id,
    }
    if return_ktru:
        params["ktru"] = return_ktru
    if return_status:
        params["status"] = return_status
    if return_lag:
        params["lag"] = return_lag
    url = str(request.url_for("page_eis_pp_link")) + "?" + urlencode(params)
    return RedirectResponse(url=url, status_code=303)


@app.get("/api/eis-pp-link/stats", name="api_eis_pp_link_stats")
def api_eis_pp_link_stats(
    _: Auth,
    ktru: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    ktru_code = str(ktru or "").strip() or None
    window_min = int(pp_eis_link_window_min())
    return pp_eis_match_stats(_eis_analytics_db_path(), ktru_code=ktru_code, window_min=window_min)


@app.get("/pp-drafts", name="page_pp_drafts")
def page_pp_drafts(
    request: Request,
    _: Auth,
    page: Annotated[int, Query()] = 1,
) -> Any:
    drafts, cookie_meta = _load_pp_drafts_for_page(
        request,
        page=page,
        enrich_rts=_pp_drafts_enrich_rts_enabled(),
    )
    pagination = cookie_meta.get("pagination") if isinstance(cookie_meta.get("pagination"), dict) else {}
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Черновики ПП — RTS",
        "active": "pp_drafts",
        "paths": paths_dict(),
        "drafts": drafts,
        "cookie_meta": cookie_meta,
        "pagination": pagination,
        "admin_drafts_view": bool(cookie_meta.get("admin_view")),
        "rts_offers_base": "https://offers.rts-tender.ru",
        "pp_publish_enabled": _pp_publish_enabled(),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("pp_drafts.html", ctx)


@app.get("/preoffers", name="page_preoffers")
def page_preoffers(
    request: Request,
    _: Auth,
    edit_offer_id: Annotated[str | None, Query()] = None,
    from_purchase: Annotated[str | None, Query()] = None,
    line_key: Annotated[str | None, Query()] = None,
) -> Any:
    raw = load_preoffers_raw(_rts_preoffers_path())
    offers = [
        _preoffer_modal_payload(x)
        for x in raw.get("offers", [])
        if isinstance(x, dict)
    ]
    form = _preoffer_empty_form()
    if edit_offer_id:
        form = _preoffer_form_from_offer(preoffer_by_id(_rts_preoffers_path(), edit_offer_id))
    elif from_purchase and line_key:
        form = _preoffer_draft_from_notice_line(from_purchase.strip(), line_key.strip()) or _preoffer_empty_form()
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Справочник предложений — RTS",
        "active": "preoffers",
        "paths": paths_dict(),
        "offers": offers,
        "form": form,
        "preoffer_flash_ok": request.session.pop("preoffer_flash_ok", None),
        "preoffer_flash_err": request.session.pop("preoffer_flash_err", None),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("preoffers.html", ctx)


async def _read_registry_json_upload(
    registry_json_text: str,
    registry_file: UploadFile | None,
) -> str:
    text = (registry_json_text or "").strip()
    if registry_file and registry_file.filename:
        raw = await registry_file.read()
        if raw:
            text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise ValueError("Загрузите JSON-файл или вставьте текст ответа API реестра RTS.")
    return text


def _clear_registry_import_session(request: Request) -> None:
    old = str(request.session.pop("registry_import_token", "") or "").strip()
    if old:
        delete_registry_import_draft(_META, old)
    for key in (
        "registry_import_preview",
        "registry_import_payload",
        "registry_import_filtered",
        "registry_import_skipped",
    ):
        request.session.pop(key, None)


def _load_registry_import_draft_view(request: Request) -> dict[str, Any]:
    token = str(request.session.get("registry_import_token") or "").strip()
    if not token:
        return {"preview_rows": None, "filtered": [], "skipped": [], "import_ready": False}
    draft = load_registry_import_draft(_META, token)
    if draft is None:
        request.session.pop("registry_import_token", None)
        return {"preview_rows": None, "filtered": [], "skipped": [], "import_ready": False}
    preview = draft.get("preview")
    filtered = draft.get("filtered")
    skipped = draft.get("skipped")
    payload = draft.get("payload")
    return {
        "preview_rows": preview if isinstance(preview, list) else None,
        "filtered": filtered if isinstance(filtered, list) else [],
        "skipped": skipped if isinstance(skipped, list) else [],
        "import_ready": isinstance(payload, list) and len(payload) > 0,
    }


@app.get("/preoffers/import", name="page_preoffers_import")
def page_preoffers_import(request: Request, _: Auth) -> Any:
    draft_view = _load_registry_import_draft_view(request)
    ctx: dict[str, Any] = {
        "request": request,
        "title": "Импорт из JSON RTS — справочник",
        "active": "preoffers",
        "paths": paths_dict(),
        "preview_rows": draft_view["preview_rows"],
        "filtered_unpublished": draft_view["filtered"],
        "skipped": draft_view["skipped"],
        "import_ready": draft_view["import_ready"],
        "import_flash_ok": request.session.pop("import_flash_ok", None),
        "import_flash_err": request.session.pop("import_flash_err", None),
    }
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("preoffers_import.html", ctx)


@app.post("/action/preoffers-registry-preview", name="action_preoffers_registry_preview")
async def action_preoffers_registry_preview(request: Request, _: Auth) -> Any:
    back = request.url_for("page_preoffers_import")
    form = await request.form()
    registry_json_text = str(form.get("registry_json_text") or "")
    only_published = form.get("only_published") == "1"
    upload = form.get("registry_file")
    registry_file = upload if isinstance(upload, UploadFile) else None
    try:
        text = await _read_registry_json_upload(registry_json_text, registry_file)
        result = parse_registry_json(text, only_published=only_published)
    except ValueError as exc:
        request.session["import_flash_err"] = str(exc)[:2000]
        _clear_registry_import_session(request)
        return RedirectResponse(back, status_code=303)

    _clear_registry_import_session(request)
    token = save_registry_import_draft(
        _META,
        preview=preview_rows_from_items(result.items),
        payload=payload_from_items(result.items),
        filtered=result.filtered_unpublished,
        skipped=result.skipped,
    )
    request.session["registry_import_token"] = token
    request.session.pop("import_flash_err", None)
    request.session["import_flash_ok"] = (
        f"К импорту готово {len(result.items)} поз. из {result.total_in_file} в файле."
    )
    return RedirectResponse(back, status_code=303)


@app.post("/action/preoffers-registry-import", name="action_preoffers_registry_import")
def action_preoffers_registry_import(request: Request, _: Auth) -> RedirectResponse:
    back_preoffers = request.url_for("page_preoffers")
    back_import = request.url_for("page_preoffers_import")
    token = str(request.session.get("registry_import_token") or "").strip()
    draft = load_registry_import_draft(_META, token) if token else None
    try:
        if draft is None:
            raise ValueError("Нет данных для импорта — сначала выполните предпросмотр.")
        items = items_from_payload(draft.get("payload"))
        written, unchanged = apply_registry_import(_rts_preoffers_path(), items)
    except ValueError as exc:
        request.session["import_flash_err"] = str(exc)[:2000]
        return RedirectResponse(back_import, status_code=303)
    except Exception as exc:  # noqa: BLE001
        request.session["import_flash_err"] = f"Ошибка импорта: {exc}"[:2000]
        return RedirectResponse(back_import, status_code=303)

    _clear_registry_import_session(request)
    request.session["preoffer_flash_ok"] = (
        f"Импорт RTS: записано или обновлено {written}, без изменений {unchanged}."
    )
    return RedirectResponse(back_preoffers, status_code=303)


@app.get("/session", name="page_session")
def page_session(request: Request, _: Auth) -> Any:
    cookie_path = _cookie_path_for_request(request)
    profile = _rts_profile_for_request(request)
    ctx = {
        "request": request,
        "title": "Cookie / сессия — RTS",
        "active": "session",
        "paths": paths_dict(request),
        "masked_preview": "",
        "cookie_note": cookie_note(cookie_path),
        "cookie_status": cookie_file_status(cookie_path, _META, rts_profile=profile),
        "cookie_ingest_enabled": cookie_ingest_enabled(),
        "rts_profile": profile,
        "known_profiles": list_known_profiles(),
    }
    if "flash_ok" in request.session:
        ctx["flash_ok"] = request.session.pop("flash_ok")
    if "flash_err" in request.session:
        ctx["flash_err"] = request.session.pop("flash_err")
    ctx.update(web_auth_ui_context(request))
    return _templates.TemplateResponse("session.html", ctx)


@app.post("/action/import-ktru-cache", name="action_import_ktru_cache")
def action_import_ktru_cache(
    request: Request,
    _: Auth,
    ktru_code: Annotated[str, Form()] = "",
    ktru_json: Annotated[str, Form()] = "",
) -> RedirectResponse:
    code = ktru_code.strip()
    raw_text = ktru_json.strip()
    if not code or not raw_text:
        request.session["flash_err"] = "Укажите код КТРУ и JSON ответа Ktru/search."
        return RedirectResponse(request.url_for("page_session"), status_code=303)
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        request.session["flash_err"] = f"Некорректный JSON: {exc}"
        return RedirectResponse(request.url_for("page_session"), status_code=303)
    try:
        item = import_ktru_template(_KTRU_CACHE, code, data)
    except (ValueError, RtsPpApiError) as exc:
        request.session["flash_err"] = str(exc)[:500]
        return RedirectResponse(request.url_for("page_session"), status_code=303)
    chars = len(item.get("characteristics") or [])
    request.session["flash_ok"] = (
        f"КТРУ {item.get('code')} v{item.get('version')} — {item.get('name') or code}; "
        f"характеристик: {chars}. Кэш: {_KTRU_CACHE}"
    )
    return RedirectResponse(request.url_for("page_session"), status_code=303)


@app.post("/action/poll", name="action_poll")
def action_poll(
    request: Request,
    _: Auth,
    days_window: Annotated[int, Form()] = 90,
    rows_per_page: Annotated[int, Form()] = 50,
    enrich: Annotated[str | None, Form()] = None,
    no_notify: Annotated[str | None, Form()] = None,
    dry_run: Annotated[str | None, Form()] = None,
    print_all: Annotated[str | None, Form()] = None,
    fetch_bodies: Annotated[str | None, Form()] = None,
    lot_items: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    request.session["form_days"] = days_window
    request.session["form_rows"] = rows_per_page
    request.session["form_enrich"] = enrich == "1"
    request.session["form_no_notify"] = no_notify == "1"
    request.session["form_dry_run"] = dry_run == "1"
    request.session["form_print_all"] = print_all == "1"
    request.session["form_fetch_bodies"] = fetch_bodies == "1"
    request.session["form_lot_items"] = lot_items == "1"

    cfg = PollConfig(
        cookie_path=_cookie_path_for_request(request),
        state_path=_state_path_for_request(request),
        meta_parent=_META,
        rows_per_page=max(10, min(rows_per_page, 200)),
        days_window=max(1, min(days_window, 365)),
        enrich=enrich == "1" or lot_items == "1",
        no_notify=no_notify == "1",
        dry_run=dry_run == "1",
        print_all=print_all == "1",
        fetch_bodies_all=fetch_bodies == "1",
        fetch_lot_items=lot_items == "1",
    )
    result = run_poll(cfg)
    request.session["poll_stdout"] = result.stdout
    request.session["poll_stderr"] = result.stderr
    if fetch_bodies == "1":
        write_last_invite_bodies(
            list(result.structured.invite_bodies),
            jqgrid_records=result.structured.records_total,
            parsed_rows=result.structured.fetched_count,
        )
    else:
        clear_last_invite_bodies()
    if result.ok:
        request.session["flash_ok"] = "Опрос завершён."
    else:
        request.session["flash_err"] = (result.error or "Ошибка опроса.")[:2000]
    return RedirectResponse(request.url_for("page_dashboard"), status_code=303)


@app.post("/action/eis-poll", name="action_eis_poll")
def action_eis_poll(
    request: Request,
    _: Auth,
    eis_org_region: Annotated[str, Form()] = "",
    eis_document_type44: Annotated[str, Form()] = "epNotificationEZT2020",
    eis_ktru: Annotated[str, Form()] = "",
    eis_date: Annotated[str, Form()] = "",
    eis_timeout: Annotated[int, Form()] = 120,
    eis_dry_run: Annotated[str | None, Form()] = None,
    eis_require_ezt_xml: Annotated[str | None, Form()] = None,
    eis_notify_telegram: Annotated[str | None, Form()] = None,
    eis_no_verify: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    org = (eis_org_region or "").strip()
    doc_t = (eis_document_type44 or "epNotificationEZT2020").strip() or "epNotificationEZT2020"
    ktru_s = (eis_ktru or "").strip()
    date_s = (eis_date or "").strip()

    request.session["eis_form_org_region"] = org
    request.session["eis_form_document_type"] = doc_t
    request.session["eis_form_ktru"] = ktru_s
    request.session["eis_form_date"] = date_s
    request.session["form_eis_dry_run"] = eis_dry_run == "1"
    request.session["form_eis_require_ezt"] = eis_require_ezt_xml == "1"
    request.session["form_eis_notify_tg"] = eis_notify_telegram == "1"
    request.session["form_eis_no_verify"] = eis_no_verify == "1"

    if not org:
        request.session["eis_flash_err"] = "ЕИС: укажите код региона заказчика (orgRegion), напр. 72."
        url = request.url_for("page_dashboard")
        return RedirectResponse(f"{url}#eis-poll", status_code=303)

    tout = max(30, min(int(eis_timeout), 900))
    ssl_legacy = os.environ.get("EIS_WEB_SSL_LEGACY", "").lower() in ("1", "true", "yes")
    tls_max = os.environ.get("EIS_WEB_TLS_MAX_12", "").lower() in ("1", "true", "yes")
    legacy_int44 = os.environ.get("EIS_LEGACY_INT44", "").lower() in ("1", "true", "yes")
    service_url = os.environ.get("EIS_GET_DOCS_IP_URL", "").strip() or None

    raw_tg_env = os.environ.get("EIS_WEB_TELEGRAM_ENV", "eis").strip().lower()
    telegram_env_choice = raw_tg_env if raw_tg_env in ("eis", "rts") else "eis"

    cfg = EisPollConfig(
        env_file=_EIS_ENV,
        state_file=_EIS_STATE,
        meta_parent=_META,
        org_region=org,
        document_type_44=doc_t,
        ktru_csv=ktru_s or None,
        exact_date=date_s or None,
        timeout=float(tout),
        dry_run=eis_dry_run == "1",
        require_ezt_xml=eis_require_ezt_xml == "1",
        notify_telegram=eis_notify_telegram == "1",
        telegram_env=telegram_env_choice,
        no_verify=eis_no_verify == "1",
        ssl_legacy=ssl_legacy,
        tls_max_12=tls_max,
        legacy_int44=legacy_int44,
        service_url=service_url,
        analytics_db_name=os.environ.get("EIS_ANALYTICS_DB", DEFAULT_DB_NAME),
    )
    if eis_notify_telegram != "1":
        cfg.notify_telegram = False

    res = run_eis_poll(cfg)
    n_new = len(res.new_matches)
    if res.ok:
        request.session["eis_flash_ok"] = (
            f"ЕИС: готово. Архивов: {res.zips_downloaded}, новых по фильтру КТРУ: {n_new}, "
            f"в state закупок: {res.state_seen_count}."
        )
    else:
        err_text = "; ".join(res.errors) if res.errors else "Ошибка без текста."
        request.session["eis_flash_err"] = err_text[:2000]
    url = request.url_for("page_dashboard")
    return RedirectResponse(f"{url}#eis-poll", status_code=303)


@app.post("/action/eis-cron-start", name="action_eis_cron_start")
def action_eis_cron_start(request: Request, _: Auth) -> RedirectResponse:
    _eis_ui_cron.enabled = True
    _eis_ui_cron.interval_sec = 120
    _eis_ui_cron.org_region = "35"
    _eis_ui_cron.document_type_44 = "epNotificationEZT2020"
    _eis_ui_cron.ktru_csv = None
    _eis_ui_cron.exact_date = None
    request.session["eis_flash_ok"] = (
        "ЕИС: включён регулярный опрос из веба каждые 2 минуты "
        "(регион заказчика 35, documentType44 epNotificationEZT2020, дата — сегодня, фильтр КТРУ отключён)."
    )
    url = request.url_for("page_dashboard")
    return RedirectResponse(f"{url}#eis-poll", status_code=303)


@app.post("/action/eis-cron-stop", name="action_eis_cron_stop")
def action_eis_cron_stop(request: Request, _: Auth) -> RedirectResponse:
    _eis_ui_cron.enabled = False
    request.session["eis_flash_ok"] = "ЕИС: регулярный опрос из веба выключен."
    url = request.url_for("page_dashboard")
    return RedirectResponse(f"{url}#eis-poll", status_code=303)


@app.post("/action/eis-rss-cron-start", name="action_eis_rss_cron_start")
def action_eis_rss_cron_start(
    request: Request,
    _: Auth,
    rss_url: Annotated[str, Form()] = DEFAULT_RSS_URL,
    rss_interval_sec: Annotated[int, Form()] = 30,
    rss_query_label: Annotated[str, Form()] = DEFAULT_QUERY_LABEL,
) -> RedirectResponse:
    url = request.url_for("page_analytics")
    clean_url = (rss_url or "").strip()
    if not clean_url.startswith(("http://", "https://")):
        request.session["rss_flash_err"] = "ЕИС RSS: укажите корректный RSS URL."
        return RedirectResponse(f"{url}#eis-rss-cron", status_code=303)
    _eis_rss_cron.rss_url = clean_url
    _eis_rss_cron.interval_sec = max(30, min(int(rss_interval_sec), 3600))
    _eis_rss_cron.query_label = (rss_query_label or DEFAULT_QUERY_LABEL).strip() or DEFAULT_QUERY_LABEL
    _eis_rss_cron.enabled = True
    _save_eis_rss_settings()
    request.session["rss_flash_ok"] = (
        f"ЕИС RSS: cron включён, интервал {_eis_rss_cron.interval_sec} сек. "
        "Первый опрос начнётся в ближайшие секунды."
    )
    return RedirectResponse(f"{url}#eis-rss-cron", status_code=303)


@app.post("/action/eis-rss-cron-stop", name="action_eis_rss_cron_stop")
def action_eis_rss_cron_stop(request: Request, _: Auth) -> RedirectResponse:
    _eis_rss_cron.enabled = False
    _save_eis_rss_settings()
    request.session["rss_flash_ok"] = "ЕИС RSS: cron выключен."
    url = request.url_for("page_analytics")
    return RedirectResponse(f"{url}#eis-rss-cron", status_code=303)


@app.post("/action/preoffers-save", name="action_preoffers_save")
def action_preoffers_save(
    request: Request,
    _: Auth,
    offer_id: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    ktru_code: Annotated[str, Form()] = "",
    rts_url: Annotated[str, Form()] = "",
    sku: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    characteristics_text: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = request.url_for("page_preoffers")
    if not title.strip() or not ktru_code.strip():
        request.session["preoffer_flash_err"] = "Укажите название и КТРУ."
        return RedirectResponse(back, status_code=303)
    try:
        saved = upsert_preoffer(
            _rts_preoffers_path(),
            {
                "offer_id": offer_id.strip(),
                "title": title,
                "rts_url": rts_url,
                "sku": sku,
                "ktru_code": ktru_code,
                "enabled": enabled == "1",
                "characteristics": _parse_preoffer_characteristics_text(characteristics_text),
            },
        )
        request.session["preoffer_flash_ok"] = f"Предложение сохранено: {saved['title']}"
    except Exception as exc:  # noqa: BLE001
        request.session["preoffer_flash_err"] = f"Не удалось сохранить предложение: {exc}"[:2000]
    return RedirectResponse(back, status_code=303)


@app.post("/action/preoffers-disable", name="action_preoffers_disable")
def action_preoffers_disable(
    request: Request,
    _: Auth,
    offer_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = request.url_for("page_preoffers")
    if disable_preoffer(_rts_preoffers_path(), offer_id):
        request.session["preoffer_flash_ok"] = "Предложение отключено."
    else:
        request.session["preoffer_flash_err"] = "Предложение не найдено."
    return RedirectResponse(back, status_code=303)


@app.post("/action/preoffers-delete", name="action_preoffers_delete")
def action_preoffers_delete(
    request: Request,
    _: Auth,
    offer_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = request.url_for("page_preoffers")
    if delete_preoffer(_rts_preoffers_path(), offer_id):
        request.session["preoffer_flash_ok"] = "Предложение удалено из справочника."
    else:
        request.session["preoffer_flash_err"] = "Предложение не найдено."
    return RedirectResponse(back, status_code=303)


@app.post("/action/eis-notice-html-fetch", name="action_eis_notice_html_fetch")
def action_eis_notice_html_fetch(
    request: Request,
    _: Auth,
    purchase_number: Annotated[str, Form()] = "",
    source_url: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "shelf",
) -> RedirectResponse:
    pn = (purchase_number or "").strip()
    url_s = (source_url or "").strip()
    if return_to == "day_summary":
        back = request.url_for("page_day_summary")
    elif return_to == "offer_matching":
        back = request.url_for("page_offer_matching")
    else:
        back = request.url_for("page_shelf_purchases")
    if not pn or not url_s:
        request.session["html_flash_err"] = "ЕИС HTML: нет номера закупки или ссылки на карточку."
        return RedirectResponse(back, status_code=303)
    try:
        lines_count, chars_count = _fetch_and_store_notice_html(pn, url_s, timeout=30.0)
        try:
            _run_auto_participate_after_html()
        except Exception:
            _LOG_WEB.exception("Auto participate after manual HTML fetch")
        try:
            _notify_preoffer_watch_matches()
        except Exception:
            _LOG_WEB.exception("Telegram preoffer watch after manual HTML fetch")
        request.session["html_flash_ok"] = (
            f"ЕИС HTML: карточка {pn} загружена. Позиций: {lines_count}, "
            f"характеристик: {chars_count}."
        )
    except Exception as exc:  # noqa: BLE001
        record_notice_html_page(
            _eis_analytics_db_path(),
            purchase_number=pn,
            source_url=url_s,
            html_text="",
            lines=[],
            fetched_at_utc=datetime.now(timezone.utc),
            ok=False,
            http_status=None,
            error_text=str(exc)[:2000],
        )
        request.session["html_flash_err"] = f"ЕИС HTML: ошибка загрузки карточки {pn}: {exc}"[:2000]
    return RedirectResponse(back, status_code=303)


@app.post("/action/save-cookie", name="action_save_cookie")
def action_save_cookie(
    request: Request,
    _: Auth,
    cookie_body: Annotated[str, Form()] = "",
) -> RedirectResponse:
    cookie_path = _cookie_path_for_request(request)
    try:
        save_cookie_to_file(cookie_path, cookie_body)
        request.session["flash_ok"] = f"Cookie сохранён ({_rts_profile_for_request(request) or 'default'})."
    except ValueError as e:
        request.session["flash_err"] = str(e)
    except OSError as e:
        request.session["flash_err"] = f"Не удалось записать файл: {e}"
    return RedirectResponse(request.url_for("page_session"), status_code=303)


@app.post("/api/rts/cookie/save", name="api_rts_cookie_save")
async def api_rts_cookie_save(request: Request) -> dict[str, Any]:
    """Приём cookie от расширения RTS Cookie Bridge (заголовок api-key)."""
    if not cookie_ingest_enabled():
        raise HTTPException(
            status_code=503,
            detail="Приём cookie отключён: задайте RTS_COOKIE_INGEST_KEY на сервере.",
        )
    api_key = (request.headers.get("api-key") or "").strip()
    if not validate_ingest_api_key(api_key):
        raise HTTPException(status_code=403, detail="Неверный api-key.")
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ожидается JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект.")
    probe = body.get("probe", True) is not False
    source = str(body.get("source") or "extension").strip() or "extension"
    try:
        profile = validate_rts_profile(str(body.get("profile") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cookie_path = _cookie_path_for_profile(profile)
    try:
        return save_cookie_from_ingest(
            cookie_path=cookie_path,
            meta_dir=_META,
            body=body,
            source=source,
            probe=probe,
            rts_profile=profile,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Не удалось записать cookie: {exc}") from exc


@app.get("/api/rts/cookie/status", name="api_rts_cookie_status")
def api_rts_cookie_status(request: Request, _: Auth) -> dict[str, Any]:
    """Статус cookie для веб-консоли (требует сессию оператора)."""
    profile = _rts_profile_for_request(request)
    return cookie_file_status(_cookie_path_for_request(request), _META, rts_profile=profile)


@app.get("/api/eis-cron-status", name="api_eis_cron_status")
def api_eis_cron_status(_: Auth) -> dict[str, Any]:
    now = time.time()
    nxt = _eis_next_poll_unix
    ui_on = _eis_ui_cron.enabled
    env_org = os.environ.get("EIS_SCHEDULE_ORG_REGION", "").strip()
    env_on = _env_schedule_poll_flag() and bool(env_org)
    if ui_on:
        interval_vis = max(30, int(_eis_ui_cron.interval_sec))
    elif env_on:
        interval_vis = max(30, int(os.environ.get("EIS_SCHEDULE_INTERVAL_SEC", "120")))
    else:
        interval_vis = None

    seconds_left: int | None
    if _eis_polling_now or nxt is None:
        seconds_left = None
    elif nxt <= now:
        seconds_left = 0
    else:
        seconds_left = max(0, int(nxt - now))

    active_source: str | None = "ui" if ui_on else ("env" if env_on else None)
    return {
        "ui_cron_enabled": ui_on,
        "env_schedule_ready": env_on,
        "active_source": active_source,
        "schedule_interval_sec": interval_vis,
        "polling": _eis_polling_now,
        "next_poll_unix": nxt,
        "seconds_until_next": seconds_left,
    }


def _eis_rss_cron_status_payload() -> dict[str, Any]:
    now = time.time()
    nxt = _eis_rss_next_poll_unix
    if _eis_rss_polling_now or nxt is None:
        seconds_left = None
    elif nxt <= now:
        seconds_left = 0
    else:
        seconds_left = max(0, int(nxt - now))
    last = last_rss_poll_run(_eis_analytics_db_path())
    return {
        "enabled": _eis_rss_cron.enabled,
        "polling": _eis_rss_polling_now,
        "interval_sec": int(_eis_rss_cron.interval_sec),
        "rss_url": _eis_rss_cron.rss_url,
        "query_label": _eis_rss_cron.query_label,
        "next_poll_unix": nxt,
        "seconds_until_next": seconds_left,
        "last_run": last,
    }


def _rts_pp_monitor_cron_status_payload() -> dict[str, Any]:
    if cron_mode_enabled():
        payload = cron_status_payload(now=time.time())
        # Keep legacy keys expected by existing UI templates.
        payload.setdefault("interval_min", max(1, int(payload.get("interval_sec") or 420) // 60))
        if _pp_monitor_bg_poll_active:
            payload["polling"] = True
            payload["seconds_until_next"] = None
        return payload
    now = time.time()
    nxt = _rts_pp_monitor_next_poll_unix
    settings = load_pp_monitor_settings(_pp_monitor_settings_path())
    env_on = _env_bool("RTS_PP_MONITOR", False)
    enabled = env_on and bool(settings.get("enabled"))
    interval_min = int(settings.get("poll_interval_min") or 20)
    try:
        env_interval = int(os.environ.get("RTS_PP_MONITOR_INTERVAL_MIN", "0"))
        if env_interval > 0:
            interval_min = env_interval
    except ValueError:
        pass
    if _rts_pp_monitor_polling_now:
        seconds_left: int | None = None
    elif not enabled or nxt is None:
        seconds_left = None
    elif nxt <= now:
        seconds_left = 0
    else:
        seconds_left = max(0, int(nxt - now))
    return {
        "enabled": enabled,
        "env_monitor": env_on,
        "settings_enabled": bool(settings.get("enabled")),
        "polling": _rts_pp_monitor_polling_now,
        "interval_min": interval_min,
        "next_poll_unix": nxt,
        "seconds_until_next": seconds_left,
    }


@app.get("/api/rts-pp-monitor-cron-status", name="api_rts_pp_monitor_cron_status")
def api_rts_pp_monitor_cron_status(_: Auth) -> dict[str, Any]:
    _maybe_trigger_overdue_pp_monitor_poll()
    return _rts_pp_monitor_cron_status_payload()


@app.get("/api/eis-rss-cron-status", name="api_eis_rss_cron_status")
def api_eis_rss_cron_status(_: Auth) -> dict[str, Any]:
    return _eis_rss_cron_status_payload()


@app.get("/api/eis-rss-compare-rows", name="api_eis_rss_compare_rows")
def api_eis_rss_compare_rows(_: Auth) -> dict[str, Any]:
    return {
        "rows": rss_vs_getdocs_recent(_eis_analytics_db_path(), limit=30),
        "last_run": last_rss_poll_run(_eis_analytics_db_path()),
        "status": _eis_rss_cron_status_payload(),
    }


@app.get("/api/eis-notice-html/{purchase_number}", name="api_eis_notice_html")
def api_eis_notice_html(purchase_number: str, _: Auth) -> dict[str, Any]:
    return notice_html_payload(_eis_analytics_db_path(), purchase_number.strip())


@app.get("/api/offer-matching/{purchase_number}", name="api_offer_matching")
def api_offer_matching(
    request: Request,
    purchase_number: str,
    _: Auth,
    line_key: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    pn = purchase_number.strip()
    lk = (line_key or "").strip() or None
    rows = offer_matching_ktru_rows(
        _eis_analytics_db_path(),
        limit=100,
        purchase_number=pn,
        line_key=lk,
    )
    positions = _offer_match_payload_for_lines(rows)
    db = _eis_analytics_db_path()
    positions = sync_offer_matching_lines(
        db,
        meta_dir=_META,
        lines=positions,
        fetch_timeout=25.0,
        run_pp_match=True,
        refresh_outcome=True,
    )
    status = "loaded" if any(p.get("html_status") == "loaded" for p in positions) else ("not_loaded" if positions else "not_found")
    rts_pp_draft = None
    if lk:
        profile = _rts_profile_for_request(request)
        rts_pp_draft = _rts_pp_draft_api_payload(
            get_rts_pp_draft(_eis_analytics_db_path(), pn, lk, rts_profile=profile)
        )
    return {
        "purchase_number": pn,
        "status": status,
        "positions": positions,
        "preoffers_count": len(_load_rts_preoffers()),
        "rts_pp_draft": rts_pp_draft,
        "participation_price_ord_enabled": _participation_price_ord_enabled(),
    }


@app.post("/api/eis-pp/manual-register", name="api_eis_pp_manual_register")
async def api_eis_pp_manual_register(request: Request, auth: Auth) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ожидается JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект.")
    pn = str(body.get("purchase_number") or "").strip()
    lk = str(body.get("line_key") or "").strip()
    oid = str(body.get("offer_id") or body.get("offer_price_list_item_id") or "").strip()
    if not pn or not oid:
        raise HTTPException(status_code=400, detail="Укажите purchase_number и offer_id.")
    try:
        from eis_pp_manual_register import register_manual_eis_pp_pair

        result = register_manual_eis_pp_pair(
            _eis_analytics_db_path(),
            _META,
            purchase_number=pn,
            offer_price_list_item_id=oid,
            line_key=lk,
            link_date_msk=str(body.get("link_date_msk") or "").strip(),
            expected_oktmo=str(body.get("oktmo") or "").strip(),
            analyst_note=str(body.get("analyst_note") or "из модалки подбора предложений").strip(),
            analyst_user=str(auth),
            source="manual_offer_matching",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc
    detail_url = (
        str(request.url_for("page_eis_pp_link"))
        + f"?purchase_number={quote_plus(pn)}&line_key={quote_plus(result.line_key)}"
        + f"&offer_id={quote_plus(result.offer_price_list_item_id)}"
    )
    payload = result.as_dict()
    payload.update(
        {
            "ok": True,
            "eis_pp_link_url": detail_url,
            "pp_offer_url": f"https://offers.rts-tender.ru/customer/offers/offer-item/{result.offer_price_list_item_id}",
            "lag_label": format_lag_minutes(result.lag_minutes) if result.lag_minutes is not None else "—",
        }
    )
    return payload


@app.post("/api/rts/pp/draft-from-notice", name="api_rts_pp_draft_from_notice")
async def api_rts_pp_draft_from_notice(request: Request, _: Auth) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ожидается JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект.")
    created_by = str(request.session.get(SESSION_USER_KEY) or "").strip()
    profile = _require_operator_rts_profile(request)
    return _rts_pp_draft_from_notice(
        purchase_number=str(body.get("purchase_number") or ""),
        line_key=str(body.get("line_key") or ""),
        rts_profile=profile,
        cookie_path=_cookie_path_for_request(request),
        created_by=created_by,
    )


@app.post("/api/rts/pp/draft-from-monitor", name="api_rts_pp_draft_from_monitor")
async def api_rts_pp_draft_from_monitor(request: Request, _: Auth) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ожидается JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект.")
    created_by = str(request.session.get(SESSION_USER_KEY) or "").strip()
    profile = _require_operator_rts_profile(request)
    offer_id = str(body.get("offer_price_list_item_id") or "")
    cookie_path = _cookie_path_for_request(request)
    _LOG_WEB.info(
        "draft-from-monitor start offer_id=%s profile=%s",
        offer_id,
        profile,
    )
    result = await asyncio.to_thread(
        create_pp_draft_from_monitor,
        analytics_db=_eis_analytics_db_path(),
        meta_dir=_META,
        preoffers_path=_rts_preoffers_path(),
        cookie_path=cookie_path,
        offer_price_list_item_id=offer_id,
        rts_profile=profile,
        created_by=created_by,
        price_ord_enabled=_participation_price_ord_enabled(),
    )
    if not result.ok and not result.already_existed:
        _LOG_WEB.warning(
            "draft-from-monitor failed offer_id=%s profile=%s reason=%s",
            offer_id,
            profile,
            result.reason or result.error,
        )
        raise HTTPException(status_code=result.http_status, detail=result.error)
    _LOG_WEB.info(
        "draft-from-monitor ok offer_id=%s profile=%s draft_id=%s existed=%s",
        offer_id,
        profile,
        result.rts_draft_id,
        result.already_existed,
    )
    return result.as_api_dict()


@app.get("/api/rts/pp/monitor-draft-verify", name="api_rts_pp_monitor_draft_verify")
def api_rts_pp_monitor_draft_verify(
    request: Request,
    _: Auth,
    offer_id: Annotated[str, Query()],
) -> dict[str, Any]:
    oid = str(offer_id or "").strip()
    if not oid:
        raise HTTPException(status_code=400, detail="Укажите offer_id.")
    profile = _rts_profile_for_request(request)
    row = get_pp_monitor_draft(_eis_analytics_db_path(), oid, rts_profile=profile)
    if row is None:
        raise HTTPException(status_code=404, detail="Черновик для образца не найден.")
    verify_report: dict[str, Any] = {}
    raw = str(row.get("verify_json") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                verify_report = parsed
        except json.JSONDecodeError:
            pass
    draft_id = row.get("rts_draft_id")
    finding = get_pp_monitor_finding(_eis_analytics_db_path(), oid)
    find_item = finding.get("find") if finding and isinstance(finding.get("find"), dict) else {}
    if draft_id and find_item:
        try:
            from monitor_find_to_draft import compare_find_to_draft
            from rts_pp_api import get_draft, load_offers_cookie, validate_cookie_session

            cookie = load_offers_cookie(_cookie_path_for_request(request))
            validate_cookie_session(cookie)
            item = get_draft(cookie, int(draft_id), timeout=20)
            verify_report = compare_find_to_draft(find_item, item)
        except Exception:
            pass
    return {
        "offer_price_list_item_id": oid,
        "rts_profile": profile,
        "verify_report": verify_report,
    }


@app.get("/api/rts/pp/draft-verify", name="api_rts_pp_draft_verify")
def api_rts_pp_draft_verify(
    request: Request,
    _: Auth,
    purchase_number: Annotated[str, Query()],
    line_key: Annotated[str, Query()],
) -> dict[str, Any]:
    verify_report = _draft_verify_for_line(
        purchase_number=purchase_number,
        line_key=line_key,
        rts_profile=_rts_profile_for_request(request),
        cookie_path=_cookie_path_for_request(request),
    )
    return {
        "purchase_number": purchase_number.strip(),
        "line_key": line_key.strip(),
        "verify_report": verify_report,
    }


@app.post("/api/rts/pp/drafts/delete", name="api_rts_pp_draft_delete")
async def api_rts_pp_draft_delete(request: Request, _: Auth) -> dict[str, Any]:
    """Drop local PP draft record so «Участвовать» can create again (RTS LK unchanged)."""
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ожидается JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект.")
    pn = str(body.get("purchase_number") or "").strip()
    lk = str(body.get("line_key") or "").strip()
    if not pn or not lk:
        raise HTTPException(status_code=400, detail="Укажите purchase_number и line_key.")
    profile = _draft_rts_profile_for_action(
        request,
        str(body.get("rts_profile") or "").strip() or None,
    )
    db_path = _eis_analytics_db_path()
    if is_monitor_draft_line_key(lk):
        oid = monitor_draft_offer_id_from_line_key(lk) or pn
        deleted = delete_pp_monitor_draft(db_path, oid, rts_profile=profile)
    else:
        deleted = delete_rts_pp_draft(db_path, pn, lk, rts_profile=profile)
    if not deleted:
        raise HTTPException(status_code=404, detail="Запись черновика не найдена.")
    return {"ok": True, "purchase_number": pn, "line_key": lk}


def _raise_publish_result(result: Any) -> dict[str, Any]:
    if not result.ok and result.http_status >= 400:
        raise HTTPException(status_code=result.http_status, detail=result.error or result.reason or "error")
    return result.as_api_dict()


@app.post("/api/rts/pp/publish/start", name="api_rts_pp_publish_start")
async def api_rts_pp_publish_start(request: Request, _: Auth) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ожидается JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект.")
    profile = _require_operator_rts_profile(request)
    try:
        draft_id = int(body.get("rts_draft_id") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="rts_draft_id должен быть числом.") from exc
    force_new = bool(body.get("force_new") or body.get("retry"))
    result = await asyncio.to_thread(
        start_publish_job,
        analytics_db=_eis_analytics_db_path(),
        cookie_path=_cookie_path_for_request(request),
        rts_draft_id=draft_id,
        rts_profile=profile,
        source=str(body.get("source") or "").strip(),
        source_key=str(body.get("source_key") or "").strip(),
        force_new=force_new,
    )
    return _raise_publish_result(result)


@app.post("/api/rts/pp/publish/submit-signatures", name="api_rts_pp_publish_submit_signatures")
async def api_rts_pp_publish_submit_signatures(request: Request, _: Auth) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ожидается JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект.")
    profile = _require_operator_rts_profile(request)
    try:
        job_id = int(body.get("job_id") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="job_id должен быть числом.") from exc
    signatures = body.get("signatures")
    if not isinstance(signatures, list):
        raise HTTPException(status_code=400, detail="Ожидается массив signatures.")
    result = await asyncio.to_thread(
        submit_publish_signatures,
        analytics_db=_eis_analytics_db_path(),
        cookie_path=_cookie_path_for_request(request),
        job_id=job_id,
        signatures=signatures,
        rts_profile=profile,
    )
    return _raise_publish_result(result)


@app.get("/api/rts/pp/publish/status", name="api_rts_pp_publish_status")
def api_rts_pp_publish_status(
    request: Request,
    _: Auth,
    job_id: Annotated[int, Query()],
) -> dict[str, Any]:
    profile = _require_operator_rts_profile(request)
    result = get_publish_job_status(
        analytics_db=_eis_analytics_db_path(),
        job_id=int(job_id),
        rts_profile=profile,
    )
    return _raise_publish_result(result)


@app.post("/api/rts/pp/publish/cancel", name="api_rts_pp_publish_cancel")
async def api_rts_pp_publish_cancel(request: Request, _: Auth) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ожидается JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект.")
    profile = _require_operator_rts_profile(request)
    try:
        job_id = int(body.get("job_id") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="job_id должен быть числом.") from exc
    result = cancel_publish_job(
        analytics_db=_eis_analytics_db_path(),
        job_id=job_id,
        rts_profile=profile,
    )
    return _raise_publish_result(result)


@app.get("/api/eis-compare-rows", name="api_eis_compare_rows")
def api_eis_compare_rows(
    _: Auth,
    line_key: Annotated[str | None, Query()] = None,
    purchase: Annotated[str | None, Query()] = None,
    ktru: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    lk = (line_key or "").strip()
    pn_q = (purchase or "").strip()
    kt_q = (ktru or "").strip()
    if not lk and not (pn_q and kt_q):
        raise HTTPException(
            status_code=400,
            detail="Укажите line_key или пару purchase + ktru.",
        )
    snap = read_last_eis_poll()
    if snap is None:
        raise HTTPException(status_code=404, detail="Нет сохранённого last_eis_poll.json — выполните опрос ЕИС.")
    raw_lines = snap.get("customer_characteristics")
    if not isinstance(raw_lines, list):
        raise HTTPException(
            status_code=404,
            detail="В последнем опросе ещё нет блока customer_characteristics (нужна свежая выгрузка).",
        )
    picked: dict[str, Any] | None = None
    if lk:
        for ln in raw_lines:
            if isinstance(ln, dict) and str(ln.get("line_key") or "").strip() == lk:
                picked = ln
                break
    elif pn_q and kt_q:
        for ln in raw_lines:
            if not isinstance(ln, dict):
                continue
            if str(ln.get("purchase_number") or "").strip() != pn_q:
                continue
            if str(ln.get("ktru_code") or "").strip() != kt_q:
                continue
            picked = ln
            break
    if picked is None:
        raise HTTPException(
            status_code=404,
            detail="Строка не найдена в последнем опросе (возможно, журнал старше выгрузки — запустите опрос ЕИС снова).",
        )
    ktru_c = str(picked.get("ktru_code") or "").strip()

    cust_flat: list[dict[str, str]] = []
    chs = picked.get("characteristics")
    if isinstance(chs, list):
        for c in chs:
            if not isinstance(c, dict):
                continue
            cust_flat.append(
                {
                    "match_key": str(c.get("match_key") or ""),
                    "display_label": str(c.get("display_label") or ""),
                    "value_display": str(c.get("value_display") or ""),
                }
            )

    supplier_rows = _eis_supplier_chars_stub.get_for_ktru(ktru_c)
    cmp_out, supplier_empty = build_comparison_rows(cust_flat, supplier_rows)

    pos_name = str(picked.get("position_display_name") or "").strip()
    price_u = str(picked.get("price_per_unit_display") or "").strip()
    notice_tot = str(picked.get("notice_total_display") or "").strip()

    cust_org = str(picked.get("customer_organization_display") or "").strip()

    return {
        "purchase_number": picked.get("purchase_number"),
        "ktru_code": ktru_c,
        "line_key": picked.get("line_key"),
        "supplier_placeholder": supplier_empty,
        "parse_phase2_note": snap.get("parse_phase2_note") if isinstance(snap.get("parse_phase2_note"), str) else "",
        "comparison_rows": [r.as_dict() for r in cmp_out],
        "modal_header": {
            "position_display_name": pos_name or "—",
            "price_per_unit": price_u or "—",
            "notice_total_max_price": notice_tot or "—",
            "ktru_code": ktru_c or "—",
            "purchase_number": str(picked.get("purchase_number") or "").strip() or "—",
            "customer_organization_display": cust_org or "—",
        },
    }


@app.get("/api/status", name="api_status")
def api_status(request: Request, _: Auth) -> dict[str, Any]:
    state_path = _state_path_for_request(request)
    cookie_path = _cookie_path_for_request(request)
    seen, st = load_state(state_path)
    eis_seen = load_eis_state(_EIS_STATE)
    return {
        "paths": paths_dict(request),
        "cookie_exists": cookie_path.is_file(),
        "rts_profile": _rts_profile_for_request(request),
        "state": {"count": len(seen), "updated_at": st.get("updated_at") if isinstance(st, dict) else None},
        "last_poll": read_last_poll(),
        "eis": {
            "state_count": len(eis_seen),
            "env_exists": _EIS_ENV.is_file(),
            "last": read_last_eis_poll(),
        },
    }

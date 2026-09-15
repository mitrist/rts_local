"""Automatic RTS PP draft creation after EIS HTML fetch."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from auto_participate_settings import (
    effective_min_amount,
    global_enabled,
    load_settings as load_auto_participate_settings,
    profile_rules,
    settings_path as auto_participate_settings_path,
)
from eis_analytics_db import (
    auto_participate_candidates,
    count_auto_participate_created_since,
    get_auto_participate_attempt,
    record_auto_participate_attempt,
)
from notification_settings import passes_min_purchase_filter
from offer_matcher import rank_offer_matches
from rts_cookie_profiles import cookie_path_for_profile, list_known_profiles, normalize_rts_profile
from rts_ktru_cache import cache_dir_default, is_ktru_template_cached
from rts_pp_api import load_offers_cookie, validate_cookie_session
from rts_pp_draft_service import create_pp_draft_from_notice
from rts_preoffers import RtsPreoffer, load_preoffers, preoffers_by_ktru

_LOG = logging.getLogger("auto_participate")

_COOKIE_ALERT_COOLDOWN_SEC = 3600
_last_cookie_alert_at: dict[str, float] = {}


@dataclass
class AutoParticipateSummary:
    profiles_checked: int = 0
    created: int = 0
    already_existed: int = 0
    errors: int = 0
    cookie_expired: int = 0
    skipped_rules: int = 0
    skipped_blocked: int = 0
    cookie_alerts: list[str] = field(default_factory=list)


def auto_participate_enabled() -> bool:
    return os.environ.get("RTS_AUTO_PARTICIPATE", "").strip().lower() in ("1", "true", "yes", "on")


def row_matches_rules(
    row: dict[str, Any],
    *,
    rules: dict[str, Any],
    min_amount: float | None,
    offers: list[RtsPreoffer],
    ktru_cache_dir: Path,
) -> tuple[bool, str]:
    if not rules.get("enabled"):
        return False, "disabled"
    ktru = str(row.get("ktru_code") or "").strip()
    if not ktru:
        return False, "no_ktru"
    if not passes_min_purchase_filter(row, min_amount):
        return False, "min_amount"
    mode = str(rules.get("ktru_mode") or "catalog").strip().lower()
    if mode == "whitelist":
        allowed = {str(x).strip() for x in (rules.get("ktru_codes") or []) if str(x).strip()}
        if ktru not in allowed:
            return False, "ktru_whitelist"
    else:
        catalog = {o.ktru_code for o in offers if o.enabled and o.ktru_code}
        if ktru not in catalog:
            return False, "ktru_catalog"
    if not is_ktru_template_cached(ktru_cache_dir, ktru):
        return False, "ktru_template"
    min_ratio = float(rules.get("min_match_ratio") or 0.0)
    if min_ratio > 0:
        matching = preoffers_by_ktru(offers, ktru)
        if not matching:
            return False, "no_preoffer"
        results = rank_offer_matches(row.get("characteristics") or [], matching)
        best = results[0] if results else None
        if not best:
            return False, "no_match"
        required = int(best.required or 0)
        matched = int(best.matched or 0)
        ratio = (matched / required) if required > 0 else 0.0
        if ratio < min_ratio:
            return False, "match_ratio"
    return True, ""


def _validate_profile_cookie(cookie_path: Path) -> tuple[bool, str]:
    if not cookie_path.is_file():
        return False, "Файл cookie не найден."
    try:
        cookie = load_offers_cookie(cookie_path)
        validate_cookie_session(cookie)
        return True, ""
    except Exception as exc:
        return False, str(exc)[:500]


def _record_auto_skip(
    analytics_db: Path,
    *,
    purchase_number: str,
    line_key: str,
    rts_profile: str,
    ktru_code: str,
    status: str,
    error_text: str,
) -> bool:
    """Записать пропуск/ошибку, если успешная попытка ещё не зафиксирована."""
    existing = get_auto_participate_attempt(
        analytics_db,
        purchase_number,
        line_key,
        rts_profile=rts_profile,
    )
    if existing and existing.get("status") == "created":
        return False
    record_auto_participate_attempt(
        analytics_db,
        purchase_number=purchase_number,
        line_key=line_key,
        rts_profile=rts_profile,
        status=status,
        ktru_code=ktru_code,
        error_text=error_text,
    )
    return True


def _should_send_cookie_alert(profile: str) -> bool:
    now = time.time()
    last = _last_cookie_alert_at.get(profile, 0.0)
    if now - last < _COOKIE_ALERT_COOLDOWN_SEC:
        return False
    _last_cookie_alert_at[profile] = now
    return True


def run_auto_participate_after_html(
    *,
    analytics_db: Path,
    history_db: Path,
    meta_dir: Path,
    project_root: Path,
    preoffers_path: Path,
    price_ord_enabled: bool = True,
    notify_cookie_alert: Callable[[str, str], None] | None = None,
) -> AutoParticipateSummary:
    summary = AutoParticipateSummary()
    if not auto_participate_enabled():
        return summary

    settings = load_auto_participate_settings(auto_participate_settings_path(meta_dir))
    if not global_enabled(settings):
        return summary
    ktru_cache_dir = cache_dir_default(meta_dir)
    offers = load_preoffers(preoffers_path)

    try:
        limit_candidates = max(1, min(int(os.environ.get("RTS_AUTO_PARTICIPATE_MAX_PER_RUN", "20")), 100))
    except ValueError:
        limit_candidates = 20
    try:
        delay = max(0.0, min(float(os.environ.get("RTS_AUTO_PARTICIPATE_DELAY_SEC", "1.5")), 10.0))
    except ValueError:
        delay = 1.5

    for profile in list_known_profiles():
        rules = profile_rules(settings, profile)
        if not rules.get("enabled"):
            continue
        summary.profiles_checked += 1
        cookie_path = cookie_path_for_profile(profile, meta_dir, project_root)
        cookie_ok, cookie_err = _validate_profile_cookie(cookie_path)
        if not cookie_ok:
            summary.cookie_expired += 1
            if notify_cookie_alert and _should_send_cookie_alert(profile):
                msg = f"Авто-участие ({profile}): cookie недоступен — {cookie_err}"
                summary.cookie_alerts.append(msg)
                try:
                    notify_cookie_alert(profile, msg)
                except Exception:
                    _LOG.exception("Auto participate cookie alert failed for %s", profile)

        min_amount = effective_min_amount(rules)
        max_hour = int(rules.get("max_per_hour") or 10)
        since = datetime.now(timezone.utc) - timedelta(hours=1)

        candidates = auto_participate_candidates(
            analytics_db,
            limit=limit_candidates,
            rts_profile=profile,
        )
        created_this_run = 0
        for idx, row in enumerate(candidates):
            ok, skip_reason = row_matches_rules(
                row,
                rules=rules,
                min_amount=min_amount,
                offers=offers,
                ktru_cache_dir=ktru_cache_dir,
            )
            if not ok:
                summary.skipped_rules += 1
                continue

            pn = str(row.get("purchase_number") or "").strip()
            lk = str(row.get("line_key") or "").strip()
            ktru = str(row.get("ktru_code") or "").strip()
            if not pn or not lk:
                continue

            if not cookie_ok:
                if _record_auto_skip(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    ktru_code=ktru,
                    status="cookie_expired",
                    error_text=cookie_err,
                ):
                    summary.skipped_blocked += 1
                continue

            if count_auto_participate_created_since(analytics_db, rts_profile=profile, since_utc=since) >= max_hour:
                limit_msg = f"Достигнут лимит {max_hour} черновиков в час для профиля {profile}."
                if _record_auto_skip(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    ktru_code=ktru,
                    status="skipped",
                    error_text=limit_msg,
                ):
                    summary.skipped_blocked += 1
                continue

            if idx > 0 and delay > 0 and created_this_run > 0:
                time.sleep(delay)

            result = create_pp_draft_from_notice(
                analytics_db=analytics_db,
                history_db=history_db,
                meta_dir=meta_dir,
                preoffers_path=preoffers_path,
                cookie_path=cookie_path,
                purchase_number=pn,
                line_key=lk,
                rts_profile=profile,
                created_by="auto",
                price_ord_enabled=price_ord_enabled,
            )

            if result.already_existed and result.ok:
                summary.already_existed += 1
                record_auto_participate_attempt(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    status="created",
                    ktru_code=ktru,
                    rts_draft_id=result.rts_draft_id,
                    edit_url=result.edit_url,
                )
                continue

            if result.ok:
                summary.created += 1
                created_this_run += 1
                record_auto_participate_attempt(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    status="created",
                    ktru_code=ktru,
                    rts_draft_id=result.rts_draft_id,
                    edit_url=result.edit_url,
                )
                _LOG.info(
                    "Auto participate: created draft %s for %s/%s profile=%s",
                    result.rts_draft_id,
                    pn,
                    lk,
                    profile,
                )
            else:
                summary.errors += 1
                status = "cookie_expired" if result.reason in ("no_cookie", "session_expired") else "error"
                record_auto_participate_attempt(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    status=status,
                    ktru_code=ktru,
                    error_text=result.error,
                )
                _LOG.warning(
                    "Auto participate: failed %s/%s profile=%s: %s",
                    pn,
                    lk,
                    profile,
                    result.error,
                )

    return summary

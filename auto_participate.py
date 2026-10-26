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
    allowed_etp_names,
    effective_min_amount,
    etp_name_allowed,
    global_enabled,
    load_settings as load_auto_participate_settings,
    profile_rules,
    settings_path as auto_participate_settings_path,
)
from eis_analytics_db import (
    auto_participate_candidates,
    auto_participate_rss_candidates,
    count_auto_participate_created_since,
    get_auto_participate_attempt,
    list_auto_participate_attempts,
    record_auto_participate_attempt,
    rts_synthetic_line_key,
)
from notification_settings import passes_min_purchase_filter
from offer_matcher import rank_offer_matches
from rts_cookie_profiles import cookie_path_for_profile, list_known_profiles, normalize_rts_profile
from rts_pp_api import load_offers_cookie, validate_cookie_session
from rts_pp_draft_service import create_pp_draft_from_notice, create_pp_draft_from_rss_notice
from rts_pp_publish_service import publish_enabled, start_publish_job
from rts_preoffers import RtsPreoffer, load_preoffers, preoffers_by_ktru

_LOG = logging.getLogger("auto_participate")
AUTO_PARTICIPATE_PUBLISH_SOURCE = "notice"

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
    publish_prepared: int = 0
    publish_errors: int = 0
    cookie_alerts: list[str] = field(default_factory=list)


def auto_participate_enabled() -> bool:
    return os.environ.get("RTS_AUTO_PARTICIPATE", "").strip().lower() in ("1", "true", "yes", "on")


def row_matches_rules(
    row: dict[str, Any],
    *,
    rules: dict[str, Any],
    min_amount: float | None,
    offers: list[RtsPreoffer],
    ktru_cache_dir: Path | None = None,
    allowed_etp: list[str] | None = None,
    allow_empty_etp: bool = False,
) -> tuple[bool, str]:
    del ktru_cache_dir  # notice-path uses RTS creation-data; local KTRU template not required
    if not rules.get("enabled"):
        return False, "disabled"
    etp = str(row.get("etp_name") or "").strip()
    if etp or not allow_empty_etp:
        if not etp_name_allowed(etp, allowed_etp):
            return False, "etp"
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
    min_ratio = float(rules.get("min_match_ratio") or 0.0)
    chars = row.get("characteristics") or []
    if min_ratio > 0 and chars:
        matching = preoffers_by_ktru(offers, ktru)
        if not matching:
            return False, "no_preoffer"
        results = rank_offer_matches(chars, matching)
        best = results[0] if results else None
        if not best:
            return False, "no_match"
        required = int(best.required or 0)
        matched = int(best.matched or 0)
        ratio = (matched / required) if required > 0 else 0.0
        if ratio < min_ratio:
            return False, "match_ratio"
    return True, ""


def _allowed_ktru_for_rules(rules: dict[str, Any], offers: list[RtsPreoffer]) -> set[str]:
    mode = str(rules.get("ktru_mode") or "catalog").strip().lower()
    if mode == "whitelist":
        return {str(x).strip() for x in (rules.get("ktru_codes") or []) if str(x).strip()}
    return {o.ktru_code for o in offers if o.enabled and o.ktru_code}


def _validate_profile_cookie(cookie_path: Path) -> tuple[bool, str]:
    if not cookie_path.is_file():
        return False, "Файл cookie не найден."
    try:
        cookie = load_offers_cookie(cookie_path)
        validate_cookie_session(cookie)
        return True, ""
    except Exception as exc:
        return False, str(exc)[:500]


def _maybe_start_auto_publish(
    *,
    analytics_db: Path,
    cookie_path: Path,
    rules: dict[str, Any],
    profile: str,
    purchase_number: str,
    line_key: str,
    rts_draft_id: int | None,
    summary: AutoParticipateSummary,
) -> str:
    """After draft: prepare publish job if auto_publish on. Returns attempt status."""
    if not rules.get("auto_publish"):
        return "created"
    if not publish_enabled():
        summary.publish_errors += 1
        return "created"
    if not rts_draft_id:
        return "created"
    try:
        pub = start_publish_job(
            analytics_db=analytics_db,
            cookie_path=cookie_path,
            rts_draft_id=int(rts_draft_id),
            rts_profile=profile,
            source=AUTO_PARTICIPATE_PUBLISH_SOURCE,
            source_key=f"{purchase_number}|{line_key}",
            force_new=False,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("auto_participate publish prepare crashed draft=%s", rts_draft_id)
        summary.publish_errors += 1
        return "created"
    if pub.ok and str(pub.status or "") == "awaiting_signature":
        summary.publish_prepared += 1
        return "awaiting_signature"
    if pub.ok and str(pub.status or "") == "published":
        summary.publish_prepared += 1
        return "published"
    summary.publish_errors += 1
    _LOG.warning(
        "auto_participate publish prepare failed draft=%s: %s",
        rts_draft_id,
        pub.error or pub.reason,
    )
    return "created"


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
    if existing and existing.get("status") in ("created", "awaiting_signature", "published"):
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


def notice_has_auto_participate_ktru(
    ktru_codes: list[str] | set[str],
    *,
    settings: dict[str, Any],
    offers: list[RtsPreoffer],
) -> bool:
    """True if any enabled profile could match at least one KTRU (catalog/whitelist)."""
    codes = {str(c).strip() for c in ktru_codes if str(c or "").strip()}
    if not codes:
        return False
    if not global_enabled(settings):
        return False
    catalog = {o.ktru_code for o in offers if o.enabled and o.ktru_code}
    for profile in list_known_profiles():
        rules = profile_rules(settings, profile)
        if not rules.get("enabled"):
            continue
        mode = str(rules.get("ktru_mode") or "catalog").strip().lower()
        if mode == "whitelist":
            allowed = {str(x).strip() for x in (rules.get("ktru_codes") or []) if str(x).strip()}
            if codes & allowed:
                return True
        elif codes & catalog:
            return True
    return False


def run_auto_participate_after_html(
    *,
    analytics_db: Path,
    history_db: Path,
    meta_dir: Path,
    project_root: Path,
    preoffers_path: Path,
    price_ord_enabled: bool = True,
    notify_cookie_alert: Callable[[str, str], None] | None = None,
    purchase_number: str | None = None,
    amount_floor: float | None = None,
) -> AutoParticipateSummary:
    summary = AutoParticipateSummary()
    if not auto_participate_enabled():
        return summary

    settings = load_auto_participate_settings(auto_participate_settings_path(meta_dir))
    if not global_enabled(settings):
        return summary
    etp_allow = allowed_etp_names(settings)
    offers = load_preoffers(preoffers_path)
    focus_pn = str(purchase_number or "").strip() or None
    floor: float | None = None
    if amount_floor is not None:
        try:
            floor_val = float(amount_floor)
            if floor_val > 0:
                floor = floor_val
        except (TypeError, ValueError):
            floor = None

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
        if floor is not None:
            min_amount = floor if min_amount is None else max(float(min_amount), floor)
        max_hour = int(rules.get("max_per_hour") or 10)
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        allowed_ktru = _allowed_ktru_for_rules(rules, offers)
        created_this_run = 0

        # Fast path: RSS without HTML → probe RTS creation-data.
        rss_candidates = auto_participate_rss_candidates(
            analytics_db,
            limit=limit_candidates,
            rts_profile=profile,
            purchase_number=focus_pn,
            min_price=min_amount,
        )
        for idx, row in enumerate(rss_candidates):
            if not rules.get("enabled"):
                break
            etp = str(row.get("etp_name") or "").strip()
            if etp and not etp_name_allowed(etp, etp_allow):
                summary.skipped_rules += 1
                continue
            if not allowed_ktru:
                summary.skipped_rules += 1
                continue
            pn = str(row.get("purchase_number") or "").strip()
            if not pn:
                continue
            if not cookie_ok:
                placeholder_lk = f"{pn}|unknown|rts:pending"
                if _record_auto_skip(
                    analytics_db,
                    purchase_number=pn,
                    line_key=placeholder_lk,
                    rts_profile=profile,
                    ktru_code="",
                    status="cookie_expired",
                    error_text=cookie_err,
                ):
                    summary.skipped_blocked += 1
                continue
            if count_auto_participate_created_since(analytics_db, rts_profile=profile, since_utc=since) >= max_hour:
                summary.skipped_blocked += 1
                continue
            if idx > 0 and delay > 0 and created_this_run > 0:
                time.sleep(delay)
            result = create_pp_draft_from_rss_notice(
                analytics_db=analytics_db,
                meta_dir=meta_dir,
                preoffers_path=preoffers_path,
                cookie_path=cookie_path,
                purchase_number=pn,
                rts_profile=profile,
                created_by="auto_rss",
                allowed_ktru=allowed_ktru,
                rss_meta=row,
            )
            star = str((result.fill_report or {}).get("star_notification_number") or "")
            ktru = str(result.ktru_code or "")
            lk = (
                rts_synthetic_line_key(pn, ktru, star)
                if ktru and star
                else f"{pn}|unknown|rts:pending"
            )
            if result.already_existed and result.ok:
                summary.already_existed += 1
                q_status = _maybe_start_auto_publish(
                    analytics_db=analytics_db,
                    cookie_path=cookie_path,
                    rules=rules,
                    profile=profile,
                    purchase_number=pn,
                    line_key=lk,
                    rts_draft_id=result.rts_draft_id,
                    summary=summary,
                )
                record_auto_participate_attempt(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    status=q_status,
                    ktru_code=ktru,
                    rts_draft_id=result.rts_draft_id,
                    edit_url=result.edit_url,
                )
                continue
            if result.ok:
                summary.created += 1
                created_this_run += 1
                q_status = _maybe_start_auto_publish(
                    analytics_db=analytics_db,
                    cookie_path=cookie_path,
                    rules=rules,
                    profile=profile,
                    purchase_number=pn,
                    line_key=lk,
                    rts_draft_id=result.rts_draft_id,
                    summary=summary,
                )
                record_auto_participate_attempt(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    status=q_status,
                    ktru_code=ktru,
                    rts_draft_id=result.rts_draft_id,
                    edit_url=result.edit_url,
                )
                _LOG.info(
                    "Auto participate RSS: created draft %s for %s/%s profile=%s status=%s",
                    result.rts_draft_id,
                    pn,
                    lk,
                    profile,
                    q_status,
                )
            else:
                if result.reason in ("not_eligible", "star_mismatch"):
                    summary.skipped_rules += 1
                    continue
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
                    "Auto participate RSS: failed %s profile=%s: %s",
                    pn,
                    profile,
                    result.error,
                )

        candidates = auto_participate_candidates(
            analytics_db,
            limit=limit_candidates,
            rts_profile=profile,
            purchase_number=focus_pn,
        )
        for idx, row in enumerate(candidates):
            ok, skip_reason = row_matches_rules(
                row,
                rules=rules,
                min_amount=min_amount,
                offers=offers,
                allowed_etp=etp_allow,
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
                q_status = _maybe_start_auto_publish(
                    analytics_db=analytics_db,
                    cookie_path=cookie_path,
                    rules=rules,
                    profile=profile,
                    purchase_number=pn,
                    line_key=lk,
                    rts_draft_id=result.rts_draft_id,
                    summary=summary,
                )
                record_auto_participate_attempt(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    status=q_status,
                    ktru_code=ktru,
                    rts_draft_id=result.rts_draft_id,
                    edit_url=result.edit_url,
                )
                continue

            if result.ok:
                summary.created += 1
                created_this_run += 1
                q_status = _maybe_start_auto_publish(
                    analytics_db=analytics_db,
                    cookie_path=cookie_path,
                    rules=rules,
                    profile=profile,
                    purchase_number=pn,
                    line_key=lk,
                    rts_draft_id=result.rts_draft_id,
                    summary=summary,
                )
                record_auto_participate_attempt(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    status=q_status,
                    ktru_code=ktru,
                    rts_draft_id=result.rts_draft_id,
                    edit_url=result.edit_url,
                )
                _LOG.info(
                    "Auto participate: created draft %s for %s/%s profile=%s status=%s",
                    result.rts_draft_id,
                    pn,
                    lk,
                    profile,
                    q_status,
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

        # Catch-up: drafts already recorded as «created» still need publish prepare
        # when auto_publish was turned on later (candidates skip existing attempts).
        if rules.get("auto_publish") and cookie_ok and publish_enabled():
            for attempt in list_auto_participate_attempts(
                analytics_db, limit=50, rts_profile=profile
            ):
                if str(attempt.get("status") or "") != "created":
                    continue
                draft_id = attempt.get("rts_draft_id")
                if not draft_id:
                    continue
                pn = str(attempt.get("purchase_number") or "").strip()
                lk = str(attempt.get("line_key") or "").strip()
                if not pn or not lk:
                    continue
                if focus_pn and pn != focus_pn:
                    continue
                q_status = _maybe_start_auto_publish(
                    analytics_db=analytics_db,
                    cookie_path=cookie_path,
                    rules=rules,
                    profile=profile,
                    purchase_number=pn,
                    line_key=lk,
                    rts_draft_id=int(draft_id),
                    summary=summary,
                )
                if q_status == "created":
                    continue
                record_auto_participate_attempt(
                    analytics_db,
                    purchase_number=pn,
                    line_key=lk,
                    rts_profile=profile,
                    status=q_status,
                    ktru_code=str(attempt.get("ktru_code") or ""),
                    rts_draft_id=int(draft_id),
                    edit_url=str(attempt.get("edit_url") or ""),
                )
                _LOG.info(
                    "Auto participate: publish catch-up draft %s for %s/%s profile=%s status=%s",
                    draft_id,
                    pn,
                    lk,
                    profile,
                    q_status,
                )

    return summary

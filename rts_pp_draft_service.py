"""Create RTS PP drafts from EIS notice lines (shared by web UI and auto-participate)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("rts_pp_draft_service")

from eis_analytics_db import (
    get_pp_monitor_draft,
    get_pp_monitor_finding,
    get_pp_monitor_registry_row,
    get_rts_pp_draft,
    has_created_pp_draft_for_ktru,
    notice_line_for_pp,
    rts_synthetic_line_key,
    save_creation_data_snapshot,
    save_html_creation_diffs,
    save_pp_monitor_draft,
    save_rts_pp_draft,
    upsert_pp_monitor_finding,
)
from monitor_find_to_draft import (
    apply_find_delivery,
    apply_find_to_draft_item,
    build_find_create_payload,
    compare_find_to_draft,
    competitor_draft_unit_price,
    competitor_unit_price_amount,
)
from notice_rts_temporary import (
    assert_creation_data_matches_ktru,
    build_temporary_create_payload,
    creation_data_ktru_code,
    okpd2_for_creation,
)
from offer_matcher import rank_offer_matches
from participation_price import (
    compute_participation_economics,
    get_price_ord_catalog,
    parse_money_amount,
    participation_economics_disabled,
)
from rts_customer_pp_api import RtsCustomerPpApiError, find_offer_item, load_customer_cookie, pp_monitor_card_url
from rts_cookie_profiles import normalize_rts_profile
from rts_ktru_cache import cache_dir_default, is_ktru_template_cached
from ktru_country_origin import default_country_origin_path, resolve_country_for_draft
from rts_pp_api import (
    RtsPpApiError,
    OFFERS_DRAFTS_URL,
    apply_draft_defaults,
    create_and_save_pp_draft,
    create_draft,
    create_temporary_offer,
    draft_edit_url,
    draft_search_by_id,
    fetch_national_mode_info_types,
    fetch_offer_creation_data,
    get_draft,
    load_offers_cookie,
    notice_offer_edit_url,
    resolve_ktru_for_create,
    resolve_offer_creation_data,
    star_notification_number,
    update_draft,
    validate_cookie_session,
)
from rts_pp_api import _fix_characteristic_value_ids
from rts_preoffers import RtsPreoffer, load_preoffers, preoffers_by_ktru


@dataclass(slots=True)
class DraftResult:
    ok: bool = False
    already_existed: bool = False
    error: str = ""
    reason: str = ""
    http_status: int = 400
    rts_draft_id: int | None = None
    protected_id: str = ""
    edit_url: str = ""
    ktru_code: str = ""
    preoffer_title: str = ""
    offer_id: str = ""
    fill_report: dict[str, Any] = field(default_factory=dict)
    verify_report: dict[str, Any] = field(default_factory=dict)
    drafts_list_url: str = ""
    registry_warning: str = ""
    purchase_number: str = ""
    line_key: str = ""

    def as_api_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "already_existed": self.already_existed,
            "rts_draft_id": self.rts_draft_id,
            "protected_id": self.protected_id,
            "edit_url": self.edit_url,
            "ktru_code": self.ktru_code,
            "preoffer_title": self.preoffer_title,
            "offer_id": self.offer_id,
        }
        if self.fill_report:
            out["fill_report"] = self.fill_report
        if self.verify_report:
            out["verify_report"] = self.verify_report
        if self.drafts_list_url:
            out["drafts_list_url"] = self.drafts_list_url
        if self.registry_warning:
            out["registry_warning"] = self.registry_warning
        if self.purchase_number:
            out["purchase_number"] = self.purchase_number
        if self.line_key:
            out["line_key"] = self.line_key
        return out


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def pp_participate_eligibility(
    ktru_code: str,
    *,
    preoffers: list[RtsPreoffer],
    ktru_cache_dir: Path,
    require_pp_template: bool = True,
) -> dict[str, Any]:
    """Gate for creating PP.

    ``require_pp_template`` — local draft_ktru_dict cache. Needed for monitor/HTML
    draft path; for EIS notice RTS creation-data path pass False (snapshot is on RTS).
    """
    code = str(ktru_code or "").strip()
    catalog_codes = {o.ktru_code for o in preoffers if o.enabled and o.ktru_code}
    in_catalog = code in catalog_codes
    in_pp_template = is_ktru_template_cached(ktru_cache_dir, code) if code else False
    require_catalog = _env_bool("RTS_PP_PARTICIPATE_REQUIRE_PREOFFER_CATALOG", default=True)
    if require_catalog:
        can = bool(code and in_catalog and (in_pp_template if require_pp_template else True))
    else:
        can = bool(code and (in_pp_template if require_pp_template else True))
    reason = ""
    if code and require_catalog and not in_catalog:
        reason = "КТРУ нет в справочнике предложений."
    elif code and require_pp_template and not in_pp_template:
        reason = "КТРУ нет в справочнике шаблонов ПП (draft_ktru_dict)."
    return {
        "can_participate": can,
        "participate_reason": reason,
        "in_preoffer_catalog": in_catalog,
        "in_pp_template": in_pp_template,
    }


def participation_economics_for_line(
    line: dict[str, Any],
    *,
    price_ord_enabled: bool,
) -> dict[str, Any]:
    if not price_ord_enabled:
        return participation_economics_disabled()
    catalog = get_price_ord_catalog()
    # Economics can use line fields even when HTML gate is skipped (RTS create path).
    return compute_participation_economics(
        ktru_code=str(line.get("ktru_code") or ""),
        delivery_place=str(line.get("delivery_place") or ""),
        region=str(line.get("region") or ""),
        price_per_unit_text=str(line.get("price_per_unit_text") or ""),
        quantity_text=str(line.get("quantity_text") or ""),
        line_sum_text=str(line.get("line_sum_text") or line.get("price_text") or ""),
        catalog=catalog,
    )


def _persist_creation_data_calibration(
    analytics_db: Path,
    *,
    purchase_number: str,
    line_key: str,
    star: str,
    creation_data: dict[str, Any],
    html_chars: list[dict[str, Any]] | None = None,
    ktru_code: str = "",
    meta_dir: Path | None = None,
) -> None:
    """Best-effort: store creation-data snapshot and HTML diff for calibration."""
    try:
        save_creation_data_snapshot(
            analytics_db,
            purchase_number=purchase_number,
            star=star,
            creation_data=creation_data,
            source="pp_create",
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("creation-data snapshot save failed pn=%s", purchase_number)
        return
    chars = list(html_chars or [])
    code = str(ktru_code or "").strip()
    lk = str(line_key or "").strip()
    if not chars and lk:
        try:
            line = notice_line_for_pp(analytics_db, purchase_number, lk)
            if line:
                chars = list(line.get("characteristics") or [])
                if not code:
                    code = str(line.get("ktru_code") or "")
        except Exception:  # noqa: BLE001
            chars = []
    if not chars and code:
        try:
            from eis_analytics_db import list_collector_candidates

            cands = list_collector_candidates(
                analytics_db,
                limit=1,
                purchase_number=purchase_number,
                ktru_code=code,
            )
            if cands:
                chars = list(cands[0].get("characteristics") or [])
                if not lk:
                    lk = str(cands[0].get("line_key") or "")
        except Exception:  # noqa: BLE001
            chars = []
    if not chars:
        return
    ktru_item = None
    try:
        if meta_dir and code:
            from rts_ktru_cache import cache_dir_default, load_cached_template

            ktru_item = load_cached_template(cache_dir_default(meta_dir), code, ttl_sec=0)
    except Exception:  # noqa: BLE001
        ktru_item = None
    try:
        from html_creation_compare import compare_html_to_creation_data

        report = compare_html_to_creation_data(chars, creation_data, ktru_item=ktru_item)
        save_html_creation_diffs(
            analytics_db,
            purchase_number=purchase_number,
            line_key=lk or rts_synthetic_line_key(purchase_number, code, star),
            star=star,
            rows=report.get("rows") or [],
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("html↔creation-data diff save failed pn=%s", purchase_number)


def create_pp_draft_from_notice(
    *,
    analytics_db: Path,
    history_db: Path,
    meta_dir: Path,
    preoffers_path: Path,
    cookie_path: Path,
    purchase_number: str,
    line_key: str,
    rts_profile: str = "",
    created_by: str = "",
    price_ord_enabled: bool | None = None,
) -> DraftResult:
    """Create Temporary PP from EIS notice via RTS creation-data (not HTML chars)."""
    del history_db  # kept for call-site compatibility; chars come from RTS
    pn = purchase_number.strip()
    lk = line_key.strip()
    profile = normalize_rts_profile(rts_profile)
    if not pn or not lk:
        return DraftResult(error="Укажите purchase_number и line_key.", reason="bad_request", http_status=400)

    existing = get_rts_pp_draft(analytics_db, pn, lk, rts_profile=profile)
    if existing and existing.get("status") == "created" and existing.get("rts_draft_id"):
        return DraftResult(
            ok=True,
            already_existed=True,
            rts_draft_id=int(existing["rts_draft_id"]),
            protected_id=str(existing.get("protected_id") or ""),
            edit_url=str(existing.get("edit_url") or ""),
            ktru_code=str(existing.get("ktru_code") or ""),
            offer_id=str(existing.get("offer_id") or ""),
        )

    line = notice_line_for_pp(analytics_db, pn, lk)
    if not line:
        return DraftResult(error="Позиция не найдена в базе.", reason="not_found", http_status=404)

    ktru_code = str(line.get("ktru_code") or "").strip()
    if not ktru_code:
        return DraftResult(error="У позиции нет кода КТРУ.", reason="no_ktru", http_status=400)

    ktru_cache = cache_dir_default(meta_dir)
    offers = load_preoffers(preoffers_path)
    participate = pp_participate_eligibility(
        ktru_code,
        preoffers=offers,
        ktru_cache_dir=ktru_cache,
        require_pp_template=False,
    )
    if not participate["can_participate"]:
        return DraftResult(
            error=participate["participate_reason"] or "Участие недоступно для этого КТРУ.",
            reason="not_eligible",
            http_status=400,
            ktru_code=ktru_code,
        )

    matching = preoffers_by_ktru(offers, ktru_code)
    # Soft metadata only — characteristics come from RTS creation-data.
    best = None
    if matching:
        results = rank_offer_matches(line.get("characteristics") or [], matching)
        best = results[0] if results else matching[0]
    preoffer_title = getattr(best, "title", "") if best else ""
    offer_id = getattr(best, "offer_id", "") if best else ""

    ord_enabled = price_ord_enabled if price_ord_enabled is not None else _env_bool(
        "RTS_PARTICIPATION_PRICE_ORD_ENABLED", default=True
    )
    economics = participation_economics_for_line(line, price_ord_enabled=ord_enabled)
    unit_price_override = None
    if economics.get("available") and economics.get("unit_price_for_draft"):
        unit_price_override = str(economics["unit_price_for_draft"]).strip() or None

    try:
        preferred_lot = int(line.get("position_index") or 0) + 1
    except (TypeError, ValueError):
        preferred_lot = 1
    preferred_lot = max(1, preferred_lot)
    star = star_notification_number(pn, lot_index=preferred_lot)
    edit_url = notice_offer_edit_url(pn, star=star)
    rss_first_seen = str(line.get("first_seen_at_utc") or "")
    pub_date = str(line.get("pub_date_utc") or "")
    creation_data_ok_at = ""
    t0 = time.monotonic()

    try:
        cookie = load_offers_cookie(cookie_path)
        validate_cookie_session(cookie)
        creation_data, star = resolve_offer_creation_data(
            cookie,
            pn,
            expected_ktru=ktru_code,
            preferred_lot_index=preferred_lot,
        )
        creation_data_ok_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        assert_creation_data_matches_ktru(creation_data, ktru_code)
        _persist_creation_data_calibration(
            analytics_db,
            purchase_number=pn,
            line_key=lk,
            star=star,
            creation_data=creation_data,
            html_chars=line.get("characteristics") or [],
            ktru_code=ktru_code,
            meta_dir=meta_dir,
        )
        edit_url = notice_offer_edit_url(pn, star=star)
        okpd2 = okpd2_for_creation(creation_data)
        info_types: dict[str, Any] = {}
        try:
            info_types = fetch_national_mode_info_types(cookie, okpd2)
        except RtsPpApiError:
            info_types = {}
        country_origin_path = default_country_origin_path(Path(__file__).resolve().parent)
        country_title = (
            preoffer_title
            or str(creation_data.get("ktruName") or "").strip()
            or str(line.get("position_display_name") or "").strip()
        )
        country = resolve_country_for_draft(
            ktru_code=ktru_code,
            title=country_title,
            path=country_origin_path,
        )
        payload = build_temporary_create_payload(
            creation_data,
            unit_price_override=unit_price_override,
            info_types_payload=info_types or None,
            country=country,
        )
        draft_id = create_temporary_offer(cookie, payload)
    except RtsPpApiError as exc:
        if exc.reason in ("no_cookie", "session_expired"):
            return DraftResult(
                error=str(exc),
                reason=exc.reason,
                http_status=503,
                ktru_code=ktru_code,
            )
        if exc.reason == "star_mismatch":
            return DraftResult(
                error=str(exc),
                reason="star_mismatch",
                http_status=409,
                ktru_code=ktru_code,
            )
        save_rts_pp_draft(
            analytics_db,
            purchase_number=pn,
            line_key=lk,
            ktru_code=ktru_code,
            rts_draft_id=None,
            offer_id=offer_id,
            status="error",
            error_text=str(exc),
            created_by=created_by,
            rts_profile=profile,
        )
        return DraftResult(
            error=str(exc),
            reason=exc.reason or "api_error",
            http_status=502,
            ktru_code=ktru_code,
        )

    draft_created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fill_report = {
        "source": "rts_creation_data",
        "star_notification_number": star,
        "ktru_code": ktru_code,
        "unit_price_override": unit_price_override or "",
        "country_code": country.code,
        "country_name": country.name,
        "rss_first_seen_at": rss_first_seen,
        "pub_date_utc": pub_date,
        "creation_data_ok_at": creation_data_ok_at,
        "draft_created_at": draft_created_at,
        "html_present": True,
        "preferred_lot_index": preferred_lot,
    }
    verify_report = {
        "source": "rts_creation_data",
        "ktru_matched": True,
        "star_notification_number": star,
    }
    protected_id = ""
    try:
        cookie = load_offers_cookie(cookie_path)
        row = draft_search_by_id(cookie, draft_id, poll_retries=2, poll_delay=0.5)
        if isinstance(row, dict):
            protected_id = str(row.get("protectedId") or row.get("protected_id") or "").strip()
            if protected_id:
                edit_url = draft_edit_url(protected_id, draft_id=draft_id)
    except Exception:  # noqa: BLE001
        protected_id = ""

    save_rts_pp_draft(
        analytics_db,
        purchase_number=pn,
        line_key=lk,
        ktru_code=ktru_code,
        rts_draft_id=int(draft_id),
        protected_id=protected_id,
        edit_url=edit_url,
        offer_id=offer_id,
        status="created",
        created_by=created_by,
        rts_profile=profile,
        fill_report_json=json.dumps(fill_report, ensure_ascii=False),
        verify_json=json.dumps(verify_report, ensure_ascii=False),
    )
    lag_rss = _lag_sec(rss_first_seen, draft_created_at)
    lag_pub = _lag_sec(pub_date, draft_created_at)
    _LOG.info(
        "PP create ok pn=%s star=%s ktru=%s html_present=1 lag_rss_to_create_sec=%s lag_pub_to_create_sec=%s elapsed_sec=%.2f",
        pn,
        star,
        ktru_code,
        lag_rss if lag_rss is not None else "n/a",
        lag_pub if lag_pub is not None else "n/a",
        time.monotonic() - t0,
    )
    return DraftResult(
        ok=True,
        rts_draft_id=int(draft_id),
        protected_id=protected_id,
        edit_url=edit_url,
        drafts_list_url=OFFERS_DRAFTS_URL,
        ktru_code=ktru_code,
        preoffer_title=preoffer_title or "",
        offer_id=offer_id or "",
        fill_report=fill_report,
        verify_report=verify_report,
    )


def _lag_sec(start_iso: str, end_iso: str) -> float | None:
    def _parse(s: str) -> datetime | None:
        raw = str(s or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    a = _parse(start_iso)
    b = _parse(end_iso)
    if not a or not b:
        return None
    return round((b - a).total_seconds(), 1)


def create_pp_draft_from_rss_notice(
    *,
    analytics_db: Path,
    meta_dir: Path,
    preoffers_path: Path,
    cookie_path: Path,
    purchase_number: str,
    rts_profile: str = "",
    created_by: str = "",
    allowed_ktru: set[str] | None = None,
    rss_meta: dict[str, Any] | None = None,
) -> DraftResult:
    """Create Temporary PP from RSS notice via RTS creation-data probe (no EIS HTML)."""
    pn = str(purchase_number or "").strip()
    profile = normalize_rts_profile(rts_profile)
    if not pn:
        return DraftResult(error="Укажите purchase_number.", reason="bad_request", http_status=400)

    offers = load_preoffers(preoffers_path)
    catalog = {o.ktru_code for o in offers if o.enabled and o.ktru_code}
    allowed = set(allowed_ktru) if allowed_ktru is not None else catalog
    allowed = {str(x).strip() for x in allowed if str(x).strip()}
    if not allowed:
        return DraftResult(
            error="Нет разрешённых КТРУ для RSS fast-path.",
            reason="not_eligible",
            http_status=400,
        )

    meta = dict(rss_meta or {})
    rss_first_seen = str(meta.get("first_seen_at_utc") or "")
    pub_date = str(meta.get("pub_date_utc") or "")
    t0 = time.monotonic()

    try:
        cookie = load_offers_cookie(cookie_path)
        validate_cookie_session(cookie)
        creation_data, star = resolve_offer_creation_data(
            cookie,
            pn,
            allowed_ktru=allowed,
        )
        creation_data_ok_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ktru_code = creation_data_ktru_code(creation_data)
        if not ktru_code or ktru_code not in allowed:
            return DraftResult(
                error="creation-data КТРУ не в каталоге/whitelist.",
                reason="not_eligible",
                http_status=400,
                ktru_code=ktru_code,
            )
        if has_created_pp_draft_for_ktru(
            analytics_db,
            purchase_number=pn,
            ktru_code=ktru_code,
            rts_profile=profile,
        ):
            return DraftResult(
                ok=True,
                already_existed=True,
                ktru_code=ktru_code,
                reason="already_existed",
            )
        lk = rts_synthetic_line_key(pn, ktru_code, star)
        _persist_creation_data_calibration(
            analytics_db,
            purchase_number=pn,
            line_key=lk,
            star=star,
            creation_data=creation_data,
            ktru_code=ktru_code,
            meta_dir=meta_dir,
        )
        existing = get_rts_pp_draft(analytics_db, pn, lk, rts_profile=profile)
        if existing and existing.get("status") == "created" and existing.get("rts_draft_id"):
            return DraftResult(
                ok=True,
                already_existed=True,
                rts_draft_id=int(existing["rts_draft_id"]),
                protected_id=str(existing.get("protected_id") or ""),
                edit_url=str(existing.get("edit_url") or ""),
                ktru_code=ktru_code,
                offer_id=str(existing.get("offer_id") or ""),
            )

        matching = preoffers_by_ktru(offers, ktru_code)
        best = matching[0] if matching else None
        preoffer_title = getattr(best, "title", "") if best else ""
        offer_id = getattr(best, "offer_id", "") if best else ""
        edit_url = notice_offer_edit_url(pn, star=star)

        okpd2 = okpd2_for_creation(creation_data)
        info_types: dict[str, Any] = {}
        try:
            info_types = fetch_national_mode_info_types(cookie, okpd2)
        except RtsPpApiError:
            info_types = {}
        country_origin_path = default_country_origin_path(Path(__file__).resolve().parent)
        country_title = (
            preoffer_title
            or str(creation_data.get("ktruName") or "").strip()
        )
        country = resolve_country_for_draft(
            ktru_code=ktru_code,
            title=country_title,
            path=country_origin_path,
        )
        payload = build_temporary_create_payload(
            creation_data,
            unit_price_override=None,
            info_types_payload=info_types or None,
            country=country,
        )
        draft_id = create_temporary_offer(cookie, payload)
    except RtsPpApiError as exc:
        if exc.reason in ("no_cookie", "session_expired"):
            return DraftResult(
                error=str(exc),
                reason=exc.reason,
                http_status=503,
            )
        if exc.reason == "star_mismatch":
            return DraftResult(
                error=str(exc),
                reason="star_mismatch",
                http_status=409,
            )
        return DraftResult(
            error=str(exc),
            reason=exc.reason or "api_error",
            http_status=502,
        )

    draft_created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fill_report = {
        "source": "rts_creation_data_rss",
        "star_notification_number": star,
        "ktru_code": ktru_code,
        "country_code": country.code,
        "country_name": country.name,
        "rss_first_seen_at": rss_first_seen,
        "pub_date_utc": pub_date,
        "creation_data_ok_at": creation_data_ok_at,
        "draft_created_at": draft_created_at,
        "html_present": False,
    }
    verify_report = {
        "source": "rts_creation_data_rss",
        "ktru_matched": True,
        "star_notification_number": star,
    }
    protected_id = ""
    try:
        cookie = load_offers_cookie(cookie_path)
        row = draft_search_by_id(cookie, draft_id, poll_retries=2, poll_delay=0.5)
        if isinstance(row, dict):
            protected_id = str(row.get("protectedId") or row.get("protected_id") or "").strip()
            if protected_id:
                edit_url = draft_edit_url(protected_id, draft_id=draft_id)
    except Exception:  # noqa: BLE001
        protected_id = ""

    save_rts_pp_draft(
        analytics_db,
        purchase_number=pn,
        line_key=lk,
        ktru_code=ktru_code,
        rts_draft_id=int(draft_id),
        protected_id=protected_id,
        edit_url=edit_url,
        offer_id=offer_id,
        status="created",
        created_by=created_by,
        rts_profile=profile,
        fill_report_json=json.dumps(fill_report, ensure_ascii=False),
        verify_json=json.dumps(verify_report, ensure_ascii=False),
    )
    lag_rss = _lag_sec(rss_first_seen, draft_created_at)
    lag_pub = _lag_sec(pub_date, draft_created_at)
    _LOG.info(
        "PP create ok pn=%s star=%s ktru=%s html_present=0 lag_rss_to_create_sec=%s lag_pub_to_create_sec=%s elapsed_sec=%.2f",
        pn,
        star,
        ktru_code,
        lag_rss if lag_rss is not None else "n/a",
        lag_pub if lag_pub is not None else "n/a",
        time.monotonic() - t0,
    )
    return DraftResult(
        ok=True,
        rts_draft_id=int(draft_id),
        protected_id=protected_id,
        edit_url=edit_url,
        drafts_list_url=OFFERS_DRAFTS_URL,
        ktru_code=ktru_code,
        preoffer_title=preoffer_title or "",
        offer_id=offer_id or "",
        fill_report=fill_report,
        verify_report=verify_report,
    )


def _ensure_monitor_finding(
    analytics_db: Path,
    meta_dir: Path,
    offer_price_list_item_id: str,
) -> dict[str, Any] | None:
    finding = get_pp_monitor_finding(analytics_db, offer_price_list_item_id)
    if finding:
        return finding
    registry = get_pp_monitor_registry_row(analytics_db, offer_price_list_item_id)
    if not registry:
        return None
    try:
        customer_cookie = load_customer_cookie(meta_dir)
        find_item = find_offer_item(customer_cookie, offer_price_list_item_id)
    except RtsCustomerPpApiError:
        return None
    ktru_obj = find_item.get("ktru") if isinstance(find_item.get("ktru"), dict) else {}
    ktru_from_find = str(ktru_obj.get("code") or "")
    upsert_pp_monitor_finding(
        analytics_db,
        offer_price_list_item_id=offer_price_list_item_id,
        find_json=json.dumps(find_item, ensure_ascii=False),
        ktru_code=str(registry.get("ktru_code") or ktru_from_find),
        trade_mark=str(registry.get("trade_mark") or find_item.get("tradeMark") or ""),
        link_date=str(registry.get("link_date") or find_item.get("offerPriceListItemLinkDate") or ""),
        unit_price=registry.get("unit_price"),
        card_url=str(registry.get("card_url") or pp_monitor_card_url(offer_price_list_item_id)),
        source="draft",
    )
    return get_pp_monitor_finding(analytics_db, offer_price_list_item_id)


def _duplicate_draft_message(ktru_code: str, exc: BaseException | None = None) -> str:
    raw = str(exc or "").casefold()
    if "характеристик" in raw:
        return (
            f"На РТС уже есть позиция по КТРУ {ktru_code} с таким же набором характеристик. "
            "Этот вариант ядра после сопоставления совпадает с уже созданным черновиком или ПП."
        )
    return (
        f"На РТС уже есть черновик по КТРУ {ktru_code}. "
        "Удалите дубликат в списке черновиков ЛК РТС и повторите, "
        "или откройте существующий черновик для редактирования."
    )


def _is_duplicate_draft_error(exc: RtsPpApiError) -> bool:
    text = str(exc).casefold()
    return "уже существует" in text or "already exists" in text


def _save_monitor_draft_record(
    analytics_db: Path,
    *,
    offer_price_list_item_id: str,
    ktru_code: str,
    rts_draft_id: int | None,
    protected_id: str = "",
    edit_url: str = "",
    offer_id: str = "",
    status: str,
    error_text: str = "",
    created_by: str = "",
    rts_profile: str = "",
    fill_report: dict[str, Any] | None = None,
    verify_report: dict[str, Any] | None = None,
    draft_unit_price: float | None = None,
    source_unit_price: float | None = None,
) -> None:
    save_pp_monitor_draft(
        analytics_db,
        offer_price_list_item_id=offer_price_list_item_id,
        ktru_code=ktru_code,
        rts_draft_id=rts_draft_id,
        protected_id=protected_id,
        edit_url=edit_url,
        offer_id=offer_id,
        status=status,
        error_text=error_text,
        created_by=created_by,
        rts_profile=rts_profile,
        fill_report_json=json.dumps(fill_report, ensure_ascii=False) if fill_report else "",
        verify_json=json.dumps(verify_report, ensure_ascii=False) if verify_report else "",
        draft_unit_price=draft_unit_price,
        source_unit_price=source_unit_price,
    )


def _finalize_monitor_draft_update(
    cookie: str,
    draft_id: int,
    item: dict[str, Any],
    *,
    find_item: dict[str, Any],
    ktru_item: dict[str, Any],
    ktru_code: str,
    preoffer_title: str,
    ktru_name: str | None,
    draft_unit_price: str | None,
    country_origin_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str, str]:
    """Apply find values, delivery, country; update draft and verify."""
    from rts_national_mode import country_from_find, okpd2_code_from_ktru

    registry_warning = ""
    fill_report = apply_find_to_draft_item(item, find_item, ktru_item).as_dict()
    # Товарный знак: только из find, если там задан; не подставляем наименование/preoffer.
    find_tm = find_item.get("tradeMark") if isinstance(find_item, dict) else None
    if isinstance(find_tm, str) and find_tm.strip():
        item["tradeMark"] = find_tm.strip()
    else:
        item["tradeMark"] = None
    apply_find_delivery(item, find_item, price_per_unit=draft_unit_price)
    # При клоне из find страна конкурента важнее CSV/дефолта РФ.
    country = country_from_find(find_item) or resolve_country_for_draft(
        ktru_code=ktru_code,
        title=preoffer_title or ktru_name or "",
        path=country_origin_path,
    )
    info_types: dict[str, Any] = {}
    try:
        okpd2 = okpd2_code_from_ktru(ktru_code, ktru_item if isinstance(ktru_item, dict) else None)
        if not okpd2 and isinstance(find_item, dict):
            find_ktru = find_item.get("ktru")
            okpd2 = okpd2_code_from_ktru(
                ktru_code,
                find_ktru if isinstance(find_ktru, dict) else find_item,
            )
        if okpd2:
            info_types = fetch_national_mode_info_types(cookie, okpd2)
    except RtsPpApiError:
        info_types = {}
    nm_source = apply_draft_defaults(
        item,
        country=country,
        find_item=find_item,
        info_types_payload=info_types or None,
    )
    if isinstance(fill_report, dict):
        fill_report["national_mode_source"] = nm_source
    _fix_characteristic_value_ids(item, ktru_item)
    update_draft(cookie, item)
    item = get_draft(cookie, draft_id)
    verify_report = compare_find_to_draft(find_item, item, ktru_item=ktru_item)
    protected_id = str(item.get("protectedId") or "").strip()
    if not protected_id:
        found = draft_search_by_id(cookie, draft_id)
        if found:
            protected_id = str(found.get("protectedId") or "").strip()
    edit_url = draft_edit_url(protected_id, draft_id=draft_id)
    if not protected_id:
        registry_warning = (
            "РТС не вернул protectedId для формы редактирования. "
            f"Откройте список черновиков и найдите позицию по КТРУ {ktru_code} (id {draft_id})."
        )
    return fill_report, verify_report, item, protected_id, edit_url, registry_warning


def create_pp_draft_from_monitor(
    *,
    analytics_db: Path,
    meta_dir: Path,
    preoffers_path: Path,
    cookie_path: Path,
    offer_price_list_item_id: str,
    rts_profile: str = "",
    created_by: str = "",
    price_ord_enabled: bool | None = None,
) -> DraftResult:
    oid = str(offer_price_list_item_id or "").strip()
    profile = normalize_rts_profile(rts_profile)
    if not oid:
        return DraftResult(error="Укажите offer_price_list_item_id.", reason="bad_request", http_status=400)

    finding = _ensure_monitor_finding(analytics_db, meta_dir, oid)
    if not finding:
        return DraftResult(error="Образец не найден.", reason="not_found", http_status=404)

    existing = get_pp_monitor_draft(analytics_db, oid, rts_profile=profile)
    resume_draft_id: int | None = None
    if existing and existing.get("rts_draft_id"):
        try:
            resume_draft_id = int(existing["rts_draft_id"])
        except (TypeError, ValueError):
            resume_draft_id = None
    if existing and existing.get("status") == "created" and resume_draft_id:
        verify_report: dict[str, Any] = {}
        raw_verify = str(existing.get("verify_json") or "").strip()
        if raw_verify:
            try:
                parsed = json.loads(raw_verify)
                if isinstance(parsed, dict):
                    verify_report = parsed
            except json.JSONDecodeError:
                pass
        return DraftResult(
            ok=True,
            already_existed=True,
            rts_draft_id=resume_draft_id,
            protected_id=str(existing.get("protected_id") or ""),
            edit_url=str(existing.get("edit_url") or ""),
            ktru_code=str(existing.get("ktru_code") or ""),
            offer_id=str(existing.get("offer_id") or ""),
            verify_report=verify_report,
        )

    find_item = finding.get("find") if isinstance(finding.get("find"), dict) else {}
    ktru_obj = find_item.get("ktru") if isinstance(find_item.get("ktru"), dict) else {}
    ktru_code = str(finding.get("ktru_code") or ktru_obj.get("code") or "").strip()
    if not ktru_code:
        return DraftResult(error="У образца нет кода КТРУ.", reason="no_ktru", http_status=400)

    ktru_cache = cache_dir_default(meta_dir)
    offers = load_preoffers(preoffers_path)
    participate = pp_participate_eligibility(ktru_code, preoffers=offers, ktru_cache_dir=ktru_cache)
    if not participate["can_participate"]:
        return DraftResult(
            error=participate["participate_reason"] or "Участие недоступно для этого КТРУ.",
            reason="not_eligible",
            http_status=400,
            ktru_code=ktru_code,
        )

    matching = preoffers_by_ktru(offers, ktru_code)
    best = matching[0] if matching else None
    preoffer_title = best.title if best else ""
    offer_id = best.offer_id if best else ""

    fallback_price = finding.get("unit_price")
    draft_unit_price = competitor_draft_unit_price(
        find_item,
        fallback_unit_price=fallback_price if isinstance(fallback_price, (int, float)) else None,
    )
    source_unit_price = competitor_unit_price_amount(
        find_item,
        fallback_unit_price=fallback_price if isinstance(fallback_price, (int, float)) else None,
    )
    draft_unit_price_float = parse_money_amount(draft_unit_price) if draft_unit_price else None
    registry_warning = ""
    if not draft_unit_price:
        registry_warning = "Не удалось определить цену конкурента для расчёта (−5%)."

    unit_obj = find_item.get("unit")
    unit_text = str(unit_obj.get("name") or "").strip() if isinstance(unit_obj, dict) else None
    ktru_name = str(ktru_obj.get("name") or "").strip() or None
    country_origin_path = default_country_origin_path(Path(__file__).resolve().parent)

    draft_id: int | None = resume_draft_id
    fill_report: dict[str, Any] = {}
    verify_report: dict[str, Any] = {}
    protected_id = ""
    edit_url = ""

    try:
        cookie = load_offers_cookie(cookie_path)
        validate_cookie_session(cookie)
        ktru_item = resolve_ktru_for_create(
            cookie,
            ktru_code,
            ktru_name=ktru_name,
            cache_dir=ktru_cache,
        )
        if draft_id is None:
            payload, create_fill = build_find_create_payload(
                ktru_item,
                find_item,
                unit_from_line=unit_text,
            )
            fill_report = create_fill.as_dict()
            try:
                draft_id = create_draft(cookie, payload)
            except RtsPpApiError as create_exc:
                if _is_duplicate_draft_error(create_exc):
                    return DraftResult(
                        error=_duplicate_draft_message(ktru_code, create_exc),
                        reason="duplicate_draft",
                        http_status=409,
                        ktru_code=ktru_code,
                    )
                raise
        item = get_draft(cookie, int(draft_id))
        fill_update, verify_report, item, protected_id, edit_url, warn = _finalize_monitor_draft_update(
            cookie,
            int(draft_id),
            item,
            find_item=find_item,
            ktru_item=ktru_item,
            ktru_code=ktru_code,
            preoffer_title=preoffer_title,
            ktru_name=ktru_name,
            draft_unit_price=draft_unit_price,
            country_origin_path=country_origin_path,
        )
        fill_report = {**fill_report, **fill_update}
        if warn:
            registry_warning = f"{registry_warning} {warn}".strip() if registry_warning else warn
    except RtsPpApiError as exc:
        if exc.reason in ("no_cookie", "session_expired"):
            return DraftResult(
                error=str(exc),
                reason=exc.reason,
                http_status=503,
                ktru_code=ktru_code,
            )
        if _is_duplicate_draft_error(exc):
            return DraftResult(
                error=_duplicate_draft_message(ktru_code, exc),
                reason="duplicate_draft",
                http_status=409,
                ktru_code=ktru_code,
            )
        _save_monitor_draft_record(
            analytics_db,
            offer_price_list_item_id=oid,
            ktru_code=ktru_code,
            rts_draft_id=draft_id,
            offer_id=offer_id,
            status="error",
            error_text=str(exc),
            created_by=created_by,
            rts_profile=profile,
            fill_report=fill_report or None,
            draft_unit_price=draft_unit_price_float,
            source_unit_price=source_unit_price,
        )
        detail = str(exc)
        if exc.reason == "ktru_catalog_unavailable":
            detail = (
                f"{exc} Импортируйте JSON Ktru/search на странице "
                f"«Cookie / сессия» (раздел «Кэш шаблонов КТРУ»)."
            )
        if draft_id is not None:
            detail = (
                f"{detail} Черновик на РТС создан (id {draft_id}) — "
                "повторите «Создать черновик» для дозаполнения."
            )
        return DraftResult(
            error=detail,
            reason=exc.reason or "api_error",
            http_status=502,
            ktru_code=ktru_code,
            rts_draft_id=draft_id,
        )

    _save_monitor_draft_record(
        analytics_db,
        offer_price_list_item_id=oid,
        ktru_code=ktru_code,
        rts_draft_id=int(draft_id),
        protected_id=protected_id,
        edit_url=edit_url,
        offer_id=offer_id,
        status="created",
        created_by=created_by,
        rts_profile=profile,
        fill_report=fill_report,
        verify_report=verify_report,
        draft_unit_price=draft_unit_price_float,
        source_unit_price=source_unit_price,
    )
    return DraftResult(
        ok=True,
        rts_draft_id=int(draft_id),
        protected_id=protected_id,
        edit_url=edit_url,
        drafts_list_url=OFFERS_DRAFTS_URL,
        registry_warning=registry_warning,
        ktru_code=ktru_code,
        preoffer_title=preoffer_title,
        offer_id=offer_id,
        fill_report=fill_report if isinstance(fill_report, dict) else {},
        verify_report=verify_report if isinstance(verify_report, dict) else {},
    )


def create_pp_draft_from_catalog(
    *,
    analytics_db: Path,
    meta_dir: Path,
    cookie_path: Path,
    ktru_code: str,
    characteristics: list[dict[str, str]] | None = None,
    regions: list[dict[str, Any]] | None = None,
    rts_profile: str = "",
    created_by: str = "",
    source: str = "catalog",
    fill_extra: dict[str, Any] | None = None,
) -> DraftResult:
    """Create a PP draft from KTRU template + OKTMO catalog regions (not notice/monitor)."""
    from catalog_pp_draft import (
        CATALOG_PURCHASE_NUMBER,
        CatalogPpDraftError,
        apply_catalog_characteristics,
        apply_catalog_to_draft_item,
        normalize_characteristic_selections,
        normalize_region_prices,
    )
    from rts_national_mode import okpd2_code_from_ktru
    from rts_pp_api import build_create_payload

    code = str(ktru_code or "").strip()
    profile = normalize_rts_profile(rts_profile)
    if not code:
        return DraftResult(error="Укажите код КТРУ.", reason="bad_request", http_status=400)
    try:
        selections = normalize_characteristic_selections(characteristics)
        region_rows = normalize_region_prices(regions)
    except CatalogPpDraftError as exc:
        return DraftResult(error=str(exc), reason="bad_request", http_status=400)

    line_key = str(uuid.uuid4())
    ktru_cache = cache_dir_default(meta_dir)
    draft_id: int | None = None
    fill_report: dict[str, Any] = {}
    protected_id = ""
    edit_url = ""

    try:
        cookie = load_offers_cookie(cookie_path)
        validate_cookie_session(cookie)
        ktru_item = resolve_ktru_for_create(cookie, code, cache_dir=ktru_cache)
        payload = build_create_payload(ktru_item)
        apply_catalog_characteristics(payload, ktru_item, selections)
        try:
            draft_id = create_draft(cookie, payload)
        except RtsPpApiError as create_exc:
            if _is_duplicate_draft_error(create_exc):
                return DraftResult(
                    error=_duplicate_draft_message(code, create_exc),
                    reason="duplicate_draft",
                    http_status=409,
                    ktru_code=code,
                )
            raise
        item = get_draft(cookie, int(draft_id))
        info_types: dict[str, Any] = {}
        try:
            okpd2 = okpd2_code_from_ktru(code, ktru_item)
            if okpd2:
                info_types = fetch_national_mode_info_types(cookie, okpd2)
        except RtsPpApiError:
            info_types = {}
        fill_report = apply_catalog_to_draft_item(
            item,
            ktru_item,
            selections=selections,
            regions=region_rows,
            info_types_payload=info_types or None,
        )
        src = str(source or "catalog").strip() or "catalog"
        if src not in ("catalog", "core", "virtual_core"):
            src = "catalog"
        fill_report["source"] = src
        if isinstance(fill_extra, dict):
            for key, value in fill_extra.items():
                if key == "source":
                    continue
                fill_report[key] = value
        update_draft(cookie, item)
        item = get_draft(cookie, int(draft_id))
        protected_id = str(item.get("protectedId") or "").strip()
        if not protected_id:
            found = draft_search_by_id(cookie, int(draft_id), poll_retries=2, poll_delay=0.5)
            if isinstance(found, dict):
                protected_id = str(found.get("protectedId") or found.get("protected_id") or "").strip()
        edit_url = draft_edit_url(protected_id, draft_id=int(draft_id))
    except CatalogPpDraftError as exc:
        if draft_id is not None:
            save_rts_pp_draft(
                analytics_db,
                purchase_number=CATALOG_PURCHASE_NUMBER,
                line_key=line_key,
                ktru_code=code,
                rts_draft_id=int(draft_id),
                status="error",
                error_text=str(exc),
                created_by=created_by,
                rts_profile=profile,
            )
        return DraftResult(
            error=str(exc),
            reason="bad_request",
            http_status=400,
            ktru_code=code,
            rts_draft_id=draft_id,
        )
    except RtsPpApiError as exc:
        if exc.reason in ("no_cookie", "session_expired"):
            return DraftResult(
                error=str(exc),
                reason=exc.reason,
                http_status=503,
                ktru_code=code,
            )
        save_rts_pp_draft(
            analytics_db,
            purchase_number=CATALOG_PURCHASE_NUMBER,
            line_key=line_key,
            ktru_code=code,
            rts_draft_id=draft_id,
            status="error",
            error_text=str(exc),
            created_by=created_by,
            rts_profile=profile,
        )
        return DraftResult(
            error=str(exc),
            reason=exc.reason or "api_error",
            http_status=502,
            ktru_code=code,
            rts_draft_id=draft_id,
        )

    save_rts_pp_draft(
        analytics_db,
        purchase_number=CATALOG_PURCHASE_NUMBER,
        line_key=line_key,
        ktru_code=code,
        rts_draft_id=int(draft_id) if draft_id is not None else None,
        protected_id=protected_id,
        edit_url=edit_url,
        status="created",
        created_by=created_by,
        rts_profile=profile,
        fill_report_json=json.dumps(fill_report, ensure_ascii=False),
        verify_json=json.dumps({"source": fill_report.get("source") or "catalog", "ok": 1, "total": 1}, ensure_ascii=False),
    )
    return DraftResult(
        ok=True,
        rts_draft_id=int(draft_id) if draft_id is not None else None,
        protected_id=protected_id,
        edit_url=edit_url,
        drafts_list_url=OFFERS_DRAFTS_URL,
        ktru_code=code,
        fill_report=fill_report,
        verify_report={"source": fill_report.get("source") or "catalog"},
        purchase_number=CATALOG_PURCHASE_NUMBER,
        line_key=line_key,
    )


"""Create RTS PP drafts from EIS notice lines (shared by web UI and auto-participate)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eis_analytics_db import (
    enrich_notice_characteristics,
    get_pp_monitor_draft,
    get_pp_monitor_finding,
    get_pp_monitor_registry_row,
    get_rts_pp_draft,
    notice_line_for_pp,
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
    draft_edit_url,
    draft_search_by_id,
    get_draft,
    load_offers_cookie,
    resolve_ktru_for_create,
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
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    catalog_codes = {o.ktru_code for o in preoffers if o.enabled and o.ktru_code}
    in_catalog = code in catalog_codes
    in_pp_template = is_ktru_template_cached(ktru_cache_dir, code) if code else False
    require_catalog = _env_bool("RTS_PP_PARTICIPATE_REQUIRE_PREOFFER_CATALOG", default=True)
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


def participation_economics_for_line(
    line: dict[str, Any],
    *,
    price_ord_enabled: bool,
) -> dict[str, Any]:
    if not price_ord_enabled:
        return participation_economics_disabled()
    catalog = get_price_ord_catalog()
    if line.get("html_status") != "loaded":
        return compute_participation_economics(ktru_code="", catalog=catalog)
    return compute_participation_economics(
        ktru_code=str(line.get("ktru_code") or ""),
        delivery_place=str(line.get("delivery_place") or ""),
        region=str(line.get("region") or ""),
        price_per_unit_text=str(line.get("price_per_unit_text") or ""),
        quantity_text=str(line.get("quantity_text") or ""),
        line_sum_text=str(line.get("line_sum_text") or line.get("price_text") or ""),
        catalog=catalog,
    )


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
    if line.get("html_status") != "loaded":
        return DraftResult(
            error="Карточка ЕИС не загружена — сначала загрузите HTML.",
            reason="html_not_loaded",
            http_status=400,
        )

    ktru_code = str(line.get("ktru_code") or "").strip()
    if not ktru_code:
        return DraftResult(error="У позиции нет кода КТРУ.", reason="no_ktru", http_status=400)

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

    notice_chars = enrich_notice_characteristics(
        analytics_db,
        purchase_number=pn,
        ktru_code=ktru_code,
        html_chars=line.get("characteristics") or [],
        history_path=history_db,
    )

    matching = preoffers_by_ktru(offers, ktru_code)
    results = rank_offer_matches(notice_chars, matching)
    best = results[0] if results else None
    preoffer_title = best.title if best else ""
    offer_id = best.offer_id if best else ""

    ord_enabled = price_ord_enabled if price_ord_enabled is not None else _env_bool(
        "RTS_PARTICIPATION_PRICE_ORD_ENABLED", default=True
    )
    economics = participation_economics_for_line(line, price_ord_enabled=ord_enabled)
    draft_unit_price = str(line.get("price_per_unit_text") or "").strip() or None
    if economics.get("available") and economics.get("unit_price_for_draft"):
        draft_unit_price = str(economics["unit_price_for_draft"]).strip()

    try:
        cookie = load_offers_cookie(cookie_path)
        validate_cookie_session(cookie)
        created = create_and_save_pp_draft(
            cookie,
            ktru_code=ktru_code,
            ktru_name=str(line.get("position_display_name") or "").strip() or None,
            unit_text=str(line.get("unit_text") or "").strip() or None,
            notice_characteristics=notice_chars,
            delivery_place=str(line.get("delivery_place") or "").strip() or None,
            price_per_unit=draft_unit_price,
            delivery_days=str(line.get("delivery_days") or "").strip() or None,
            quantity_text=str(line.get("quantity_text") or "").strip() or None,
            ktru_cache_dir=ktru_cache,
            product_title=preoffer_title or str(line.get("position_display_name") or "").strip(),
            country_origin_path=default_country_origin_path(Path(__file__).resolve().parent),
        )
    except RtsPpApiError as exc:
        if exc.reason in ("no_cookie", "session_expired"):
            return DraftResult(
                error=str(exc),
                reason=exc.reason,
                http_status=503,
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
        detail = str(exc)
        if exc.reason == "ktru_catalog_unavailable":
            detail = (
                f"{exc} Импортируйте JSON Ktru/search на странице "
                f"«Cookie / сессия» (раздел «Кэш шаблонов КТРУ»)."
            )
        return DraftResult(
            error=detail,
            reason=exc.reason or "api_error",
            http_status=502,
            ktru_code=ktru_code,
        )

    fill_report = created.get("fill_report") or {}
    verify_report = created.get("verify_report") or {}
    save_rts_pp_draft(
        analytics_db,
        purchase_number=pn,
        line_key=lk,
        ktru_code=ktru_code,
        rts_draft_id=int(created["rts_draft_id"]),
        protected_id=str(created.get("protected_id") or ""),
        edit_url=str(created.get("edit_url") or ""),
        offer_id=offer_id,
        status="created",
        created_by=created_by,
        rts_profile=profile,
        fill_report_json=json.dumps(fill_report, ensure_ascii=False) if fill_report else "",
        verify_json=json.dumps(verify_report, ensure_ascii=False) if verify_report else "",
    )
    return DraftResult(
        ok=True,
        rts_draft_id=int(created["rts_draft_id"]),
        protected_id=str(created.get("protected_id") or ""),
        edit_url=str(created.get("edit_url") or ""),
        drafts_list_url=str(created.get("drafts_list_url") or ""),
        registry_warning=str(created.get("registry_warning") or ""),
        ktru_code=ktru_code,
        preoffer_title=preoffer_title,
        offer_id=offer_id,
        fill_report=fill_report if isinstance(fill_report, dict) else {},
        verify_report=verify_report if isinstance(verify_report, dict) else {},
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


def _duplicate_draft_message(ktru_code: str) -> str:
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
    registry_warning = ""
    fill_report = apply_find_to_draft_item(item, find_item, ktru_item).as_dict()
    if preoffer_title:
        item["tradeMark"] = preoffer_title
    apply_find_delivery(item, find_item, price_per_unit=draft_unit_price)
    country = resolve_country_for_draft(
        ktru_code=ktru_code,
        title=preoffer_title or ktru_name or "",
        path=country_origin_path,
    )
    apply_draft_defaults(item, country=country)
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
                        error=_duplicate_draft_message(ktru_code),
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
                error=_duplicate_draft_message(ktru_code),
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

"""Register analyst-found EIS notice ↔ RTS PP pairs (ground truth, match_origin=manual)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eis_analytics_db import (
    append_pp_eis_event,
    set_pp_eis_match_status,
    upsert_pp_eis_match,
    upsert_pp_monitor_finding,
    upsert_pp_monitor_registry_row,
)
from eis_changelog import MSK
from eis_pp_link import compare_notice_to_find, compute_lag_minutes, is_exact_match_notice_driven
from eis_pp_link_service import _load_ktru_template, pilot_ktru_codes
from pp_prehistory import _notice_region_context, notice_region_matches_pp_place
from pp_stack import stack_key

_MSK_DT_RE = re.compile(
    r"^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?$"
)


@dataclass
class ManualRegisterResult:
    offer_price_list_item_id: str
    purchase_number: str
    line_key: str
    matched_count: int
    total_compared: int
    is_exact_match: bool
    lag_minutes: float | None
    analyst_status: str
    oktmo_match: bool
    oktmo_detail: str
    pp_link_date: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "offer_price_list_item_id": self.offer_price_list_item_id,
            "purchase_number": self.purchase_number,
            "line_key": self.line_key,
            "matched_count": self.matched_count,
            "total_compared": self.total_compared,
            "is_exact_match": self.is_exact_match,
            "lag_minutes": self.lag_minutes,
            "analyst_status": self.analyst_status,
            "oktmo_match": self.oktmo_match,
            "oktmo_detail": self.oktmo_detail,
            "pp_link_date": self.pp_link_date,
        }


def parse_link_date_msk(text: str) -> str:
    """Parse 'DD.MM.YYYY HH:MM[:SS]' MSK to ISO UTC for pp_link_date."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    m = _MSK_DT_RE.match(raw)
    if not m:
        raise ValueError(f"ожидается DD.MM.YYYY HH:MM[:SS], получено: {raw!r}")
    dd, mm, yyyy, hh, mi, ss = m.groups()
    sec = int(ss or 0)
    dt = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mi), sec, tzinfo=MSK)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _notice_chars_from_db(db_path: Path, purchase_number: str, line_key: str) -> list[dict[str, Any]]:
    from eis_analytics_db import connect

    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT characteristics_json
            FROM notice_html_ktru_lines
            WHERE purchase_number = ? AND line_key = ?
            """,
            (purchase_number, line_key),
        ).fetchone()
    if not row:
        raise ValueError(f"строка извещения не найдена: {purchase_number} / {line_key}")
    return json.loads(str(row[0] or "[]"))


def _notice_pub_from_db(db_path: Path, purchase_number: str) -> str:
    from eis_analytics_db import connect

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT pub_date_utc FROM eis_rss_observations WHERE reg_number = ?",
            (purchase_number,),
        ).fetchone()
    return str(row[0] if row else "").strip()


def _notice_source_url(db_path: Path, purchase_number: str) -> str:
    from eis_analytics_db import connect

    pn = str(purchase_number or "").strip()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT link FROM eis_rss_observations WHERE reg_number = ?",
            (pn,),
        ).fetchone()
        if row and str(row[0] or "").strip():
            return str(row[0]).strip()
        row = conn.execute(
            "SELECT source_url FROM notice_html_pages WHERE purchase_number = ?",
            (pn,),
        ).fetchone()
        if row and str(row[0] or "").strip():
            return str(row[0]).strip()
    return (
        f"https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber={pn}"
    )


def _has_notice_ktru_lines(db_path: Path, purchase_number: str) -> bool:
    from eis_analytics_db import connect

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM notice_html_ktru_lines WHERE purchase_number = ? LIMIT 1",
            (purchase_number,),
        ).fetchone()
    return bool(row)


def ensure_notice_html_loaded(
    db_path: Path,
    purchase_number: str,
    *,
    timeout: float = 30.0,
) -> int:
    """Fetch and store notice HTML when KTRU lines are missing (manual import prerequisite)."""
    if _has_notice_ktru_lines(db_path, purchase_number):
        return 0
    from eis_analytics_db import record_notice_html_page
    from eis_notice_html_fetch import fetch_notice_common_info_html
    from eis_notice_html_parse import parse_notice_common_info_html

    pn = str(purchase_number or "").strip()
    url = _notice_source_url(db_path, pn)
    try:
        html, status = fetch_notice_common_info_html(url, timeout=timeout)
        lines = parse_notice_common_info_html(html, purchase_number=pn, source_url=url)
        record_notice_html_page(
            db_path,
            purchase_number=pn,
            source_url=url,
            html_text=html,
            lines=lines,
            fetched_at_utc=datetime.now(timezone.utc),
            ok=True,
            http_status=status,
        )
        if not lines:
            raise ValueError(f"HTML извещения {pn} загружен, но строк KTRU не найдено")
        return len(lines)
    except Exception as exc:
        record_notice_html_page(
            db_path,
            purchase_number=pn,
            source_url=url,
            html_text="",
            lines=[],
            fetched_at_utc=datetime.now(timezone.utc),
            ok=False,
            http_status=None,
            error_text=str(exc)[:500],
        )
        raise ValueError(f"не удалось загрузить HTML извещения {pn}: {exc}") from exc


def resolve_line_key(
    db_path: Path,
    *,
    purchase_number: str,
    line_key: str = "",
    ktru_code: str = "",
) -> str:
    lk = str(line_key or "").strip()
    if lk:
        return lk
    from eis_analytics_db import connect

    pn = str(purchase_number or "").strip()
    ktru = str(ktru_code or "").strip()
    with connect(db_path) as conn:
        if ktru:
            row = conn.execute(
                """
                SELECT line_key FROM notice_html_ktru_lines
                WHERE purchase_number = ? AND ktru_code = ?
                ORDER BY line_key LIMIT 1
                """,
                (pn, ktru),
            ).fetchone()
        else:
            pilot = tuple(pilot_ktru_codes())
            if pilot:
                placeholders = ",".join("?" for _ in pilot)
                row = conn.execute(
                    f"""
                    SELECT line_key FROM notice_html_ktru_lines
                    WHERE purchase_number = ? AND ktru_code IN ({placeholders})
                    ORDER BY line_key LIMIT 1
                    """,
                    (pn, *pilot),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT line_key FROM notice_html_ktru_lines
                    WHERE purchase_number = ?
                    ORDER BY line_key LIMIT 1
                    """,
                    (pn,),
                ).fetchone()
    if not row:
        raise ValueError(f"не найдена строка KTRU для извещения {pn}")
    return str(row[0] or "").strip()


def _check_oktmo(
    db_path: Path,
    *,
    purchase_number: str,
    find_item: dict[str, Any],
    expected_oktmo: str = "",
) -> tuple[bool, str]:
    notice_ctx = _notice_region_context(db_path, purchase_number)
    expected = str(expected_oktmo or "").strip()
    places = find_item.get("deliveryPlaces") if isinstance(find_item.get("deliveryPlaces"), list) else []
    hits: list[str] = []
    for place in places:
        if not isinstance(place, dict):
            continue
        ok, label = notice_region_matches_pp_place(notice_ctx, place)
        oktmo = place.get("oktmo") if isinstance(place.get("oktmo"), dict) else {}
        code = str(oktmo.get("code") or "").strip()
        if ok:
            hits.append(label or code or "?")
        if expected and code == expected:
            return True, f"ОКТМО {code} ({label or code})"
    notice_code = str(notice_ctx.get("oktmo_code") or "").strip()
    if expected:
        if expected == notice_code:
            for place in places:
                if not isinstance(place, dict):
                    continue
                oktmo = place.get("oktmo") if isinstance(place.get("oktmo"), dict) else {}
                if str(oktmo.get("code") or "").strip() == expected:
                    return True, f"ОКТМО {expected}"
        return False, f"ожидался ОКТМО {expected}, извещение {notice_code or '—'}, совпадений: {', '.join(hits) or 'нет'}"
    if hits:
        return True, ", ".join(hits)
    return False, f"регион извещения ({notice_ctx.get('region') or '—'}) не совпал с местами поставки ПП"


def _extract_pp_fields(find_item: dict[str, Any], *, link_date_override: str = "") -> dict[str, Any]:
    oid = str(find_item.get("offerPriceListItemId") or "").strip()
    if not oid:
        raise ValueError("offerPriceListItemId отсутствует в find")
    ktru_obj = find_item.get("ktru") if isinstance(find_item.get("ktru"), dict) else {}
    ktru_code = str(ktru_obj.get("code") or "").strip()
    trade_mark = str(find_item.get("tradeMark") or "").strip()
    create_dt = str(find_item.get("createDateTime") or "").strip()
    link_date = str(link_date_override or find_item.get("offerPriceListItemLinkDate") or create_dt).strip()
    price_list_item_id = find_item.get("priceListItemId")
    try:
        price_list_item_id = int(price_list_item_id) if price_list_item_id is not None else None
    except (TypeError, ValueError):
        price_list_item_id = None
    unit_price: float | None = None
    delivery_regions = 0
    places = find_item.get("deliveryPlaces")
    if isinstance(places, list):
        delivery_regions = len(places)
        if places and isinstance(places[0], dict):
            raw_price = str(places[0].get("unitPrice") or "").replace(",", ".")
            try:
                unit_price = float(raw_price)
            except ValueError:
                unit_price = None
    return {
        "offer_price_list_item_id": oid,
        "ktru_code": ktru_code,
        "ktru_name": str(ktru_obj.get("name") or "").strip(),
        "trade_mark": trade_mark,
        "link_date": link_date,
        "unit_price": unit_price,
        "delivery_regions_count": delivery_regions or None,
        "price_list_item_id": price_list_item_id,
        "card_url": f"https://offers.rts-tender.ru/customer/offers/offer-item/{oid}",
    }


def register_manual_eis_pp_pair(
    db_path: Path,
    meta_dir: Path,
    *,
    purchase_number: str,
    offer_price_list_item_id: str,
    line_key: str = "",
    find_item: dict[str, Any] | None = None,
    link_date_msk: str = "",
    expected_oktmo: str = "",
    analyst_note: str = "",
    analyst_user: str = "manual_import",
    source: str = "manual_ground_truth",
    fetch_timeout: float = 30.0,
) -> ManualRegisterResult:
    pn = str(purchase_number or "").strip()
    oid = str(offer_price_list_item_id or "").strip()
    if not pn or not oid:
        raise ValueError("purchase_number и offer_price_list_item_id обязательны")

    if find_item is None:
        from rts_customer_pp_api import find_offer_item, load_customer_cookie

        cookie = load_customer_cookie(meta_dir)
        if not cookie:
            raise ValueError("нет customer-cookie для загрузки find с РТС")
        find_item = find_offer_item(cookie, oid, timeout=fetch_timeout)
    if not isinstance(find_item, dict):
        raise ValueError("find_item должен быть dict")

    pp_preview = _extract_pp_fields(find_item)
    ensure_notice_html_loaded(db_path, pn, timeout=fetch_timeout)
    lk = resolve_line_key(
        db_path,
        purchase_number=pn,
        line_key=line_key,
        ktru_code=pp_preview["ktru_code"],
    )
    link_override = ""
    if str(link_date_msk or "").strip():
        link_override = parse_link_date_msk(link_date_msk)

    pp = _extract_pp_fields(find_item, link_date_override=link_override)
    find_json = json.dumps(find_item, ensure_ascii=False)

    upsert_pp_monitor_registry_row(
        db_path,
        offer_price_list_item_id=pp["offer_price_list_item_id"],
        ktru_code=pp["ktru_code"],
        name=pp["ktru_name"],
        trade_mark=pp["trade_mark"],
        link_date=pp["link_date"],
        unit_price=pp["unit_price"],
        delivery_regions_count=pp["delivery_regions_count"],
        card_url=pp["card_url"],
        price_list_item_id=pp["price_list_item_id"],
    )
    upsert_pp_monitor_finding(
        db_path,
        offer_price_list_item_id=pp["offer_price_list_item_id"],
        find_json=find_json,
        ktru_code=pp["ktru_code"],
        trade_mark=pp["trade_mark"],
        link_date=pp["link_date"],
        unit_price=pp["unit_price"],
        card_url=pp["card_url"],
        source=source,
    )

    oktmo_ok, oktmo_detail = _check_oktmo(
        db_path,
        purchase_number=pn,
        find_item=find_item,
        expected_oktmo=expected_oktmo,
    )

    notice_chars = _notice_chars_from_db(db_path, pn, lk)
    notice_pub = _notice_pub_from_db(db_path, pn)
    ktru_item = _load_ktru_template(meta_dir, pp["ktru_code"])
    result = compare_notice_to_find(notice_chars, find_item, ktru_item=ktru_item)
    lag = compute_lag_minutes(notice_pub, pp["link_date"])
    exact = is_exact_match_notice_driven(result)

    from eis_analytics_db import _characteristics_hash_from_find_json

    chash = _characteristics_hash_from_find_json(find_json) or ""
    sk = stack_key(pp["ktru_code"], chash) if chash else ""

    note_parts = [str(analyst_note or "").strip()]
    if link_date_msk:
        note_parts.append(f"link_date_msk={link_date_msk.strip()}")
    if expected_oktmo:
        note_parts.append(f"oktmo={expected_oktmo.strip()} ({'ok' if oktmo_ok else 'mismatch'}: {oktmo_detail})")
    elif oktmo_detail:
        note_parts.append(f"region: {oktmo_detail}")
    full_note = " · ".join(x for x in note_parts if x)[:2000]

    upsert_pp_eis_match(
        db_path,
        purchase_number=pn,
        line_key=lk,
        offer_price_list_item_id=oid,
        ktru_code=pp["ktru_code"],
        stack_key=sk,
        matched_count=result.matched_count,
        total_compared=result.total_compared,
        notice_only_count=result.notice_only_count,
        pp_only_count=result.pp_only_count,
        diff_json=json.dumps(result.rows, ensure_ascii=False),
        notice_pub_date_utc=notice_pub,
        pp_link_date=pp["link_date"],
        is_exact_match=exact,
        lag_minutes=lag,
        match_origin="manual",
    )
    set_pp_eis_match_status(
        db_path,
        purchase_number=pn,
        line_key=lk,
        offer_price_list_item_id=oid,
        status="pending",
        analyst_user=analyst_user,
        analyst_note=full_note,
    )
    append_pp_eis_event(
        db_path,
        event_type="manual_ground_truth_import",
        ktru_code=pp["ktru_code"],
        payload={
            "purchase_number": pn,
            "line_key": lk,
            "offer_price_list_item_id": oid,
            "lag_minutes": lag,
            "is_exact_match": exact,
            "matched_count": result.matched_count,
            "total_compared": result.total_compared,
            "oktmo_match": oktmo_ok,
            "oktmo_detail": oktmo_detail,
            "source": source,
        },
    )
    return ManualRegisterResult(
        offer_price_list_item_id=oid,
        purchase_number=pn,
        line_key=lk,
        matched_count=result.matched_count,
        total_compared=result.total_compared,
        is_exact_match=exact,
        lag_minutes=lag,
        analyst_status="pending",
        oktmo_match=oktmo_ok,
        oktmo_detail=oktmo_detail,
        pp_link_date=pp["link_date"],
    )

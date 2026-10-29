"""Weekly snapshot of the RTS preliminary-offer registry and competitor analytics."""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from competitor_monitor_settings import (
    clear_stale_job,
    finish_job,
    format_msk,
    load_settings,
    schedule_due,
    settings_path,
    update_job,
    utc_now_iso,
)
from eis_analytics_db import load_competitor_ktru, replace_competitor_ktru_snapshot
from participation_price import subject_candidates_from_oktmo
from pp_core_catalog import demand_key, find_selection_pairs, locked_code_set
from rts_customer_pp_api import (
    SEARCH_PAGE_TAKE,
    SEARCH_STATUS_PUBLISHED,
    RegistrySearchItem,
    RtsCustomerPpApiError,
    build_search_payload,
    load_customer_cookie,
    resolve_ktru_template,
    search_registry_items,
)
from rts_oktmo_catalog import OktmoCatalogError, load_subject_oktmo

_LOG = logging.getLogger("competitor_monitor")
_POLL_LOCK = threading.Lock()
PAGE_DELAY_SEC = 3.0
KTRU_DELAY_SEC = 30.0
MAX_PAGES = 500
CHART_DEFAULT_SERIES = 3

Trigger = str


def poll_busy() -> bool:
    return _POLL_LOCK.locked()


def subject_catalog() -> dict[str, str]:
    try:
        return {row.code: row.name for row in load_subject_oktmo()}
    except OktmoCatalogError:
        return {}


def resolve_subject(code: str, name: str, catalog: dict[str, str]) -> tuple[str, str]:
    raw = str(code or "").strip()
    label = str(name or "").strip()
    if catalog and raw:
        if raw in catalog:
            return raw, catalog[raw]
        for cand in subject_candidates_from_oktmo(raw):
            if cand in catalog:
                return cand, catalog[cand]
    return raw or label, label or raw


def _median(values: list[float]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return float(statistics.median(clean))


def _min_number(values: list[float | int]) -> float | int | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return min(clean)


def variant_key_text(key: frozenset[tuple[str, str]]) -> str:
    return "|".join(f"{code}={value}" for code, value in sorted(key))


def _template_value_labels(template: dict[str, Any]) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    names: dict[str, str] = {}
    values: dict[tuple[str, str], str] = {}
    for ch in template.get("characteristics") or []:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or "").strip()
        if not code:
            continue
        names[code] = str(ch.get("name") or code).strip() or code
        for opt in ch.get("characteristicValues") or []:
            if not isinstance(opt, dict):
                continue
            value_code = str(opt.get("valueCode") or opt.get("id") or "").strip()
            text = str(opt.get("qualityDescription") or opt.get("concreteValue") or value_code).strip()
            if value_code and text:
                values[(code, value_code)] = text
    return names, values


def _label_from_key(key: frozenset[tuple[str, str]], template: dict[str, Any]) -> str:
    names, values = _template_value_labels(template)
    grouped: dict[str, list[str]] = defaultdict(list)
    for code, value_code in sorted(key):
        grouped[code].append(values.get((code, value_code), value_code))
    parts = []
    for code, texts in sorted(grouped.items(), key=lambda item: (names.get(item[0], item[0]), item[0])):
        parts.append(f"{names.get(code, code)}={', '.join(texts)}")
    return "; ".join(parts)


def _raw_characteristic_text(val: dict[str, Any]) -> str:
    for field in ("qualityDescription", "concreteValue"):
        text = str(val.get(field) or "").strip()
        if text:
            return text
    min_v = str(val.get("minValue") or "").strip()
    max_v = str(val.get("maxValue") or "").strip()
    if min_v and max_v:
        return f"{min_v}–{max_v}"
    return min_v or max_v


def _raw_variant(characteristics: list[dict[str, Any]]) -> tuple[str, str]:
    pairs: list[tuple[str, str]] = []
    labels: list[str] = []
    for ch in characteristics:
        code = str(ch.get("code") or ch.get("name") or "").strip()
        name = str(ch.get("name") or code).strip()
        texts: list[str] = []
        for val in ch.get("characteristicValues") or []:
            if not isinstance(val, dict):
                continue
            text = _raw_characteristic_text(val)
            if text:
                texts.append(text)
        if not code or not texts:
            continue
        for text in texts:
            pairs.append((code, text))
        labels.append(f"{name}={', '.join(texts)}")
    if not pairs:
        return "", "Без характеристик"
    return variant_key_text(frozenset(pairs)), "; ".join(labels)


def variant_identity(
    characteristics: list[dict[str, Any]],
    template: dict[str, Any] | None,
) -> tuple[str, str]:
    if isinstance(template, dict) and template:
        pairs = find_selection_pairs({"ktruCharacteristics": characteristics}, template)
        key = demand_key(pairs, locked=locked_code_set(template))
        if key:
            return variant_key_text(key), _label_from_key(key, template)
    return _raw_variant(characteristics)


def snapshot_rows_from_items(
    items: list[RegistrySearchItem],
    template: dict[str, Any] | None,
    catalog: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    offers: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    for item in items:
        variant_key, variant_label = variant_identity(item.characteristics, template)
        place_prices = [place.unit_price for place in item.places if place.unit_price is not None]
        place_days = [place.min_delivery_days for place in item.places if place.min_delivery_days is not None]
        place_qty = [place.min_quantity for place in item.places if place.min_quantity is not None]
        by_subject: dict[str, dict[str, Any]] = {}
        for place in item.places:
            subject_code, subject_name = resolve_subject(place.oktmo_code, place.oktmo_name, catalog)
            if not subject_code:
                continue
            bucket = by_subject.setdefault(
                subject_code,
                {"name": subject_name, "prices": [], "days": [], "qty": []},
            )
            if place.unit_price is not None:
                bucket["prices"].append(place.unit_price)
            if place.min_delivery_days is not None:
                bucket["days"].append(place.min_delivery_days)
            if place.min_quantity is not None:
                bucket["qty"].append(place.min_quantity)
        for subject_code, bucket in by_subject.items():
            regions.append(
                {
                    "offer_price_list_item_id": item.offer_price_list_item_id,
                    "subject_code": subject_code,
                    "subject_name": bucket["name"],
                    "unit_price": _median(bucket["prices"]),
                    "min_delivery_days": _min_number(bucket["days"]),
                    "min_quantity": _min_number(bucket["qty"]),
                }
            )
        offers.append(
            {
                "offer_price_list_item_id": item.offer_price_list_item_id,
                "price_list_item_id": item.price_list_item_id,
                "name": item.name,
                "participant_inn": item.participant_inn,
                "participant_name": item.participant_name,
                "participant_id": item.participant_id,
                "is_own": item.is_own,
                "variant_key": variant_key,
                "variant_label": variant_label,
                "countries": item.countries,
                "min_delivery_days": _min_number(place_days),
                "min_quantity": _min_number(place_qty),
                "median_price": _median([p for p in place_prices if p is not None]),
                "link_date": item.link_date,
                "validity_date": item.validity_date,
            }
        )
    points = _variant_points(offers)
    return offers, regions, points


def _variant_points(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in offers:
        if row.get("is_own"):
            continue
        key = str(row.get("variant_key") or "")
        bucket = grouped.setdefault(
            key,
            {
                "variant_key": key,
                "variant_label": str(row.get("variant_label") or ""),
                "count": 0,
                "prices": [],
            },
        )
        bucket["count"] += 1
        if row.get("variant_label"):
            bucket["variant_label"] = str(row.get("variant_label") or "")
        price = row.get("median_price")
        if price is not None:
            bucket["prices"].append(float(price))
    points: list[dict[str, Any]] = []
    for bucket in grouped.values():
        prices = bucket["prices"]
        points.append(
            {
                "variant_key": bucket["variant_key"],
                "variant_label": bucket["variant_label"] or "Без характеристик",
                "offer_count": int(bucket["count"]),
                "price_min": min(prices) if prices else None,
                "price_median": _median(prices),
                "price_max": max(prices) if prices else None,
            }
        )
    points.sort(key=lambda row: (-int(row["offer_count"]), str(row["variant_label"])))
    return points


def _fetch_ktru_items(
    cookie: str,
    ktru_code: str,
    *,
    on_page: Any,
) -> list[RegistrySearchItem]:
    skip = 0
    page_no = 0
    total = 0
    found: dict[str, RegistrySearchItem] = {}
    while True:
        page_no += 1
        if page_no > MAX_PAGES:
            raise RtsCustomerPpApiError("Слишком много страниц реестра.", reason="api_error")
        payload = build_search_payload(
            ktru_code,
            skip=skip,
            take=SEARCH_PAGE_TAKE,
            status=SEARCH_STATUS_PUBLISHED,
        )
        batch, page_total = search_registry_items(cookie, payload)
        total = int(page_total or 0)
        for item in batch:
            found[item.offer_price_list_item_id] = item
        on_page(page_no, total)
        skip += SEARCH_PAGE_TAKE
        if not batch or skip >= total:
            break
        if PAGE_DELAY_SEC > 0:
            time.sleep(PAGE_DELAY_SEC)
    return list(found.values())


def _execute(
    meta_dir: Path,
    db_path: Path,
    *,
    codes: list[str],
    trigger: Trigger,
) -> dict[str, Any]:
    path = settings_path(meta_dir)
    result: dict[str, Any] = {"ok": True, "reason": "", "error": "", "ktru_done": []}
    update_job(
        path,
        job_status="running",
        job_progress="Старт",
        job_error="",
        last_started_at_utc=utc_now_iso(),
        last_trigger=trigger,
    )
    try:
        cookie = load_customer_cookie(meta_dir)
    except RtsCustomerPpApiError as exc:
        finish_job(path, status="error", error=str(exc))
        result["ok"] = False
        result["reason"] = exc.reason
        result["error"] = str(exc)
        return result
    catalog = subject_catalog()
    total_codes = len(codes)
    for index, code in enumerate(codes, start=1):
        if index > 1 and KTRU_DELAY_SEC > 0:
            time.sleep(KTRU_DELAY_SEC)
        template: dict[str, Any] | None = None
        try:
            template = resolve_ktru_template(cookie, code, meta_dir)
        except RtsCustomerPpApiError as exc:
            if exc.reason == "session_expired":
                finish_job(path, status="error", error=str(exc))
                result["ok"] = False
                result["reason"] = exc.reason
                result["error"] = str(exc)
                return result
            template = None

        def _on_page(page_no: int, total: int, *, _index: int = index, _code: str = code) -> None:
            del _code
            total_text = str(total) if total else "…"
            update_job(
                path,
                job_status="running",
                job_progress=f"КТРУ {_index}/{total_codes}, страница {page_no}, всего {total_text}",
            )

        try:
            items = _fetch_ktru_items(cookie, code, on_page=_on_page)
        except RtsCustomerPpApiError as exc:
            finish_job(path, status="error", error=str(exc), progress=f"КТРУ {index}/{total_codes}")
            result["ok"] = False
            result["reason"] = exc.reason
            result["error"] = str(exc)
            return result
        offers, regions, points = snapshot_rows_from_items(items, template, catalog)
        replace_competitor_ktru_snapshot(
            db_path,
            ktru_code=code,
            snapshot_at_utc=utc_now_iso(),
            offers=offers,
            regions=regions,
            points=points,
        )
        result["ktru_done"].append(code)
        _LOG.info("competitor monitor saved %s items=%s", code, len(offers))
    finish_job(
        path,
        status="ok",
        advance=trigger in ("schedule", "manual_list") and bool(codes),
    )
    return result


def run_competitor_poll(
    meta_dir: Path,
    db_path: Path,
    *,
    codes: list[str],
    trigger: Trigger,
) -> dict[str, Any]:
    if not codes:
        return {"ok": False, "reason": "no_ktru", "error": "Список КТРУ пуст", "ktru_done": []}
    if not _POLL_LOCK.acquire(blocking=False):
        return {"ok": False, "reason": "busy", "error": "Опрос уже идёт", "ktru_done": []}
    try:
        return _execute(meta_dir, db_path, codes=list(codes), trigger=trigger)
    except Exception as exc:
        _LOG.exception("competitor monitor poll")
        finish_job(settings_path(meta_dir), status="error", error="Опрос завершился с ошибкой")
        return {"ok": False, "reason": "error", "error": str(exc), "ktru_done": []}
    finally:
        _POLL_LOCK.release()


def start_competitor_poll_async(
    meta_dir: Path,
    db_path: Path,
    *,
    codes: list[str],
    trigger: Trigger,
) -> str | None:
    if not codes:
        return "Список КТРУ пуст"
    if not _POLL_LOCK.acquire(blocking=False):
        return "Опрос уже идёт"

    def _worker() -> None:
        try:
            _execute(meta_dir, db_path, codes=list(codes), trigger=trigger)
        except Exception:
            _LOG.exception("competitor monitor poll")
            try:
                finish_job(settings_path(meta_dir), status="error", error="Опрос завершился с ошибкой")
            except Exception:
                _LOG.exception("competitor monitor status")
        finally:
            _POLL_LOCK.release()

    threading.Thread(target=_worker, daemon=True, name="competitor-monitor").start()
    return None


def supervisor_tick(meta_dir: Path, db_path: Path) -> None:
    path = settings_path(meta_dir)
    clear_stale_job(path)
    settings = load_settings(path)
    if not schedule_due(settings):
        return
    start_competitor_poll_async(
        meta_dir,
        db_path,
        codes=list(settings.get("ktru_codes") or []),
        trigger="schedule",
    )


def format_price(value: float | None) -> str:
    if value is None:
        return "—"
    text = f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    return text


def format_qty(value: float | int | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.2f}".replace(".", ",")


def _price_stats(values: list[float]) -> dict[str, float | None]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"min": None, "median": None, "max": None}
    return {"min": min(clean), "median": _median(clean), "max": max(clean)}


def _own_label(value: float | None) -> str:
    if value is None:
        return "нашей цены нет"
    return format_price(value)


def build_ktru_view(payload: dict[str, Any]) -> dict[str, Any]:
    offers = [row for row in payload.get("offers") or [] if isinstance(row, dict)]
    regions = [row for row in payload.get("regions") or [] if isinstance(row, dict)]
    points = [row for row in payload.get("points") or [] if isinstance(row, dict)]
    state = payload.get("state") if isinstance(payload.get("state"), dict) else None
    competitors = [row for row in offers if not int(row.get("is_own") or 0)]
    own_rows = [row for row in offers if int(row.get("is_own") or 0)]
    regions_by_offer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for region in regions:
        regions_by_offer[str(region.get("offer_price_list_item_id") or "")].append(region)

    card_prices = [float(row["median_price"]) for row in competitors if row.get("median_price") is not None]
    overall = _price_stats(card_prices)
    suppliers: dict[str, dict[str, Any]] = {}
    for row in competitors:
        inn = str(row.get("participant_inn") or row.get("participant_id") or "").strip() or "—"
        bucket = suppliers.setdefault(
            inn,
            {"inn": inn, "name": str(row.get("participant_name") or "").strip() or "—", "positions": 0},
        )
        if row.get("participant_name"):
            bucket["name"] = str(row.get("participant_name") or "")
        bucket["positions"] += 1
    supplier_rows = sorted(suppliers.values(), key=lambda row: (-int(row["positions"]), str(row["name"])))

    subject_prices: dict[str, dict[str, Any]] = {}
    subject_own: dict[str, list[float]] = defaultdict(list)
    for row in competitors:
        for region in regions_by_offer.get(str(row.get("offer_price_list_item_id") or ""), []):
            code = str(region.get("subject_code") or "")
            if not code or region.get("unit_price") is None:
                continue
            bucket = subject_prices.setdefault(
                code,
                {"code": code, "name": str(region.get("subject_name") or code), "prices": []},
            )
            bucket["prices"].append(float(region["unit_price"]))
    for row in own_rows:
        for region in regions_by_offer.get(str(row.get("offer_price_list_item_id") or ""), []):
            if region.get("unit_price") is None:
                continue
            subject_own[str(region.get("subject_code") or "")].append(float(region["unit_price"]))
    subject_rows = []
    for code, bucket in sorted(subject_prices.items(), key=lambda item: str(item[1]["name"])):
        stats = _price_stats(bucket["prices"])
        own_median = _median(subject_own.get(code, []))
        subject_rows.append(
            {
                "code": code,
                "name": bucket["name"],
                "n": len(bucket["prices"]),
                "min": format_price(stats["min"]),
                "median": format_price(stats["median"]),
                "max": format_price(stats["max"]),
                "own": _own_label(own_median),
            }
        )

    variant_buckets: dict[str, dict[str, Any]] = {}
    for row in competitors:
        key = str(row.get("variant_key") or "")
        bucket = variant_buckets.setdefault(
            key,
            {
                "key": key,
                "label": str(row.get("variant_label") or "") or "Без характеристик",
                "prices": [],
                "n": 0,
                "subjects": defaultdict(list),
            },
        )
        bucket["n"] += 1
        if row.get("median_price") is not None:
            bucket["prices"].append(float(row["median_price"]))
        for region in regions_by_offer.get(str(row.get("offer_price_list_item_id") or ""), []):
            if region.get("unit_price") is None:
                continue
            subject_code = str(region.get("subject_code") or "")
            bucket["subjects"][subject_code].append(
                (str(region.get("subject_name") or subject_code), float(region["unit_price"]))
            )
    own_variant_prices: dict[str, list[float]] = defaultdict(list)
    own_variant_subjects: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in own_rows:
        key = str(row.get("variant_key") or "")
        if row.get("median_price") is not None:
            own_variant_prices[key].append(float(row["median_price"]))
        for region in regions_by_offer.get(str(row.get("offer_price_list_item_id") or ""), []):
            if region.get("unit_price") is None:
                continue
            own_variant_subjects[(key, str(region.get("subject_code") or ""))].append(float(region["unit_price"]))

    variant_rows = []
    for key, bucket in variant_buckets.items():
        stats = _price_stats(bucket["prices"])
        own_median = _median(own_variant_prices.get(key, []))
        subject_view = []
        for subject_code, pairs in sorted(bucket["subjects"].items(), key=lambda item: item[1][0][0] if item[1] else item[0]):
            name = pairs[0][0] if pairs else subject_code
            prices = [price for _, price in pairs]
            sub_stats = _price_stats(prices)
            subject_view.append(
                {
                    "name": name,
                    "min": format_price(sub_stats["min"]),
                    "median": format_price(sub_stats["median"]),
                    "max": format_price(sub_stats["max"]),
                    "own": _own_label(_median(own_variant_subjects.get((key, subject_code), []))),
                }
            )
        variant_rows.append(
            {
                "key": key,
                "label": bucket["label"],
                "n": bucket["n"],
                "min": format_price(stats["min"]),
                "median": format_price(stats["median"]),
                "max": format_price(stats["max"]),
                "own": _own_label(own_median),
                "subjects": subject_view,
            }
        )
    variant_rows.sort(key=lambda row: (-int(row["n"]), str(row["label"])))

    countries: Counter[str] = Counter()
    for row in competitors:
        for name in str(row.get("countries") or "").split(" · "):
            label = name.strip()
            if label:
                countries[label] += 1
    days = [int(row["min_delivery_days"]) for row in competitors if row.get("min_delivery_days") is not None]
    qty = [float(row["min_quantity"]) for row in competitors if row.get("min_quantity") is not None]
    chart = _chart_payload(points, variant_rows)
    snapshot_at = ""
    if state and state.get("snapshot_at_utc"):
        snapshot_at = format_msk(str(state.get("snapshot_at_utc") or ""))
    elif offers:
        snapshot_at = format_msk(str(offers[0].get("snapshot_at_utc") or ""))
    return {
        "snapshot_at": snapshot_at,
        "offer_count": len(competitors),
        "supplier_count": len(supplier_rows),
        "own_offer_count": len(own_rows),
        "price_min": format_price(overall["min"]),
        "price_median": format_price(overall["median"]),
        "price_max": format_price(overall["max"]),
        "delivery_median": format_qty(_median([float(v) for v in days])) if days else "—",
        "quantity_median": format_qty(_median(qty)) if qty else "—",
        "countries": [{"name": name, "count": count} for name, count in countries.most_common()],
        "suppliers": supplier_rows,
        "subjects": subject_rows,
        "variants": variant_rows,
        "chart": chart,
    }


def _chart_payload(points: list[dict[str, Any]], variant_rows: list[dict[str, Any]]) -> dict[str, Any]:
    times = sorted({str(row.get("snapshot_at_utc") or "") for row in points if row.get("snapshot_at_utc")})
    latest_counts: dict[str, int] = {}
    if times:
        latest = times[-1]
        for row in points:
            if str(row.get("snapshot_at_utc") or "") == latest:
                latest_counts[str(row.get("variant_key") or "")] = int(row.get("offer_count") or 0)
    labels_by_key: dict[str, str] = {}
    values_by_key: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in points:
        key = str(row.get("variant_key") or "")
        labels_by_key[key] = str(row.get("variant_label") or "") or "Без характеристик"
        values_by_key[key][str(row.get("snapshot_at_utc") or "")] = row
    ranked = sorted(labels_by_key, key=lambda key: (-latest_counts.get(key, 0), labels_by_key[key]))
    if not ranked:
        ranked = [str(row.get("key") or "") for row in variant_rows]
    series = []
    for index, key in enumerate(ranked):
        median_vals = []
        min_vals = []
        max_vals = []
        for stamp in times:
            row = values_by_key.get(key, {}).get(stamp)
            median_vals.append(row.get("price_median") if row else None)
            min_vals.append(row.get("price_min") if row else None)
            max_vals.append(row.get("price_max") if row else None)
        series.append(
            {
                "key": key,
                "label": labels_by_key.get(key) or "Без характеристик",
                "checked": index < CHART_DEFAULT_SERIES,
                "median": median_vals,
                "min": min_vals,
                "max": max_vals,
            }
        )
    return {"labels": [format_msk(stamp) for stamp in times], "series": series}

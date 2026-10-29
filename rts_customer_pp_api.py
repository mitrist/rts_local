"""Customer-side RTS preliminary offer registry API (search / find / compare)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from rts_cookie_ingest import cookie_has_customer_auth_markers
from rts_cookie_profiles import cookies_dir
from rts_invites_poll import load_cookie_header
from rts_ktru_cache import cache_dir_default, load_cached_template, save_cached_template

CUSTOMER_MONITOR_PROFILE = "customer_monitor"

COMPARE_URL = (
    "https://platform.rts-tender.ru/api-gateway/preliminary-offers/"
    "offer-item-comparison/compare"
)
FIND_URL = (
    "https://platform.rts-tender.ru/api-gateway/preliminary-offers/"
    "customer/offer-item-registry/offer-items/find"
)
SEARCH_URL = (
    "https://platform.rts-tender.ru/api-gateway/preliminary-offers/"
    "customer/offer-item-registry/price-list-items/search"
)
KTRU_CODE_URL = "https://platform.rts-tender.ru/api-gateway/dictionary/ktru/code"

# UI defaults from customer registry search dialog.
SEARCH_STATUS_PUBLISHED = 2
SEARCH_SORT_LINK_DATE = 4
SEARCH_SORT_DESC = 1
SEARCH_PAGE_TAKE = 50
OWN_PARTICIPANT_INN = "263605861830"

PP_MONITOR_CARD_URL = "https://offers.rts-tender.ru/customer/offers/offer-item/{offer_price_list_item_id}"

_DEFAULT_TIMEOUT = 60.0
_DEFAULT_PROBE_KTRU = "26.20.13.000-00000002"
_RETRYABLE_HTTP = frozenset({429, 502, 503, 504})


class RtsCustomerPpApiError(Exception):
    def __init__(self, message: str, *, reason: str = "api_error", detail: Any = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RegistryOfferSummary:
    offer_price_list_item_id: str
    ktru_code: str
    name: str
    trade_mark: str
    link_date: str
    validity_date: str
    unit_price: float | None
    card_url: str
    price_list_item_id: int | None = None
    delivery_regions_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "offer_price_list_item_id": self.offer_price_list_item_id,
            "ktru_code": self.ktru_code,
            "name": self.name,
            "trade_mark": self.trade_mark,
            "link_date": self.link_date,
            "validity_date": self.validity_date,
            "unit_price": self.unit_price,
            "card_url": self.card_url,
            "price_list_item_id": self.price_list_item_id,
            "delivery_regions_count": self.delivery_regions_count,
        }


CompareOfferSummary = RegistryOfferSummary


@dataclass(slots=True)
class RegistryDeliveryPlace:
    oktmo_code: str
    oktmo_name: str
    unit_price: float | None
    min_delivery_days: int | None
    min_quantity: float | None


@dataclass(slots=True)
class RegistrySearchItem:
    offer_price_list_item_id: str
    price_list_item_id: int | None
    ktru_code: str
    name: str
    participant_inn: str
    participant_name: str
    participant_id: str
    is_own: bool
    link_date: str
    validity_date: str
    countries: str
    characteristics: list[dict[str, Any]]
    places: list[RegistryDeliveryPlace]


def customer_cookie_path(meta_dir: Path) -> Path:
    raw = os.environ.get("RTS_CUSTOMER_COOKIE_FILE", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return cookies_dir(meta_dir) / "customer_monitor.txt"


def pp_monitor_card_url(offer_price_list_item_id: str) -> str:
    oid = str(offer_price_list_item_id or "").strip()
    return PP_MONITOR_CARD_URL.format(offer_price_list_item_id=oid)


def customer_headers(cookie: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": cookie.strip(),
        "Origin": "https://platform.rts-tender.ru",
        "Referer": "https://offers.rts-tender.ru/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "x-api-request": "True",
        "x-client-app": "Angular",
    }


def load_customer_cookie(meta_dir: Path) -> str:
    path = customer_cookie_path(meta_dir)
    if not path.is_file():
        raise RtsCustomerPpApiError(
            "Файл customer cookie не найден. Войдите в ЛК заказчика РТС и отправьте cookie "
            f"(profile={CUSTOMER_MONITOR_PROFILE}).",
            reason="no_cookie",
        )
    cookie = load_cookie_header(path).strip()
    if not cookie:
        raise RtsCustomerPpApiError("Customer cookie пуст.", reason="no_cookie")
    if not cookie_has_customer_auth_markers(cookie):
        raise RtsCustomerPpApiError(
            "В customer cookie нет сессии РТС (.Auth.Sso / .Auth.Rts.C).",
            reason="no_cookie",
        )
    return cookie


def _format_search_price(value: float | int | None) -> str | None:
    """RTS UI sends whole-number prices as strings without decimals (e.g. \"73300\")."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num == int(num):
        return str(int(num))
    from participation_price import format_money_ru

    return format_money_ru(num)


def _request_json(
    method: str,
    url: str,
    *,
    cookie: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    max_retries: int = 3,
) -> Any:
    retry_delays = (2.0, 5.0, 10.0)
    last_exc: RtsCustomerPpApiError | None = None
    for attempt in range(max(1, int(max_retries))):
        try:
            resp = requests.request(
                method,
                url,
                headers=customer_headers(cookie),
                json=json_body,
                params=params,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise RtsCustomerPpApiError(f"Сеть РТС: {exc}", reason="api_error") from exc
        if resp.status_code in (401, 403):
            raise RtsCustomerPpApiError(
                "Customer-сессия РТС истекла. Обновите cookie через расширение.",
                reason="session_expired",
                detail={"http_status": resp.status_code},
            )
        if resp.status_code in _RETRYABLE_HTTP and attempt + 1 < max_retries:
            time.sleep(retry_delays[min(attempt, len(retry_delays) - 1)])
            continue
        if resp.status_code != 200:
            reason = "rate_limited" if resp.status_code == 429 else "api_error"
            last_exc = RtsCustomerPpApiError(
                f"РТС customer API {resp.status_code}: {(resp.text or '')[:500]}",
                reason=reason,
                detail={"http_status": resp.status_code},
            )
            raise last_exc
        try:
            return resp.json()
        except ValueError as exc:
            raise RtsCustomerPpApiError("РТС customer API вернул не-JSON.", reason="api_error") from exc
    if last_exc is not None:
        raise last_exc
    raise RtsCustomerPpApiError("РТС customer API: исчерпаны повторы.", reason="api_error")


def _parse_rts_price(value: object) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().replace("\xa0", " ").replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _first_okei_name(ktru_template: dict[str, Any]) -> str:
    okeis = ktru_template.get("okeis")
    if isinstance(okeis, list) and okeis:
        first = okeis[0]
        if isinstance(first, dict):
            return str(first.get("fullName") or first.get("name") or "Штука").strip() or "Штука"
    return "Штука"


def build_characteristic_requirements(ktru_template: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    chars = ktru_template.get("characteristics")
    if not isinstance(chars, list):
        return out
    for ch in chars:
        if not isinstance(ch, dict) or not ch.get("isRequired"):
            continue
        ch_id = ch.get("id")
        if ch_id is None:
            continue
        values_raw = ch.get("characteristicValues")
        value_ids: list[int] | str = ""
        if isinstance(values_raw, list):
            for val in values_raw:
                if isinstance(val, dict) and val.get("id") is not None:
                    value_ids = [int(val["id"])]
                    break
        out.append({"dictionaryId": int(ch_id), "dictionaryValuesIds": value_ids})
    return out


def build_compare_payload(
    ktru_template: dict[str, Any],
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = str(ktru_template.get("code") or "").strip()
    name = str(ktru_template.get("name") or code).strip()
    version = int(ktru_template.get("version") or 1)
    now = datetime.now(timezone.utc)
    target_date = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    target_local = now.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    payload: dict[str, Any] = {
        "ktruCode": code,
        "ktruName": name,
        "ktruVersion": version,
        "okeisName": _first_okei_name(ktru_template),
        "deliveryPlaceCode": "",
        "isDeliveryPlaceExactMatch": False,
        "unitPrice": "",
        "quantity": "",
        "maxMismatchCount": 4,
        "deliveryDays": None,
        "priceListName": "",
        "priceListItemName": "",
        "validityDateFrom": "",
        "validityDateTo": "",
        "deliveryPlaceName": "",
        "ktruCharacteristicRequirements": build_characteristic_requirements(ktru_template),
        "isShowNotRequiredCharacteristics": False,
        "notificationNumber": "",
        "targetDate": target_date,
        "targetDateLocal": target_local,
        "includeNewOffersAfterTargetDate": True,
        "isEnabledTargetDate": False,
    }
    if overrides:
        payload.update(overrides)
    return payload


def fetch_ktru_template_api(cookie: str, ktru_code: str, *, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    data = _request_json(
        "GET",
        KTRU_CODE_URL,
        cookie=cookie,
        params={"ktruCode": code},
        timeout=timeout,
    )
    if not isinstance(data, dict) or not str(data.get("code") or "").strip():
        raise RtsCustomerPpApiError(f"КТРУ {code} не найден в справочнике РТС.", reason="ktru_not_found")
    return data


def resolve_ktru_template(
    cookie: str,
    ktru_code: str,
    meta_dir: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    cache_dir = cache_dir_default(meta_dir)
    cached = load_cached_template(cache_dir, ktru_code, ttl_sec=0)
    if cached:
        return cached
    item = fetch_ktru_template_api(cookie, ktru_code, timeout=timeout)
    save_cached_template(cache_dir, ktru_code, item)
    return item


def parse_compare_response(data: dict[str, Any], *, ktru_code: str = "") -> list[CompareOfferSummary]:
    items_raw = data.get("comparisonOfferItems")
    if not isinstance(items_raw, list):
        return []
    out: list[CompareOfferSummary] = []
    ktru_default = str(ktru_code or "").strip()
    for raw in items_raw:
        if not isinstance(raw, dict):
            continue
        oid = str(raw.get("offerPriceListItemId") or "").strip()
        if not oid:
            continue
        ktru_obj = raw.get("ktru") if isinstance(raw.get("ktru"), dict) else {}
        code = str(ktru_obj.get("code") or ktru_default or "").strip()
        unit_price: float | None = None
        places = raw.get("deliveryPlaces")
        if isinstance(places, list) and places:
            first_place = places[0]
            if isinstance(first_place, dict):
                unit_price = _parse_rts_price(first_place.get("unitPrice"))
        out.append(
            CompareOfferSummary(
                offer_price_list_item_id=oid,
                ktru_code=code,
                name=str(raw.get("name") or "").strip(),
                trade_mark=str(raw.get("tradeMark") or "").strip(),
                link_date=str(raw.get("offerPriceListItemLinkDate") or "").strip(),
                validity_date=str(raw.get("validityDate") or "").strip(),
                unit_price=unit_price,
                card_url=pp_monitor_card_url(oid),
            )
        )
    return out


def build_search_payload(
    ktru_code: str,
    *,
    skip: int = 0,
    take: int = 10,
    price_from: float | None = None,
    price_to: float | None = None,
    publish_date_from: str = "",
    publish_date_to: str = "",
    validity_date_from: str = "",
    validity_date_to: str = "",
    sorting_type: int = SEARCH_SORT_LINK_DATE,
    sorting_direction: int = SEARCH_SORT_DESC,
    status: int = SEARCH_STATUS_PUBLISHED,
    search_text: str = "",
    ktru_name: str = "",
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    price_from_text = _format_search_price(price_from)
    price_to_text = _format_search_price(price_to)
    return {
        "searchText": search_text,
        "priceListName": "",
        "priceListItemName": "",
        "ktruName": ktru_name,
        "ktruCode": code,
        "priceFrom": price_from_text,
        "countryOfOriginCodes": [],
        "priceTo": price_to_text,
        "publishDateFrom": publish_date_from,
        "publishDateTo": publish_date_to,
        "validityDateFrom": validity_date_from,
        "validityDateTo": validity_date_to,
        "deliveryPlace": "",
        "deliveryPlaceName": "",
        "availableQuantity": "",
        "deliveryPlaceCode": "",
        "countryOriginName": "",
        "deliveryDays": None,
        "externalParticipantId": None,
        "externalParticipantName": None,
        "ktruCharacteristics": [],
        "offersWithAvailableQuantity": False,
        "status": status,
        "sortingType": sorting_type,
        "offersWithBlockedQuantity": False,
        "removedFromPublishedOfferStatus": 0,
        "sortingDirection": sorting_direction,
        "skip": max(0, int(skip)),
        "take": max(1, int(take)),
    }


def _protected_offer_id(raw: dict[str, Any]) -> str:
    for key in ("offerPriceListItemProtectedId", "protectedOfferItemId", "offerPriceListItemId"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return ""


def _validity_from_search_item(raw: dict[str, Any]) -> str:
    validity = str(raw.get("validityDate") or "").strip()
    if validity:
        return validity
    offer = raw.get("offer")
    if isinstance(offer, dict):
        return str(offer.get("validityDate") or "").strip()
    return ""


def _delivery_regions_count(raw: dict[str, Any]) -> int | None:
    places = raw.get("deliveryPlaces")
    if not isinstance(places, list):
        return None
    if not places:
        return 0
    codes: set[str] = set()
    for place in places:
        if not isinstance(place, dict):
            continue
        oktmo = place.get("oktmo")
        if isinstance(oktmo, dict):
            code = str(oktmo.get("code") or "").strip()
            if code:
                codes.add(code)
    return len(codes) if codes else len(places)


def parse_search_summary(raw: dict[str, Any], *, ktru_code: str = "") -> RegistryOfferSummary | None:
    if not isinstance(raw, dict):
        return None
    oid = _protected_offer_id(raw)
    if not oid:
        return None
    ktru_obj = raw.get("ktru") if isinstance(raw.get("ktru"), dict) else {}
    code = str(ktru_obj.get("code") or ktru_code or "").strip()
    unit_price: float | None = None
    places = raw.get("deliveryPlaces")
    if isinstance(places, list) and places:
        first_place = places[0]
        if isinstance(first_place, dict):
            unit_price = _parse_rts_price(first_place.get("unitPrice"))
    price_list_item_id: int | None = None
    try:
        if raw.get("id") is not None:
            price_list_item_id = int(raw["id"])
    except (TypeError, ValueError):
        price_list_item_id = None
    return RegistryOfferSummary(
        offer_price_list_item_id=oid,
        ktru_code=code,
        name=str(raw.get("name") or "").strip(),
        trade_mark=str(raw.get("tradeMark") or "").strip(),
        link_date=str(raw.get("offerPriceListItemLinkDate") or "").strip(),
        validity_date=_validity_from_search_item(raw),
        unit_price=unit_price,
        card_url=pp_monitor_card_url(oid),
        price_list_item_id=price_list_item_id,
        delivery_regions_count=_delivery_regions_count(raw),
    )


def parse_search_response(data: dict[str, Any], *, ktru_code: str = "") -> tuple[list[RegistryOfferSummary], int]:
    items_raw = data.get("page")
    if not isinstance(items_raw, list):
        return [], 0
    try:
        total = int(data.get("total") or 0)
    except (TypeError, ValueError):
        total = len(items_raw)
    out: list[RegistryOfferSummary] = []
    ktru_default = str(ktru_code or "").strip()
    for raw in items_raw:
        summary = parse_search_summary(raw, ktru_code=ktru_default)
        if summary is not None:
            out.append(summary)
    return out, total


def _parse_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    parsed = _parse_rts_price(value)
    if parsed is None:
        return None
    return int(parsed)


def _countries_label(raw: dict[str, Any]) -> str:
    rows = raw.get("countriesOfOrigin")
    if not isinstance(rows, list):
        return ""
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return " · ".join(names)


def _parse_delivery_place(raw: dict[str, Any]) -> RegistryDeliveryPlace | None:
    oktmo = raw.get("oktmo")
    code = ""
    name = ""
    if isinstance(oktmo, dict):
        code = str(oktmo.get("code") or "").strip()
        name = str(oktmo.get("name") or "").strip()
    elif isinstance(oktmo, str):
        code = oktmo.strip()
    if not code and not name:
        return None
    return RegistryDeliveryPlace(
        oktmo_code=code,
        oktmo_name=name,
        unit_price=_parse_rts_price(raw.get("unitPrice")),
        min_delivery_days=_parse_optional_int(raw.get("minDeliveryDays")),
        min_quantity=_parse_rts_price(raw.get("minQuantity")),
    )


def parse_search_item(raw: dict[str, Any], *, ktru_code: str = "") -> RegistrySearchItem | None:
    """Full registry row for competitor analytics. Does not replace parse_search_summary."""
    if not isinstance(raw, dict):
        return None
    oid = _protected_offer_id(raw)
    if not oid:
        return None
    ktru_obj = raw.get("ktru") if isinstance(raw.get("ktru"), dict) else {}
    code = str(ktru_obj.get("code") or ktru_code or "").strip()
    participant = raw.get("participant") if isinstance(raw.get("participant"), dict) else {}
    inn = str(participant.get("inn") or "").strip()
    chars_raw = raw.get("ktruCharacteristics")
    characteristics = [ch for ch in chars_raw if isinstance(ch, dict)] if isinstance(chars_raw, list) else []
    places_raw = raw.get("deliveryPlaces")
    places: list[RegistryDeliveryPlace] = []
    if isinstance(places_raw, list):
        for place in places_raw:
            if not isinstance(place, dict):
                continue
            parsed = _parse_delivery_place(place)
            if parsed is not None:
                places.append(parsed)
    price_list_item_id: int | None = None
    try:
        if raw.get("id") is not None:
            price_list_item_id = int(raw["id"])
    except (TypeError, ValueError):
        price_list_item_id = None
    return RegistrySearchItem(
        offer_price_list_item_id=oid,
        price_list_item_id=price_list_item_id,
        ktru_code=code,
        name=str(raw.get("name") or "").strip(),
        participant_inn=inn,
        participant_name=str(participant.get("name") or "").strip(),
        participant_id=str(participant.get("id") or "").strip(),
        is_own=inn == OWN_PARTICIPANT_INN,
        link_date=str(raw.get("offerPriceListItemLinkDate") or "").strip(),
        validity_date=_validity_from_search_item(raw),
        countries=_countries_label(raw),
        characteristics=characteristics,
        places=places,
    )


def parse_search_items(data: dict[str, Any], *, ktru_code: str = "") -> tuple[list[RegistrySearchItem], int]:
    items_raw = data.get("page")
    if not isinstance(items_raw, list):
        return [], 0
    try:
        total = int(data.get("total") or 0)
    except (TypeError, ValueError):
        total = len(items_raw)
    out: list[RegistrySearchItem] = []
    default_code = str(ktru_code or "").strip()
    for raw in items_raw:
        if not isinstance(raw, dict):
            continue
        item = parse_search_item(raw, ktru_code=default_code)
        if item is not None:
            out.append(item)
    return out, total


def search_registry_items(
    cookie: str,
    payload: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[list[RegistrySearchItem], int]:
    data = _request_json("POST", SEARCH_URL, cookie=cookie, json_body=payload, timeout=timeout)
    if not isinstance(data, dict):
        raise RtsCustomerPpApiError("Некорректный ответ search.", reason="api_error")
    ktru_code = str(payload.get("ktruCode") or "").strip()
    return parse_search_items(data, ktru_code=ktru_code)


def search_registry_page(
    cookie: str,
    payload: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[list[RegistryOfferSummary], int]:
    data = _request_json("POST", SEARCH_URL, cookie=cookie, json_body=payload, timeout=timeout)
    if not isinstance(data, dict):
        raise RtsCustomerPpApiError("Некорректный ответ search.", reason="api_error")
    ktru_code = str(payload.get("ktruCode") or "").strip()
    return parse_search_response(data, ktru_code=ktru_code)


def compare_offers(
    cookie: str,
    payload: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[CompareOfferSummary]:
    data = _request_json("POST", COMPARE_URL, cookie=cookie, json_body=payload, timeout=timeout)
    if not isinstance(data, dict):
        raise RtsCustomerPpApiError("Некорректный ответ compare.", reason="api_error")
    ktru_code = str(payload.get("ktruCode") or "").strip()
    return parse_compare_response(data, ktru_code=ktru_code)


def find_offer_item(
    cookie: str,
    offer_price_list_item_id: str,
    *,
    include_common_documents: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    oid = str(offer_price_list_item_id or "").strip()
    if not oid:
        raise RtsCustomerPpApiError("offerPriceListItemId не задан.", reason="api_error")
    data = _request_json(
        "POST",
        FIND_URL,
        cookie=cookie,
        json_body={
            "offerPriceListItemId": oid,
            "includeCommonDocuments": include_common_documents,
        },
        timeout=timeout,
    )
    if not isinstance(data, dict):
        raise RtsCustomerPpApiError("Некорректный ответ find.", reason="api_error")
    return data


def probe_customer_session(
    cookie: str,
    *,
    ktru_code: str = _DEFAULT_PROBE_KTRU,
    meta_dir: Path | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    del meta_dir  # search probe does not need KTRU template cache
    code = str(ktru_code or _DEFAULT_PROBE_KTRU).strip()
    try:
        payload = build_search_payload(code, skip=0, take=1)
        _summaries, total = search_registry_page(cookie, payload, timeout=timeout)
    except RtsCustomerPpApiError as exc:
        return {
            "ok": False,
            "channel": "customer_monitor",
            "reason": exc.reason,
            "detail": str(exc)[:500],
        }
    except requests.RequestException as exc:
        return {"ok": False, "channel": "customer_monitor", "reason": "network_error", "detail": str(exc)[:500]}
    return {
        "ok": True,
        "channel": "customer_monitor",
        "ktru_code": code,
        "items_count": total,
        "registry_total": total,
    }


def format_characteristic_value(ch: dict[str, Any]) -> str:
    values_raw = ch.get("characteristicValues")
    value_parts: list[str] = []
    if isinstance(values_raw, list):
        for val in values_raw:
            if not isinstance(val, dict):
                continue
            qd = str(val.get("qualityDescription") or "").strip()
            if qd:
                value_parts.append(qd)
                continue
            conc = str(val.get("concreteValue") or "").strip()
            if conc:
                value_parts.append(conc)
                continue
            min_v = str(val.get("minValue") or "").strip()
            max_v = str(val.get("maxValue") or "").strip()
            if min_v and max_v:
                value_parts.append(f"{min_v} – {max_v}")
            elif min_v:
                value_parts.append(f"≥ {min_v}")
            elif max_v:
                value_parts.append(f"≤ {max_v}")
    return "; ".join(value_parts) if value_parts else "—"


def characteristic_preview_lines(find_item: dict[str, Any], *, limit: int = 5) -> list[str]:
    out: list[str] = []
    chars = find_item.get("ktruCharacteristics")
    if not isinstance(chars, list):
        return out
    for ch in chars:
        if not isinstance(ch, dict):
            continue
        label = str(ch.get("name") or "").strip()
        if not label:
            continue
        out.append(f"{label}: {format_characteristic_value(ch)}")
        if len(out) >= limit:
            break
    return out


def list_characteristic_rows(find_item: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    chars = find_item.get("ktruCharacteristics")
    if not isinstance(chars, list):
        return rows
    for ch in chars:
        if not isinstance(ch, dict):
            continue
        label = str(ch.get("name") or "").strip()
        if not label:
            continue
        rows.append({"name": label, "value": format_characteristic_value(ch)})
    return rows

"""
RTS supplier LK API for ЭЗТ (Preliminary Offers Purchase, App504.aspx).

Цепочка read-only:
  auction/search (notificationNumber) → UrlTradeId
  procedure/model → numeric TradeId, статус, КТРУ
  lot-items/search/paged → характеристики лота
  application/model → ваша заявка (только если LastApplicationId > 0)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from rts_cookie_ingest import cookie_has_auth_markers
from rts_invites_poll import load_cookie_header
from rts_lot_items import DEFAULT_APP_REFERER, DEFAULT_ORIGIN, fetch_lot_items_search_paged

AUCTION_SEARCH_PATH = "/supplier/lk/api/auction/search"
PROCEDURE_MODEL_PATH = "/supplier/lk/api/preliminaryofferspurchase/procedure/model"
APPLICATION_MODEL_PATH = "/supplier/lk/api/preliminaryOffersPurchase/application/model"
TRADE_LOT_REQUIREMENTS_PATH = "/supplier/lk/api/trade-lot-requirements/search"

PRELIMINARY_OFFERS_PURCHASE = "PreliminaryOffersPurchase"
PURCHASE_METHOD_TYPE_EZT = 108

DEFAULT_TIMEOUT = 60.0
DEFAULT_AUCTION_PAGE_SIZE = 10
WAF_RETRY_ATTEMPTS = 2
APP_ORIGIN = "https://app.rts-tender.ru"
GOST_ORIGIN = "https://app-gost.rts-tender.ru"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class RtsPpApplicationApiError(Exception):
    def __init__(self, message: str, *, reason: str = "api_error", detail: Any = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.detail = detail


def load_supplier_cookie(path: Path) -> str:
    if not path.is_file():
        raise RtsPpApplicationApiError(
            f"Файл cookie не найден: {path}. Войдите в ЛК поставщика РТС и сохраните cookie.",
            reason="no_cookie",
        )
    cookie = load_cookie_header(path).strip()
    if not cookie:
        raise RtsPpApplicationApiError("Cookie поставщика пуст.", reason="no_cookie")
    if not cookie_has_auth_markers(cookie):
        raise RtsPpApplicationApiError(
            "В cookie нет сессии РТС (.Auth.Sso / .Auth.Rts.P). Обновите cookie.",
            reason="no_cookie",
        )
    return cookie


ORIGIN_FALLBACK_REASONS = frozenset({"waf", "network"})


def lk_origin_candidates(explicit: str | None = None) -> list[str]:
    raw = str(explicit or os.environ.get("RTS_APP_ORIGIN") or "").strip().rstrip("/")
    if raw:
        return [raw]
    # VM cannot complete TLS to app-gost (Connection reset). LK on app.* is behind Anti-DDoS JS.
    return [APP_ORIGIN, GOST_ORIGIN]


def is_hard_disconnect(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "connection reset" in text or "connection aborted" in text:
        return True
    if "ssl" in text and ("eof" in text or "reset" in text or "wrong version" in text):
        return True
    return isinstance(exc, (requests.exceptions.SSLError, ConnectionResetError))


def is_origin_fallback_error(exc: RtsPpApplicationApiError) -> bool:
    return exc.reason in ORIGIN_FALLBACK_REASONS


def session_lk_origin(session: requests.Session) -> str:
    return str(session.headers.get("Origin") or DEFAULT_ORIGIN).strip().rstrip("/") or DEFAULT_ORIGIN


def apply_cookie_header(session: requests.Session, cookie: str) -> None:
    """Put operator Cookie into the jar so WAF Set-Cookie from warmup can merge."""
    for part in str(cookie or "").split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            session.cookies.set(name, value)


def is_waf_body(text: str, status_code: int) -> bool:
    if int(status_code or 0) not in (403, 429, 502, 503, 520):
        return False
    low = (text or "")[:4000].lower()
    return "anti-ddos" in low or "qrator" in low or "<!doctype html" in low or "<html" in low


def supplier_headers(
    cookie: str = "",
    *,
    referer_page: str = DEFAULT_APP_REFERER,
    origin: str | None = None,
) -> dict[str, str]:
    host = str(origin or DEFAULT_ORIGIN).strip().rstrip("/") or DEFAULT_ORIGIN
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": host,
        "Referer": f"{host}/supplier/lk/{referer_page}",
        "User-Agent": DEFAULT_UA,
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if str(cookie or "").strip():
        headers["Cookie"] = cookie.strip()
    return headers


def supplier_session(
    cookie: str,
    *,
    referer_page: str = DEFAULT_APP_REFERER,
    origin: str | None = None,
) -> requests.Session:
    session = requests.Session()
    session.headers.update(supplier_headers("", referer_page=referer_page, origin=origin))
    apply_cookie_header(session, cookie)
    return session


def warmup_lk_session(session: requests.Session, timeout: float = 8.0) -> str:
    """Return 'ok', 'waf', or 'network' after a cheap GET of App504.aspx."""
    origin = session_lk_origin(session)
    try:
        resp = session.get(f"{origin}/supplier/lk/{DEFAULT_APP_REFERER}", timeout=timeout)
    except requests.RequestException:
        return "network"
    if is_waf_body(resp.text or "", resp.status_code):
        return "waf"
    return "ok"


def _api_error_from_response(resp: requests.Response, *, url: str) -> RtsPpApplicationApiError:
    host = urlparse(str(resp.url or url)).netloc or urlparse(url).netloc
    if is_waf_body(resp.text or "", resp.status_code):
        return RtsPpApplicationApiError(
            f"РТС анти-DDoS HTTP {resp.status_code} ({host}). "
            "Опрос ЭЗТ идёт через app-gost; обновите cookie ЛК, если ошибка повторится.",
            reason="waf",
            detail={"http_status": resp.status_code, "host": host},
        )
    if resp.status_code in (401, 403):
        return RtsPpApplicationApiError(
            "Сессия поставщика РТС истекла. Обновите cookie.",
            reason="session_expired",
            detail={"http_status": resp.status_code, "host": host},
        )
    return RtsPpApplicationApiError(
        f"РТС API {resp.status_code} ({host})",
        reason="api_error",
        detail={"http_status": resp.status_code, "host": host},
    )


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().replace("\xa0", " ").replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None



def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    resp: requests.Response | None = None
    last_exc: Exception | None = None
    for attempt in range(1, WAF_RETRY_ATTEMPTS + 1):
        try:
            resp = session.request(method, url, json=json_body, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            host = urlparse(url).netloc
            if is_hard_disconnect(exc) or attempt >= WAF_RETRY_ATTEMPTS:
                raise RtsPpApplicationApiError(
                    f"Сеть РТС ({host}): {exc}",
                    reason="network",
                    detail={"host": host},
                ) from exc
            time.sleep(1.5 * attempt)
            continue
        if resp.status_code == 200:
            break
        err = _api_error_from_response(resp, url=url)
        if err.reason == "waf" and attempt < WAF_RETRY_ATTEMPTS:
            time.sleep(1.5 * attempt)
            continue
        raise err
    if resp is None:
        host = urlparse(url).netloc
        raise RtsPpApplicationApiError(
            f"Сеть РТС ({host}): {last_exc}",
            reason="network",
            detail={"host": host},
        )
    if resp.status_code != 200:
        raise _api_error_from_response(resp, url=url)
    try:
        return resp.json()
    except ValueError as exc:
        if is_waf_body(resp.text or "", resp.status_code):
            raise _api_error_from_response(resp, url=url) from exc
        raise RtsPpApplicationApiError("РТС API вернул не-JSON.", reason="api_error") from exc


def build_auction_search_payload(
    *,
    notification_number: str | None = None,
    ktru_code: str | None = None,
    page_number: int = 1,
    page_size: int = DEFAULT_AUCTION_PAGE_SIZE,
    aggregate_procedure_types: list[str] | None = None,
    publication_from: str | None = None,
    publication_to: str | None = None,
) -> dict[str, Any]:
    return {
        "notificationNumber": notification_number,
        "name": None,
        "aggregateServicePurchaseSelectedTab": "0",
        "aggregateServicePurchaseStates": None,
        "kladrCodeRegion": "-1",
        "collectivePurchasing": None,
        "hasComplaint": None,
        "withoutApplications": None,
        "organizer": None,
        "useOrganizerName": True,
        "useOrganizerInn": None,
        "customer": None,
        "useCustomerName": True,
        "useCustomerInn": None,
        "startPriceMin": "",
        "startPriceMax": "",
        "favorite": None,
        "publicationDate": {"from": publication_from, "to": publication_to},
        "applicationEndDate": {"from": None, "to": None},
        "tradingDate": {"from": None, "to": None},
        "aggregateProcedureTypes": aggregate_procedure_types,
        "aggregateCustomerLevels": [],
        "okpd2Codes": None,
        "ktruCode": ktru_code,
        "russianPreferenceCodes": [],
        "preferenceCodes": [],
        "restrictionCodes": [],
        "requirementCodes": [],
        "nationalModeCodes": [],
        "_search": False,
        "nd": int(time.time() * 1000),
        "pageSize": max(1, int(page_size)),
        "pageNumber": max(1, int(page_number)),
        "sortingMethod": "",
        "sortingDirection": "asc",
    }


@dataclass(frozen=True, slots=True)
class AuctionSearchRow:
    notification_number: str
    url_trade_id: str
    lot_url_id: str
    purchase_method: str
    purchase_method_label: str
    trade_name: str
    region: str
    organizer_name: str
    start_price: float | None
    lot_state: int | None
    lot_state_label: str
    has_applications: bool
    participant_has_active_application: bool
    active_application_id: str | None
    publication_date: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "notification_number": self.notification_number,
            "url_trade_id": self.url_trade_id,
            "lot_url_id": self.lot_url_id,
            "purchase_method": self.purchase_method,
            "purchase_method_label": self.purchase_method_label,
            "trade_name": self.trade_name,
            "region": self.region,
            "organizer_name": self.organizer_name,
            "start_price": self.start_price,
            "lot_state": self.lot_state,
            "lot_state_label": self.lot_state_label,
            "has_applications": self.has_applications,
            "participant_has_active_application": self.participant_has_active_application,
            "active_application_id": self.active_application_id,
            "publication_date": self.publication_date,
        }


def parse_auction_search_row(raw: dict[str, Any]) -> AuctionSearchRow | None:
    if not isinstance(raw, dict):
        return None
    url_trade_id = _str_or_none(raw.get("TradeId"))
    notification_number = _str_or_none(raw.get("NotificationNumber"))
    if not url_trade_id or not notification_number:
        return None
    return AuctionSearchRow(
        notification_number=notification_number,
        url_trade_id=url_trade_id,
        lot_url_id=_str_or_none(raw.get("TradeLotId")) or "",
        purchase_method=_str_or_none(raw.get("PurchaseMethod")) or "",
        purchase_method_label=_str_or_none(raw.get("PurchaseMethodString")) or "",
        trade_name=_str_or_none(raw.get("TradeName")) or _str_or_none(raw.get("Name")) or "",
        region=_str_or_none(raw.get("Region")) or "",
        organizer_name=_str_or_none(raw.get("OrganizerName")) or "",
        start_price=_float_or_none(raw.get("StartPrice")),
        lot_state=_int_or_none(raw.get("LotState")),
        lot_state_label=_str_or_none(raw.get("LotStateString")) or "",
        has_applications=bool(raw.get("HasApplications")),
        participant_has_active_application=bool(raw.get("ParticipantHasActiveApplication")),
        active_application_id=_str_or_none(raw.get("ActiveApplicationId")),
        publication_date=_str_or_none(raw.get("PublicationDateString")) or "",
    )


def parse_auction_search_response(data: dict[str, Any]) -> tuple[list[AuctionSearchRow], int]:
    if not isinstance(data, dict):
        return [], 0
    outer = data.get("Data")
    if not isinstance(outer, dict):
        return [], 0
    rows_raw = outer.get("Data")
    if not isinstance(rows_raw, list):
        return [], 0
    try:
        total = int(outer.get("Count") or 0)
    except (TypeError, ValueError):
        total = len(rows_raw)
    rows: list[AuctionSearchRow] = []
    for raw in rows_raw:
        if isinstance(raw, dict):
            row = parse_auction_search_row(raw)
            if row is not None:
                rows.append(row)
    return rows, total


def fetch_auction_search(
    session: requests.Session,
    payload: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[list[AuctionSearchRow], int]:
    url = session_lk_origin(session) + AUCTION_SEARCH_PATH
    data = _request_json(session, "POST", url, json_body=payload, timeout=timeout)
    if not isinstance(data, dict) or not data.get("Success", True):
        errors = data.get("Errors") if isinstance(data, dict) else None
        raise RtsPpApplicationApiError(
            f"auction/search ошибка: {errors}",
            reason="api_error",
            detail=data,
        )
    return parse_auction_search_response(data)


def search_by_notification_number(
    session: requests.Session,
    notification_number: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> AuctionSearchRow | None:
    number = str(notification_number or "").strip()
    if not number:
        raise RtsPpApplicationApiError("notification_number не задан.", reason="api_error")
    payload = build_auction_search_payload(notification_number=number)
    rows, _total = fetch_auction_search(session, payload, timeout=timeout)
    for row in rows:
        if row.notification_number == number:
            return row
    return rows[0] if rows else None


def search_ezt_by_ktru(
    session: requests.Session,
    ktru_code: str,
    *,
    publication_from: str | None = None,
    page_size: int = 100,
    timeout: float = DEFAULT_TIMEOUT,
    max_pages: int = 50,
) -> tuple[list[AuctionSearchRow], int]:
    code = str(ktru_code or "").strip()
    if not code:
        raise RtsPpApplicationApiError("ktru_code не задан.", reason="api_error")
    collected: list[AuctionSearchRow] = []
    total = 0
    size = max(1, min(int(page_size), 100))
    pages = max(1, min(int(max_pages), 50))
    for page in range(1, pages + 1):
        payload = build_auction_search_payload(
            ktru_code=code,
            page_number=page,
            page_size=size,
            aggregate_procedure_types=[str(PURCHASE_METHOD_TYPE_EZT)],
            publication_from=publication_from,
        )
        rows, total = fetch_auction_search(session, payload, timeout=timeout)
        collected.extend(rows)
        if not rows or len(collected) >= total:
            break
    return collected, total


def open_lk_session_for_ezt(
    cookie: str,
    *,
    probe_ktru: str,
    publication_from: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[requests.Session, str, list[AuctionSearchRow], int]:
    """Try app.rts-tender.ru, then app-gost. Skip origin on WAF/TLS reset."""
    attempts: list[str] = []
    last_exc: RtsPpApplicationApiError | None = None
    for origin in lk_origin_candidates():
        host = urlparse(origin).netloc or origin
        session = supplier_session(cookie, origin=origin)
        warm = warmup_lk_session(session, timeout=min(timeout, 8.0))
        if warm == "network":
            attempts.append(f"{host}: нет TLS/соединения")
            last_exc = RtsPpApplicationApiError(
                f"Сеть РТС ({host}): соединение сброшено",
                reason="network",
                detail={"host": host},
            )
            continue
        try:
            rows, total = search_ezt_by_ktru(
                session,
                probe_ktru,
                publication_from=publication_from,
                timeout=timeout,
            )
            return session, origin, rows, total
        except RtsPpApplicationApiError as exc:
            last_exc = exc
            attempts.append(f"{host}: {exc}")
            if not is_origin_fallback_error(exc):
                raise
    detail = "; ".join(attempts) if attempts else "нет хостов"
    raise RtsPpApplicationApiError(
        "ЛК РТС с сервера недоступен "
        f"({detail}). auction/search за Anti-DDoS, как Ktru/search. "
        "Обновите RTS Cookie Bridge — опрос ЭЗТ идёт из браузера.",
        reason=last_exc.reason if last_exc is not None else "waf",
        detail={"attempts": attempts},
    )


@dataclass(frozen=True, slots=True)
class ProcedureSummary:
    numeric_trade_id: int
    url_trade_id: str
    numeric_lot_id: int | None
    lot_url_id: str
    notification_number: str
    purchase_name: str
    status_name: str
    status_type: int | None
    ktru_code: str
    nmck_total: float | None
    unit_price: float | None
    quantity: float | None
    application_end_date: str | None
    publication_date: str | None
    last_application_id: int
    last_application_url_id: str | None
    can_view_application: bool
    has_applications_processed: bool
    organizer_inn: str | None
    organizer_name: str | None
    oktmo_codes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_own_application(self) -> bool:
        return self.last_application_id > 0 and self.can_view_application

    @property
    def own_application_url_id(self) -> str | None:
        if not self.has_own_application:
            return None
        return self.last_application_url_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "numeric_trade_id": self.numeric_trade_id,
            "url_trade_id": self.url_trade_id,
            "numeric_lot_id": self.numeric_lot_id,
            "lot_url_id": self.lot_url_id,
            "notification_number": self.notification_number,
            "purchase_name": self.purchase_name,
            "status_name": self.status_name,
            "status_type": self.status_type,
            "ktru_code": self.ktru_code,
            "nmck_total": self.nmck_total,
            "unit_price": self.unit_price,
            "quantity": self.quantity,
            "application_end_date": self.application_end_date,
            "publication_date": self.publication_date,
            "last_application_id": self.last_application_id,
            "last_application_url_id": self.last_application_url_id,
            "can_view_application": self.can_view_application,
            "has_own_application": self.has_own_application,
            "own_application_url_id": self.own_application_url_id,
            "has_applications_processed": self.has_applications_processed,
            "organizer_inn": self.organizer_inn,
            "organizer_name": self.organizer_name,
            "oktmo_codes": list(self.oktmo_codes),
        }


def _extract_oktmo_codes(procedure: dict[str, Any]) -> tuple[str, ...]:
    codes: list[str] = []
    customers = procedure.get("Customers")
    if not isinstance(customers, list):
        return tuple()
    for customer in customers:
        if not isinstance(customer, dict):
            continue
        places = customer.get("OktmoPlaces")
        if not isinstance(places, list):
            continue
        for place in places:
            if not isinstance(place, dict):
                continue
            code = _str_or_none(place.get("OktmoCode"))
            if code and code not in codes:
                codes.append(code)
    return tuple(codes)


def parse_procedure_summary(data: dict[str, Any]) -> ProcedureSummary | None:
    if not isinstance(data, dict):
        return None
    procedure = data.get("CurrentVersionProcedure")
    if not isinstance(procedure, dict):
        return None
    numeric_trade_id = _int_or_none(procedure.get("Id"))
    url_trade_id = _str_or_none(procedure.get("UrlTradeId"))
    if numeric_trade_id is None or not url_trade_id:
        return None

    common = procedure.get("CommonInfo") if isinstance(procedure.get("CommonInfo"), dict) else {}
    contract = procedure.get("ContractObject") if isinstance(procedure.get("ContractObject"), dict) else {}
    condition = contract.get("ConditionInfo") if isinstance(contract.get("ConditionInfo"), dict) else {}
    organizer = procedure.get("OrganizatorInfo") if isinstance(procedure.get("OrganizatorInfo"), dict) else {}

    ktru_code = ""
    unit_price: float | None = None
    quantity: float | None = None
    items = contract.get("ContractObjectListItems")
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            ktru_code = _str_or_none(first.get("ClassificationCode")) or ""
            unit_price = _float_or_none((first.get("PricePerItem") or {}).get("Amount"))
            quantity = _float_or_none(first.get("Quantity"))

    nmck_total = _float_or_none((contract.get("Sum") or {}).get("Amount"))

    last_application_id = _int_or_none(procedure.get("LastApplicationId")) or 0
    last_application_url_id = _str_or_none(procedure.get("LastApplicationUrlId"))

    return ProcedureSummary(
        numeric_trade_id=numeric_trade_id,
        url_trade_id=url_trade_id,
        numeric_lot_id=_int_or_none(procedure.get("LotId")),
        lot_url_id=_str_or_none(procedure.get("LotUrlId")) or "",
        notification_number=_str_or_none(common.get("NotificationNumber")) or "",
        purchase_name=_str_or_none(common.get("NotificationName")) or "",
        status_name=_str_or_none(common.get("StatusName")) or "",
        status_type=_int_or_none(common.get("StatusType")),
        ktru_code=ktru_code,
        nmck_total=nmck_total,
        unit_price=unit_price,
        quantity=quantity,
        application_end_date=_str_or_none(condition.get("ApplicationEndDate")),
        publication_date=_str_or_none(condition.get("PublicationDate")),
        last_application_id=last_application_id,
        last_application_url_id=last_application_url_id,
        can_view_application=bool(procedure.get("IsAvailableViewApplication")),
        has_applications_processed=bool(procedure.get("IsApplicationsProcessed")),
        organizer_inn=_str_or_none(organizer.get("Inn")),
        organizer_name=_str_or_none(organizer.get("Name")),
        oktmo_codes=_extract_oktmo_codes(procedure),
    )


def fetch_procedure_model(
    session: requests.Session,
    *,
    url_trade_id: str = "",
    notification_number: str = "",
    revision_number: str = "",
    adding_action: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    trade_id = str(url_trade_id or "").strip()
    notice = str(notification_number or "").strip()
    if not trade_id and not notice:
        raise RtsPpApplicationApiError(
            "Укажите url_trade_id или notification_number для procedure/model.",
            reason="api_error",
        )
    params = {
        "addingAction": adding_action,
        "id": trade_id,
        "notificationNumber": notice,
        "revisionNumber": revision_number,
    }
    url = session_lk_origin(session) + PROCEDURE_MODEL_PATH
    data = _request_json(session, "GET", url, params=params, timeout=timeout)
    if not isinstance(data, dict):
        raise RtsPpApplicationApiError("procedure/model: ответ не объект.", reason="api_error")
    return data


@dataclass(frozen=True, slots=True)
class ApplicationSummary:
    application_id: int
    application_url_id: str
    application_state: str
    reject_reason: str | None
    quotation_amount: float | None
    trade_lot_price: float | None
    notification_number: str
    lot_state: str | None
    publication_date: str | None
    purchaser_inn: str | None
    purchaser_organization_name: str | None
    url_trade_id: str | None
    numeric_trade_id: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "application_url_id": self.application_url_id,
            "application_state": self.application_state,
            "reject_reason": self.reject_reason,
            "quotation_amount": self.quotation_amount,
            "trade_lot_price": self.trade_lot_price,
            "notification_number": self.notification_number,
            "lot_state": self.lot_state,
            "publication_date": self.publication_date,
            "purchaser_inn": self.purchaser_inn,
            "purchaser_organization_name": self.purchaser_organization_name,
            "url_trade_id": self.url_trade_id,
            "numeric_trade_id": self.numeric_trade_id,
        }


def parse_application_summary(data: dict[str, Any]) -> ApplicationSummary | None:
    if not isinstance(data, dict):
        return None
    application_id = _int_or_none(data.get("ID"))
    application_url_id = _str_or_none(data.get("UrlId"))
    if application_id is None or not application_url_id:
        return None
    return ApplicationSummary(
        application_id=application_id,
        application_url_id=application_url_id,
        application_state=_str_or_none(data.get("ApplicationState")) or "",
        reject_reason=_str_or_none(data.get("RejectReason")),
        quotation_amount=_float_or_none((data.get("Quotation") or {}).get("Amount")),
        trade_lot_price=_float_or_none((data.get("TradeLotPrice") or {}).get("Amount")),
        notification_number=_str_or_none(data.get("NotificationNumber")) or "",
        lot_state=_str_or_none(data.get("LotState")),
        publication_date=_str_or_none(data.get("PublicationDate")),
        purchaser_inn=_str_or_none(data.get("PurchaserInn")),
        purchaser_organization_name=_str_or_none(data.get("PurchaserOrganizationName")),
        url_trade_id=_str_or_none(data.get("UrlTradeId")),
        numeric_trade_id=_int_or_none(data.get("TradeId")),
    )


def fetch_application_model(
    session: requests.Session,
    application_url_id: str,
    *,
    trade_lot_url_id: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    url_id = str(application_url_id or "").strip()
    if not url_id:
        raise RtsPpApplicationApiError("application_url_id не задан.", reason="api_error")
    params = {"Id": url_id, "tradeLotId": str(trade_lot_url_id or "").strip()}
    url = session_lk_origin(session) + APPLICATION_MODEL_PATH
    data = _request_json(session, "GET", url, params=params, timeout=timeout)
    if not isinstance(data, dict):
        raise RtsPpApplicationApiError("application/model: ответ не объект.", reason="api_error")
    return data


def fetch_trade_lot_requirements(
    session: requests.Session,
    url_trade_ids: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    ids = [str(x).strip() for x in url_trade_ids if str(x).strip()]
    if not ids:
        return []
    url = session_lk_origin(session) + TRADE_LOT_REQUIREMENTS_PATH
    data = _request_json(session, "POST", url, json_body={"tradeIds": ids}, timeout=timeout)
    if not isinstance(data, list):
        raise RtsPpApplicationApiError("trade-lot-requirements/search: ожидался массив.", reason="api_error")
    return [item for item in data if isinstance(item, dict)]


@dataclass
class ProcedureBundle:
    search: AuctionSearchRow | None = None
    procedure: ProcedureSummary | None = None
    lot_items: dict[str, Any] | None = None
    application: ApplicationSummary | None = None
    raw_procedure: dict[str, Any] | None = None
    raw_application: dict[str, Any] | None = None
    skip_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "search": self.search.as_dict() if self.search else None,
            "procedure": self.procedure.as_dict() if self.procedure else None,
            "lot_items": self.lot_items,
            "application": self.application.as_dict() if self.application else None,
            "skip_reason": self.skip_reason,
        }


def resolve_procedure(
    session: requests.Session,
    *,
    notification_number: str = "",
    url_trade_id: str = "",
    fetch_lot_items: bool = True,
    fetch_application: bool = True,
    require_preliminary_offers: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> ProcedureBundle:
    """
    Полная цепочка: search (опционально) → procedure/model → lot-items → application/model.
    """
    bundle = ProcedureBundle()
    notice = str(notification_number or "").strip()
    trade_url_id = str(url_trade_id or "").strip()

    if notice and not trade_url_id:
        bundle.search = search_by_notification_number(session, notice, timeout=timeout)
        if bundle.search is None:
            bundle.skip_reason = "not_found"
            return bundle
        trade_url_id = bundle.search.url_trade_id
        if require_preliminary_offers and bundle.search.purchase_method != PRELIMINARY_OFFERS_PURCHASE:
            bundle.skip_reason = "not_preliminary_offers_purchase"
            return bundle

    if not trade_url_id and not notice:
        raise RtsPpApplicationApiError(
            "Укажите notification_number или url_trade_id.",
            reason="api_error",
        )

    raw_procedure = fetch_procedure_model(
        session,
        url_trade_id=trade_url_id,
        notification_number=notice if not trade_url_id else "",
        timeout=timeout,
    )
    bundle.raw_procedure = raw_procedure
    bundle.procedure = parse_procedure_summary(raw_procedure)
    if bundle.procedure is None:
        bundle.skip_reason = "procedure_parse_failed"
        return bundle

    if fetch_lot_items:
        bundle.lot_items = fetch_lot_items_search_paged(
            session,
            bundle.procedure.numeric_trade_id,
            timeout=timeout,
        )

    if not fetch_application:
        return bundle

    own_url_id = bundle.procedure.own_application_url_id
    if not own_url_id:
        bundle.skip_reason = "no_own_application"
        return bundle

    raw_application = fetch_application_model(
        session,
        own_url_id,
        trade_lot_url_id=bundle.procedure.lot_url_id,
        timeout=timeout,
    )
    bundle.raw_application = raw_application
    bundle.application = parse_application_summary(raw_application)
    return bundle


def resolve_procedure_by_notice(
    session: requests.Session,
    purchase_number: str,
    **kwargs: Any,
) -> ProcedureBundle:
    return resolve_procedure(session, notification_number=purchase_number, **kwargs)


def procedure_spa_url(url_trade_id: str) -> str:
    token = str(url_trade_id or "").strip()
    return f"{DEFAULT_ORIGIN}/supplier/lk/App504.aspx#/PreliminaryOffersPurchase/View/{token}"


def application_spa_url(application_url_id: str) -> str:
    token = str(application_url_id or "").strip()
    return (
        f"{DEFAULT_ORIGIN}/supplier/lk/App504.aspx"
        f"#/PreliminaryOffersPurchase/Application/View?Id={token}"
    )

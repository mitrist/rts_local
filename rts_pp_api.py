"""RTS preliminary offer (ПП) draft API client."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

import requests

from rts_cookie_ingest import cookie_has_auth_markers
from rts_invites_poll import load_cookie_header

OFFERS_BASE = "https://offers.rts-tender.ru/api/v1/OfferPriceListItem"
TRADES_BASE = "https://offers.rts-tender.ru/api/v1/Trades"
NATIONAL_MODE_INFO_TYPES_URL = f"{OFFERS_BASE}/national-mode/info-types/"
CREATION_DATA_URL = f"{TRADES_BASE}/offer-price-list-item-creation-data"
KTRU_SEARCH_URL = "https://offers.rts-tender.ru/api-gateway/dictionary/api/v1/Ktru/search"
DRAFT_SEARCH_URL = (
    "https://platform.rts-tender.ru/api-gateway/preliminary-offers/"
    "supplier/offer-price-list-item-registry/draft/search"
)
DRAFT_EDIT_URL = (
    "https://offers.rts-tender.ru/supplier/price-lists/positions/offer-item/edit/{protected_id}"
)
DRAFT_EDIT_BY_ID_URL = (
    "https://offers.rts-tender.ru/supplier/price-lists/positions/offer-item/edit/{draft_id}"
)
NOTICE_OFFER_EDIT_URL = (
    "https://offers.rts-tender.ru/supplier/price-lists/offer/edit"
    "?notificationNumber={notification_number}&starNotificationNumber={star}"
)
OFFERS_DRAFTS_URL = "https://offers.rts-tender.ru/supplier/price-lists/positions"

DEFAULT_VAT_RATE_CODE = "R22"

DEFAULT_DELIVERY: dict[str, Any] = {
    "kladr": None,
    "oktmo": {
        "code": "04500000",
        "name": "Муниципальные округа Красноярского края",
    },
    "unitPrice": "10500,0",
    "minQuantity": "1,0",
    "maxQuantity": "5,0",
    "vatRateCode": DEFAULT_VAT_RATE_CODE,
    "minDeliveryDays": 20,
    "maxDeliveryDays": 40,
    "blocked": "0,0",
    "available": "5,0",
}

_DEFAULT_TIMEOUT = 60.0

_OKTMO_LINE_RE = re.compile(r"^(\d{8,11})\s+(.+)$", re.UNICODE)
_OKTMO_EMBEDDED_RE = re.compile(r"\b(\d{8,11})\s+([^\d|]+)$", re.UNICODE)
_PRICE_CLEAN_RE = re.compile(r"[^\d,]")
_DELIVERY_DAYS_RE = re.compile(r"(\d+)")


class RtsPpApiError(Exception):
    def __init__(self, message: str, *, reason: str = "api_error", detail: Any = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.detail = detail


def load_offers_cookie(path: Path) -> str:
    if not path.is_file():
        raise RtsPpApiError(
            "Файл cookie не найден. Войдите на rts-tender.ru и отправьте cookie через расширение.",
            reason="no_cookie",
        )
    cookie = load_cookie_header(path).strip()
    if not cookie:
        raise RtsPpApiError(
            "Cookie пуст. Войдите на rts-tender.ru и отправьте cookie через расширение.",
            reason="no_cookie",
        )
    if not cookie_has_auth_markers(cookie):
        raise RtsPpApiError(
            "В cookie нет сессии РТС (.Auth.Sso / .Auth.Rts.P). Обновите cookie через расширение.",
            reason="no_cookie",
        )
    return cookie


def probe_offers_session(cookie: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Проверка сессии через API черновиков (работает с сервера, в отличие от Ktru/search)."""
    try:
        resp = requests.post(
            DRAFT_SEARCH_URL,
            headers=offers_headers(cookie),
            json={"ids": [], "page": 0, "size": 1},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return {"ok": False, "channel": "offers", "reason": "network_error", "detail": str(exc)[:500]}
    if resp.status_code in (401, 403):
        return {"ok": False, "channel": "offers", "reason": "http_auth", "http_status": resp.status_code}
    if resp.status_code != 200:
        return {
            "ok": False,
            "channel": "offers",
            "reason": "http_error",
            "http_status": resp.status_code,
            "detail": (resp.text or "")[:300],
        }
    registry_total = 0
    try:
        registry_total = int(resp.json().get("total") or 0)
    except (TypeError, ValueError, AttributeError):
        registry_total = 0
    return {
        "ok": True,
        "channel": "offers",
        "http_status": resp.status_code,
        "registry_total": registry_total,
        "registry_ok": registry_total > 0,
    }


def validate_cookie_session(cookie: str, *, probe: bool = True) -> None:
    if probe:
        result = probe_offers_session(cookie)
        if not result.get("ok"):
            raise RtsPpApiError(
                "Сессия РТС не прошла проверку. Войдите на offers.rts-tender.ru по ЭП "
                "и обновите cookie через расширение.",
                reason="session_expired",
                detail=result,
            )


def platform_registry_headers(cookie: str) -> dict[str, str]:
    headers = offers_headers(cookie)
    headers["Origin"] = "https://platform.rts-tender.ru"
    headers["Referer"] = "https://offers.rts-tender.ru/supplier/price-lists/positions"
    return headers


def offers_headers(cookie: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": cookie.strip(),
        "Origin": "https://offers.rts-tender.ru",
        "Referer": "https://offers.rts-tender.ru/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "x-api-request": "True",
        "x-client-app": "Angular",
    }


def _request_json(
    method: str,
    url: str,
    *,
    cookie: str,
    json_body: dict[str, Any] | list[Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    allow_empty: bool = False,
) -> Any:
    try:
        resp = requests.request(
            method,
            url,
            headers=offers_headers(cookie),
            json=json_body,
            params=params,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RtsPpApiError(f"Сеть РТС: {exc}", reason="api_error") from exc
    if resp.status_code in (401, 403):
        raise RtsPpApiError(
            "Сессия РТС истекла. Обновите cookie через расширение.",
            reason="session_expired",
            detail={"http_status": resp.status_code, "body": (resp.text or "")[:500]},
        )
    if resp.status_code == 204:
        if allow_empty:
            return {}
        raise RtsPpApiError(
            "РТС API 204 No Content (пустой ответ).",
            reason="api_empty",
            detail={"http_status": 204, "url": url},
        )
    if resp.status_code != 200:
        raise RtsPpApiError(
            f"РТС API {resp.status_code}: {(resp.text or '')[:500]}",
            reason="api_error",
            detail={"http_status": resp.status_code},
        )
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, (dict, list)) else {}
    try:
        return resp.json()
    except ValueError as exc:
        raise RtsPpApiError("РТС API вернул не-JSON.", reason="api_error") from exc


def _pick_ktru_item(page: list[Any], code: str) -> dict[str, Any] | None:
    for item in page:
        if isinstance(item, dict) and str(item.get("code") or "").strip() == code:
            return item
    return None


def _extract_ktru_from_search(data: Any, code: str) -> dict[str, Any] | None:
    """LK may return a single KTRU object or a paged ``{page:[...]}`` envelope."""
    if isinstance(data, list):
        return _pick_ktru_item(data, code)
    if not isinstance(data, dict):
        return None
    if str(data.get("code") or "").strip() == code and (
        "characteristics" in data or "okeis" in data or "okpd2" in data or "version" in data
    ):
        return data
    for key in ("page", "items", "result", "data"):
        nested = data.get(key)
        if isinstance(nested, list):
            found = _pick_ktru_item(nested, code)
            if found:
                return found
        if isinstance(nested, dict):
            found = _extract_ktru_from_search(nested, code)
            if found:
                return found
    return None


def _ktru_search_items(data: Any) -> list[dict[str, Any]]:
    """Flatten Ktru/search JSON (single item, page[], or nested envelopes)."""
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for row in data:
            out.extend(_ktru_search_items(row))
        return out
    if not isinstance(data, dict):
        return []
    code = str(data.get("code") or "").strip()
    if code and (
        "characteristics" in data or "okeis" in data or "okpd2" in data or "version" in data or "name" in data
    ):
        out.append(data)
    for key in ("page", "items", "result", "data"):
        nested = data.get(key)
        if isinstance(nested, (list, dict)):
            out.extend(_ktru_search_items(nested))
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in out:
        code = str(item.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        unique.append(item)
    return unique


def search_ktru_by_name(
    cookie: str,
    query: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    take: int = 20,
) -> list[dict[str, Any]]:
    """Search KTRU dictionary by name or code substring. Returns raw items (may be partial)."""
    q = str(query or "").strip()
    if not q:
        return []
    size = max(1, min(int(take), 50))
    bodies: list[dict[str, Any]] = [
        {"code": "", "name": q, "skip": 0, "take": size, "sorting": []},
        {"code": q, "name": "", "skip": 0, "take": size, "sorting": []},
        {"searchText": q, "page": 0, "size": size, "onlyActual": True},
    ]
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for body in bodies:
        data = _request_json("POST", KTRU_SEARCH_URL, cookie=cookie, json_body=body, timeout=timeout)
        for item in _ktru_search_items(data):
            code = str(item.get("code") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            merged.append(item)
            if len(merged) >= size:
                return merged
    return merged


def search_ktru(cookie: str, ktru_code: str, *, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    if not code:
        raise RtsPpApiError("Код КТРУ не задан.", reason="ktru_not_found")

    okpd_prefix = code.rsplit("-", 1)[0] if "-" in code else ""
    # Primary contract matches browser DevTools dumps (skip/take).
    bodies: list[dict[str, Any]] = [
        {"code": code, "name": "", "skip": 0, "take": 10, "sorting": []},
        {"code": code, "page": 0, "size": 20, "onlyActual": True},
        {"code": code, "page": 0, "size": 20, "onlyActual": False},
        {"searchText": code, "page": 0, "size": 50, "onlyActual": True},
        {"searchText": code, "page": 0, "size": 50, "onlyActual": False},
    ]
    if okpd_prefix and okpd_prefix != code:
        bodies.append({"searchText": okpd_prefix, "page": 0, "size": 50, "onlyActual": True})

    for body in bodies:
        data = _request_json("POST", KTRU_SEARCH_URL, cookie=cookie, json_body=body, timeout=timeout)
        item = _extract_ktru_from_search(data, code)
        if item:
            return item

    raise RtsPpApiError(f"КТРУ {code} не найден в справочнике РТС.", reason="ktru_not_found")


def stub_ktru_item(ktru_code: str, *, ktru_name: str | None = None) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    name = str(ktru_name or "").strip() or code
    return {
        "code": code,
        "name": name,
        "version": 1,
        "okeis": [{"code": "796", "fullName": "Штука"}],
        "characteristics": [],
    }


def resolve_ktru_for_create(
    cookie: str,
    ktru_code: str,
    *,
    ktru_name: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    cache_dir: Path | None = None,
    allow_stub: bool = False,
) -> dict[str, Any]:
    if cache_dir is not None:
        from rts_ktru_cache import get_ktru_template

        return get_ktru_template(
            cookie,
            ktru_code,
            cache_dir,
            timeout=timeout,
            allow_stub=allow_stub,
            ktru_name=ktru_name,
        )
    try:
        return search_ktru(cookie, ktru_code, timeout=timeout)
    except RtsPpApiError as exc:
        if exc.reason != "ktru_not_found":
            raise
        if allow_stub:
            return stub_ktru_item(ktru_code, ktru_name=ktru_name)
        raise RtsPpApiError(
            f"КТРУ {ktru_code} не найден в справочнике РТС.",
            reason="ktru_catalog_unavailable",
            detail={"ktru_code": ktru_code},
        ) from exc


def _parse_rts_num(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().replace("\xa0", " ")
    if not raw:
        return None
    raw = raw.replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _math_notation(value: Any) -> str:
    return str(value or "").strip().lower()


def _notation_compatible(want: str, got: str) -> bool:
    if not want or not got:
        return True
    return want == got


def _numeric_option_exact_match(cv: dict[str, Any], opt: dict[str, Any]) -> bool:
    cv_conc = _parse_rts_num(cv.get("concreteValue"))
    if cv_conc is not None:
        opt_conc = _parse_rts_num(opt.get("concreteValue"))
        return opt_conc is not None and cv_conc == opt_conc

    cv_min = _parse_rts_num(cv.get("minValue"))
    cv_max = _parse_rts_num(cv.get("maxValue"))
    if cv_min is None and cv_max is None:
        return False

    opt_min = _parse_rts_num(opt.get("minValue"))
    opt_max = _parse_rts_num(opt.get("maxValue"))

    if cv_min is not None:
        if opt_min is None or cv_min != opt_min:
            return False
        if not _notation_compatible(_math_notation(cv.get("minMathNotation")), _math_notation(opt.get("minMathNotation"))):
            return False
    if cv_max is not None:
        if opt_max is None or cv_max != opt_max:
            return False
        if not _notation_compatible(_math_notation(cv.get("maxMathNotation")), _math_notation(opt.get("maxMathNotation"))):
            return False
    return True


def _numeric_option_contains(cv: dict[str, Any], opt: dict[str, Any]) -> bool:
    """Notice range fits inside a template preset range (e.g. 105–150 within 100–200)."""
    cv_min = _parse_rts_num(cv.get("minValue"))
    cv_max = _parse_rts_num(cv.get("maxValue"))
    if cv_min is None or cv_max is None:
        return False
    opt_min = _parse_rts_num(opt.get("minValue"))
    opt_max = _parse_rts_num(opt.get("maxValue"))
    if opt_min is None or opt_max is None:
        return False
    if cv_min < opt_min or cv_max > opt_max:
        return False
    return _notation_compatible(_math_notation(cv.get("minMathNotation")), _math_notation(opt.get("minMathNotation"))) and _notation_compatible(
        _math_notation(cv.get("maxMathNotation")), _math_notation(opt.get("maxMathNotation"))
    )


def _resolve_value_option(
    cv: dict[str, Any],
    options: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not options:
        return None
    qd = str(cv.get("qualityDescription") or "").strip().lower()
    if qd:
        for opt in options:
            oq = str(opt.get("qualityDescription") or "").strip().lower()
            if oq and oq == qd:
                return opt
        for opt in options:
            oq = str(opt.get("qualityDescription") or "").strip()
            if oq and (qd in oq.lower() or oq.lower() in qd):
                return opt
    vc = str(cv.get("valueCode") or "").strip()
    if vc:
        for opt in options:
            if str(opt.get("valueCode") or "").strip() == vc:
                return opt
    if cv.get("minValue") or cv.get("maxValue") or cv.get("concreteValue"):
        for opt in options:
            if _numeric_option_exact_match(cv, opt):
                return opt
        for opt in options:
            if _numeric_option_contains(cv, opt):
                return opt
        if len(options) == 1:
            return options[0]
        return None
    return options[0] if len(options) == 1 else None


def _characteristic_value_for_api(
    cv: dict[str, Any],
    *,
    options: list[dict[str, Any]] | None = None,
    characteristic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    okei = cv.get("okei") if isinstance(cv.get("okei"), dict) else {}
    fmt = cv.get("format") if isinstance(cv.get("format"), dict) else {}
    ch = characteristic or {}
    value_id = None
    matched = _resolve_value_option(cv, options) if options else None
    src = matched if isinstance(matched, dict) else cv
    if matched is not None:
        value_id = matched.get("id")
    if value_id is None:
        raw_id = cv.get("id")
        if raw_id is not None and options:
            for opt in options:
                if opt.get("id") == raw_id:
                    value_id = raw_id
                    break
        elif raw_id is not None and not options:
            value_id = raw_id
    if value_id is not None and options:
        allowed = {_int_id(opt.get("id")) for opt in options}
        if _int_id(value_id) not in allowed:
            value_id = None
    char_id = _int_id(
        ch.get("id"),
        cv.get("characteristicId"),
        (matched or {}).get("characteristicId") if isinstance(matched, dict) else None,
    )
    out: dict[str, Any] = {
        "qualityDescription": src.get("qualityDescription")
        if src.get("qualityDescription") is not None
        else cv.get("qualityDescription"),
        "concreteValue": cv.get("concreteValue"),
        "minValue": cv.get("minValue"),
        "maxValue": cv.get("maxValue"),
        "productCharacteristic": cv.get("productCharacteristic"),
        "minMathNotation": cv.get("minMathNotation"),
        "maxMathNotation": cv.get("maxMathNotation"),
        "characteristicCode": ch.get("code") if ch.get("code") is not None else cv.get("characteristicCode"),
        "characteristicName": ch.get("name") if ch.get("name") is not None else cv.get("characteristicName"),
        "valueCode": src.get("valueCode") if src.get("valueCode") is not None else cv.get("valueCode"),
        "okeiCode": cv.get("okeiCode") if cv.get("okeiCode") is not None else okei.get("code", ""),
        "okeiFullName": cv.get("okeiFullName", okei.get("fullName")),
        "valueFormatId": cv.get("valueFormatId", fmt.get("id")),
    }
    if char_id is not None:
        out["characteristicId"] = char_id
    if value_id is not None:
        out["id"] = int(value_id)
    return out


def _int_id(*candidates: Any) -> int | None:
    for raw in candidates:
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _map_characteristic_value(cv: dict[str, Any]) -> dict[str, Any]:
    return _characteristic_value_for_api(cv)


def _fix_characteristic_value_ids(item: dict[str, Any], ktru_item: dict[str, Any]) -> None:
    tpl_by_code = {
        str(row.get("code") or "").strip(): row
        for row in (ktru_item.get("characteristics") or [])
        if isinstance(row, dict) and str(row.get("code") or "").strip()
    }
    for ch in item.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict):
            continue
        code = str(ch.get("code") or "").strip()
        options = _search_char_options(ktru_item, code)
        tpl = tpl_by_code.get(code) or {}
        parent = dict(ch)
        if _int_id(parent.get("id")) is None and _int_id(tpl.get("id")) is not None:
            parent["id"] = tpl.get("id")
        fixed: list[dict[str, Any]] = []
        for cv in ch.get("characteristicValues") or []:
            if isinstance(cv, dict):
                fixed.append(_characteristic_value_for_api(cv, options=options, characteristic=parent))
        if fixed:
            ch["characteristicValues"] = fixed


def _map_create_characteristic(ch: dict[str, Any]) -> dict[str, Any]:
    values = [_map_characteristic_value(cv) for cv in (ch.get("characteristicValues") or []) if isinstance(cv, dict)]
    selected: list[dict[str, Any]] = []
    if ch.get("isRequired") and values:
        kind = ch.get("characteristicKind") if isinstance(ch.get("characteristicKind"), dict) else {}
        kind_name = str(kind.get("name") or "")
        if "Неизменяемая" in kind_name:
            selected = [values[0]]
    return {
        "id": ch.get("id"),
        "code": ch.get("code"),
        "name": ch.get("name"),
        "isActual": ch.get("isActual", True),
        "isRequired": ch.get("isRequired", False),
        "characteristicType": ch.get("characteristicType"),
        "characteristicKind": ch.get("characteristicKind"),
        "characteristicChoiceType": ch.get("characteristicChoiceType"),
        "characteristicValues": selected,
    }


def build_create_payload(ktru_item: dict[str, Any], *, unit_from_line: str | None = None) -> dict[str, Any]:
    okeis = ktru_item.get("okeis") or []
    unit = {"code": "796", "name": "Штука"}
    if okeis and isinstance(okeis[0], dict):
        unit = {"code": okeis[0].get("code", "796"), "name": okeis[0].get("fullName", "Штука")}
    if unit_from_line and str(unit_from_line).strip():
        unit = {"code": unit.get("code", "796"), "name": str(unit_from_line).strip()}
    chars = [
        _map_create_characteristic(ch)
        for ch in (ktru_item.get("characteristics") or [])
        if isinstance(ch, dict)
    ]
    return {
        "ktru": {
            "code": ktru_item.get("code"),
            "name": ktru_item.get("name"),
            "okpd2": ktru_item.get("okpd2"),
            "version": ktru_item.get("version"),
        },
        "unit": unit,
        "tradeMark": None,
        "certificateNameMedicalProduct": None,
        "documentExtractUrlMedicalProduct": None,
        "certificateNumberMedicalProduct": None,
        "countriesOfOrigin": [],
        "softwareCountriesOfOrigin": [],
        "ktruCharacteristics": chars,
        "minpromtorgInfos": [
            {"npaCode": c, "registryNumber": "", "productsFirstLevelComplianceType": None, "summaryScores": "", "isSelected": False}
            for c in ("N616", "N617", "N878", "N1236")
        ],
        "minpromtorgInfoDocuments": [],
        "nationalModeInfoGroups": [],
        "softwareNationalModeInfoGroups": [],
        "deliveryPlaces": [],
        "countriesOfOriginDocuments": [],
        "otherDocuments": [],
        "m2DDocuments": [],
    }


def create_draft(cookie: str, payload: dict[str, Any], *, timeout: float = _DEFAULT_TIMEOUT) -> int:
    data = _request_json("POST", f"{OFFERS_BASE}/draft/create", cookie=cookie, json_body=payload, timeout=timeout)
    if not isinstance(data, dict) or not data.get("id"):
        raise RtsPpApiError("РТС не вернул id черновика.", reason="api_error", detail=data)
    return int(data["id"])


def star_notification_number(purchase_number: str, *, lot_index: int = 1) -> str:
    """MVP star id for RTS notice create: ``l{notificationNumber}-{lot}``."""
    pn = str(purchase_number or "").strip()
    idx = max(1, int(lot_index))
    if not pn:
        raise RtsPpApiError("Номер извещения не задан.", reason="bad_request")
    return f"l{pn}-{idx}"


def notice_offer_edit_url(purchase_number: str, *, star: str | None = None) -> str:
    pn = str(purchase_number or "").strip()
    star_val = str(star or "").strip() or star_notification_number(pn)
    return NOTICE_OFFER_EDIT_URL.format(notification_number=pn, star=star_val)


CREATION_DATA_EMPTY_BACKOFFS: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0, 10.0)
MAX_STAR_LOT_INDEX = 8


def fetch_offer_creation_data(
    cookie: str,
    notification_number: str,
    star: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """GET Trades/offer-price-list-item-creation-data for an EIS notice position."""
    pn = str(notification_number or "").strip()
    star_val = str(star or "").strip()
    if not pn or not star_val:
        raise RtsPpApiError(
            "Нужны notificationNumber и starNotificationNumber.",
            reason="bad_request",
        )
    try:
        data = _request_json(
            "GET",
            CREATION_DATA_URL,
            cookie=cookie,
            params={"notificationNumber": pn, "starNotificationNumber": star_val},
            timeout=timeout,
        )
    except RtsPpApiError as exc:
        if exc.reason == "api_empty":
            raise RtsPpApiError(
                "РТС ещё не готов (creation-data HTTP 204) для "
                f"starNotificationNumber={star_val}.",
                reason="creation_data_empty",
                detail={"notificationNumber": pn, "star": star_val},
            ) from exc
        raise
    if not isinstance(data, dict):
        raise RtsPpApiError(
            "creation-data вернул не-объект.",
            reason="api_error",
            detail=data,
        )
    if not str(data.get("ktruCode") or "").strip():
        raise RtsPpApiError(
            "РТС ещё не готов (creation-data без ktruCode) для "
            f"starNotificationNumber={star_val}.",
            reason="creation_data_empty",
            detail={"notificationNumber": pn, "star": star_val, "body": data},
        )
    return data


def _lot_indices_for_probe(
    *,
    preferred_lot_index: int | None,
    max_lot_index: int,
) -> list[int]:
    max_idx = max(1, int(max_lot_index))
    preferred = max(1, int(preferred_lot_index)) if preferred_lot_index is not None else None
    ordered: list[int] = []
    if preferred is not None and preferred <= max_idx:
        ordered.append(preferred)
    for idx in range(1, max_idx + 1):
        if idx not in ordered:
            ordered.append(idx)
    return ordered


def resolve_offer_creation_data(
    cookie: str,
    notification_number: str,
    *,
    expected_ktru: str | None = None,
    allowed_ktru: set[str] | None = None,
    preferred_lot_index: int | None = None,
    max_lot_index: int = MAX_STAR_LOT_INDEX,
    empty_backoffs: tuple[float, ...] = CREATION_DATA_EMPTY_BACKOFFS,
    sleep: Callable[[float], None] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[dict[str, Any], str]:
    """Fetch creation-data with star probe and retry on empty (HTTP 204).

    Returns ``(creation_data, starNotificationNumber)``.
    When ``expected_ktru`` is set, probes lots until ktruCode matches.
    When ``allowed_ktru`` is set (and expected is not), returns first lot whose
    ktruCode is in the set.
    """
    pn = str(notification_number or "").strip()
    if not pn:
        raise RtsPpApiError("Номер извещения не задан.", reason="bad_request")
    sleep_fn = sleep or time.sleep
    want = str(expected_ktru or "").strip() or None
    allowed = {str(x).strip() for x in (allowed_ktru or set()) if str(x).strip()} or None
    indices = _lot_indices_for_probe(
        preferred_lot_index=preferred_lot_index,
        max_lot_index=max_lot_index,
    )
    waits = (0.0, *tuple(float(x) for x in empty_backoffs))
    last_empty: RtsPpApiError | None = None
    mismatch_detail: dict[str, Any] | None = None

    for wait in waits:
        if wait > 0:
            sleep_fn(wait)
        saw_non_empty = False
        for lot_idx in indices:
            star = star_notification_number(pn, lot_index=lot_idx)
            try:
                data = fetch_offer_creation_data(cookie, pn, star, timeout=timeout)
            except RtsPpApiError as exc:
                if exc.reason == "creation_data_empty":
                    last_empty = exc
                    continue
                raise
            saw_non_empty = True
            got = str(data.get("ktruCode") or "").strip()
            if want:
                if got == want:
                    return data, star
                mismatch_detail = {
                    "notificationNumber": pn,
                    "star": star,
                    "creation_ktru": got,
                    "expected_ktru": want,
                }
                continue
            if allowed is not None:
                if got in allowed:
                    return data, star
                continue
            return data, star
        if saw_non_empty and want:
            raise RtsPpApiError(
                f"КТРУ creation-data не совпадает с позицией «{want}» "
                f"после probe star l{{pn}}-1…{max(indices)} "
                f"(starNotificationNumber, вероятно, не та позиция).",
                reason="star_mismatch",
                detail=mismatch_detail or {"expected_ktru": want, "notificationNumber": pn},
            )
        if saw_non_empty and allowed is not None:
            raise RtsPpApiError(
                "Ни одна позиция creation-data (probe star) не попала в каталог КТРУ.",
                reason="star_mismatch",
                detail={"notificationNumber": pn, "allowed_count": len(allowed)},
            )
        # all empty — retry after backoff
    raise RtsPpApiError(
        "РТС ещё не готов (creation-data HTTP 204) после retry; "
        f"probe star l{pn}-1…{max(indices)}. Позиция может быть недоступна для подачи ПП.",
        reason="creation_data_empty",
        detail={
            "notificationNumber": pn,
            "stars_probed": [star_notification_number(pn, lot_index=i) for i in indices],
            "last_empty": getattr(last_empty, "detail", None),
        },
    )


def create_temporary_offer(
    cookie: str,
    payload: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> int:
    """POST OfferPriceListItem/temporary/create — Temporary item linked to notice flow."""
    data = _request_json(
        "POST",
        f"{OFFERS_BASE}/temporary/create",
        cookie=cookie,
        json_body=payload,
        timeout=timeout,
    )
    if not isinstance(data, dict) or not data.get("id"):
        raise RtsPpApiError(
            "РТС не вернул id temporary-предложения.",
            reason="api_error",
            detail=data,
        )
    return int(data["id"])


def get_draft(cookie: str, draft_id: int, *, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    data = _request_json("GET", f"{OFFERS_BASE}/{int(draft_id)}", cookie=cookie, timeout=timeout)
    if not isinstance(data, dict):
        raise RtsPpApiError("Некорректный ответ GET черновика.", reason="api_error")
    return data


def update_draft(cookie: str, item: dict[str, Any], *, timeout: float = _DEFAULT_TIMEOUT) -> None:
    _request_json("POST", f"{OFFERS_BASE}/draft/update", cookie=cookie, json_body=item, timeout=timeout)


def fetch_national_mode_info_types(
    cookie: str,
    okpd2_code: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """GET national-mode/info-types/?okpd2Code=… — allowed ПП 1875 types for OKPD2."""
    code = str(okpd2_code or "").strip()
    if not code:
        return {}
    data = _request_json(
        "GET",
        NATIONAL_MODE_INFO_TYPES_URL,
        cookie=cookie,
        params={"okpd2Code": code},
        timeout=timeout,
    )
    return data if isinstance(data, dict) else {}


def _draft_search_page(
    cookie: str,
    body: dict[str, Any],
    *,
    headers_fn: Callable[[str], dict[str, str]] = offers_headers,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    try:
        resp = requests.post(
            DRAFT_SEARCH_URL,
            headers=headers_fn(cookie),
            json=body,
            timeout=timeout,
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    page = data.get("page") or []
    return [row for row in page if isinstance(row, dict)]


def draft_search_by_id(
    cookie: str,
    draft_id: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    poll_retries: int = 3,
    poll_delay: float = 1.0,
) -> dict[str, Any] | None:
    """Resolve draft card from platform registry (has protectedId for edit URL)."""
    bodies = [
        {"ids": [int(draft_id)], "page": 0, "size": 10},
        {"protectedIds": [str(draft_id)], "page": 0, "size": 10},
    ]
    header_fns = (offers_headers, platform_registry_headers)
    attempts = max(1, int(poll_retries))
    for attempt in range(attempts):
        if attempt and poll_delay > 0:
            time.sleep(poll_delay)
        for hdr_fn in header_fns:
            for body in bodies:
                page = _draft_search_page(cookie, body, headers_fn=hdr_fn, timeout=timeout)
                if page:
                    return page[0]
    return None


def pp_draft_direct_edit_url(draft_id: int | None) -> str:
    try:
        num = int(draft_id) if draft_id is not None else 0
    except (TypeError, ValueError):
        num = 0
    if num > 0:
        return DRAFT_EDIT_BY_ID_URL.format(draft_id=num)
    return ""


def draft_edit_url(protected_id: str, *, draft_id: int | None = None) -> str:
    pid = str(protected_id or "").strip()
    if pid:
        return DRAFT_EDIT_URL.format(protected_id=pid)
    try:
        num = int(draft_id) if draft_id is not None else 0
    except (TypeError, ValueError):
        num = 0
    if num > 0:
        # Numeric /edit/{id} does not load RTS form (UI reads draft/search by protectedId).
        return OFFERS_DRAFTS_URL
    return ""


def _search_char_options(ktru_item: dict[str, Any], code: str) -> list[dict[str, Any]]:
    for ch in ktru_item.get("characteristics") or []:
        if isinstance(ch, dict) and str(ch.get("code") or "") == code:
            return [cv for cv in (ch.get("characteristicValues") or []) if isinstance(cv, dict)]
    return []


def _set_characteristic_values(
    ch: dict[str, Any],
    cvs: list[dict[str, Any]],
    *,
    options: list[dict[str, Any]] | None = None,
) -> None:
    ch["characteristicValues"] = [
        _characteristic_value_for_api(cv, options=options, characteristic=ch)
        for cv in cvs
        if isinstance(cv, dict)
    ]


def _set_characteristic_value(
    ch: dict[str, Any],
    cv: dict[str, Any],
    *,
    options: list[dict[str, Any]] | None = None,
) -> None:
    _set_characteristic_values(ch, [cv], options=options)


def fill_required_characteristics(
    item: dict[str, Any],
    match_rows: list[dict[str, Any]] | None = None,
    *,
    ktru_search_item: dict[str, Any] | None = None,
) -> None:
    """Заполняет обязательные характеристики; match_rows — строки из rank_offer_matches."""
    offer_by_key: dict[str, str] = {}
    for row in match_rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "match":
            continue
        key = str(row.get("match_key") or "").strip()
        val = str(row.get("offer_value") or "").strip()
        if key and val:
            offer_by_key[key] = val

    for ch in item.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict) or ch.get("characteristicValues"):
            continue
        code = str(ch.get("code") or "")
        label = str(ch.get("name") or "").strip().lower()
        match_key = f"name:{label}" if label else ""
        offer_val = offer_by_key.get(match_key, "")
        if offer_val:
            options = _search_char_options(search_item, code)
            if options:
                matched = _resolve_value_option({"qualityDescription": offer_val}, options)
                _set_characteristic_value(ch, matched or options[0])
            else:
                ch["characteristicValues"] = [
                    _characteristic_value_for_api(
                        {"qualityDescription": offer_val},
                        characteristic=ch,
                    )
                ]

    search_item = ktru_search_item or {}
    for ch in item.get("ktruCharacteristics") or []:
        if not isinstance(ch, dict) or not ch.get("isRequired") or ch.get("characteristicValues"):
            continue
        options = _search_char_options(search_item, str(ch.get("code") or ""))
        if options:
            _set_characteristic_value(ch, options[0])


def parse_oktmo_from_delivery_place(text: str) -> dict[str, str] | None:
    """Parse '34701000001 Муниципальные образования …' from EIS delivery place."""
    raw = " ".join((text or "").split())
    if not raw:
        return None
    for segment in (part.strip() for part in raw.split("|")):
        if not segment:
            continue
        match = _OKTMO_LINE_RE.match(segment)
        if match:
            return {"code": match.group(1), "name": match.group(2).strip()}
        embedded = _OKTMO_EMBEDDED_RE.search(segment)
        if embedded:
            name = embedded.group(2).strip(" -—\t")
            if name:
                return {"code": embedded.group(1), "name": name}
    return None


def format_rts_quantity(text: str) -> str:
    """EIS '1 053,00' -> RTS '1053,00' (то же правило, что и для цены за единицу)."""
    return format_rts_unit_price(text)


def format_rts_unit_price(text: str) -> str:
    """EIS '2 200,00' -> RTS '2200,00'."""
    raw = (text or "").strip()
    if not raw:
        return ""
    compact = raw.replace("\xa0", " ").replace(" ", "")
    compact = compact.replace(".", ",")
    compact = _PRICE_CLEAN_RE.sub("", compact)
    if not compact or "," not in compact:
        digits = re.sub(r"\D", "", compact)
        if not digits:
            return ""
        return f"{digits},0"
    whole, frac = compact.split(",", 1)
    if not whole:
        return ""
    frac = (frac + "00")[:2]
    return f"{whole},{frac}"


def parse_notice_delivery_days(text: str) -> tuple[int, int] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    match = _DELIVERY_DAYS_RE.search(raw)
    if not match:
        return None
    days = int(match.group(1))
    if days <= 0:
        return None
    return days, days


def _apply_notice_quantity(place: dict[str, Any], quantity_text: str | None) -> bool:
    qty = format_rts_quantity(quantity_text or "")
    if not qty:
        return False
    place["minQuantity"] = qty
    place["maxQuantity"] = qty
    place["available"] = qty
    place["blocked"] = "0,0"
    return True


def build_delivery_place_from_notice(
    *,
    delivery_place: str | None = None,
    price_per_unit: str | None = None,
    delivery_days: str | None = None,
    quantity_text: str | None = None,
) -> dict[str, Any]:
    place = dict(DEFAULT_DELIVERY)
    oktmo = parse_oktmo_from_delivery_place(delivery_place or "")
    if oktmo:
        place["oktmo"] = dict(oktmo)
    unit_price = format_rts_unit_price(price_per_unit or "")
    if unit_price:
        place["unitPrice"] = unit_price
    days = parse_notice_delivery_days(delivery_days or "")
    if days:
        place["minDeliveryDays"], place["maxDeliveryDays"] = days
    _apply_notice_quantity(place, quantity_text)
    return place


def apply_notice_delivery(
    item: dict[str, Any],
    *,
    delivery_place: str | None = None,
    price_per_unit: str | None = None,
    delivery_days: str | None = None,
    quantity_text: str | None = None,
) -> None:
    notice_place = build_delivery_place_from_notice(
        delivery_place=delivery_place,
        price_per_unit=price_per_unit,
        delivery_days=delivery_days,
        quantity_text=quantity_text,
    )
    has_notice = bool(
        parse_oktmo_from_delivery_place(delivery_place or "")
        or format_rts_unit_price(price_per_unit or "")
        or parse_notice_delivery_days(delivery_days or "")
        or format_rts_quantity(quantity_text or "")
    )
    places = item.get("deliveryPlaces") or []
    if places and isinstance(places[0], dict):
        dp = places[0]
        if has_notice:
            if notice_place.get("oktmo"):
                dp["oktmo"] = notice_place["oktmo"]
            if notice_place.get("unitPrice") and notice_place["unitPrice"] != DEFAULT_DELIVERY["unitPrice"]:
                dp["unitPrice"] = notice_place["unitPrice"]
            if parse_notice_delivery_days(delivery_days or ""):
                dp["minDeliveryDays"] = notice_place["minDeliveryDays"]
                dp["maxDeliveryDays"] = notice_place["maxDeliveryDays"]
            if format_rts_quantity(quantity_text or ""):
                dp["minQuantity"] = notice_place["minQuantity"]
                dp["maxQuantity"] = notice_place["maxQuantity"]
                dp["available"] = notice_place["available"]
                dp["blocked"] = notice_place["blocked"]
        else:
            dp.setdefault("unitPrice", DEFAULT_DELIVERY["unitPrice"])
            dp.setdefault("oktmo", DEFAULT_DELIVERY["oktmo"])
            dp.setdefault("minDeliveryDays", DEFAULT_DELIVERY["minDeliveryDays"])
            dp.setdefault("maxDeliveryDays", DEFAULT_DELIVERY["maxDeliveryDays"])
        dp.setdefault("vatRateCode", DEFAULT_VAT_RATE_CODE)
        return
    item["deliveryPlaces"] = [notice_place if has_notice else dict(DEFAULT_DELIVERY)]


def ensure_delivery_stub(item: dict[str, Any]) -> None:
    apply_notice_delivery(item)


def apply_draft_defaults(
    item: dict[str, Any],
    *,
    country: "CountryOfOrigin | None" = None,
    find_item: dict[str, Any] | None = None,
    info_types_payload: dict[str, Any] | None = None,
) -> str | None:
    """Apply country/national-mode defaults. Returns source tag when country is set."""
    from ktru_country_origin import DEFAULT_COUNTRY, apply_country_to_draft_item, national_mode_groups_for_country

    if country is not None:
        return apply_country_to_draft_item(
            item,
            country,
            find_item=find_item,
            info_types_payload=info_types_payload,
        )
    rf = DEFAULT_COUNTRY
    if not item.get("countriesOfOrigin"):
        item["countriesOfOrigin"] = [{"code": rf.code, "name": rf.name}]
    # Without explicit country: fill empty groups via info-types if given, else legacy stub.
    if info_types_payload is not None or find_item is not None:
        return apply_country_to_draft_item(
            item,
            rf,
            find_item=find_item,
            info_types_payload=info_types_payload,
        )
    national, software = national_mode_groups_for_country(rf)
    if not item.get("nationalModeInfoGroups"):
        item["nationalModeInfoGroups"] = national
    if not item.get("softwareNationalModeInfoGroups"):
        item["softwareNationalModeInfoGroups"] = software
    return None


def create_and_save_pp_draft(
    cookie: str,
    *,
    ktru_code: str,
    ktru_name: str | None = None,
    unit_text: str | None = None,
    match_rows: list[dict[str, Any]] | None = None,
    notice_characteristics: list[dict[str, Any]] | None = None,
    delivery_place: str | None = None,
    price_per_unit: str | None = None,
    delivery_days: str | None = None,
    quantity_text: str | None = None,
    ktru_cache_dir: Path | None = None,
    allow_stub: bool = False,
    product_title: str = "",
    country_origin_path: Path | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    from ktru_country_origin import default_country_origin_path, resolve_country_for_draft
    from notice_to_rts_pp import (
        apply_notice_to_draft_item,
        build_notice_create_payload,
        ensure_item_ktru_fields,
        fill_remaining_required_from_template,
    )

    ktru_item = resolve_ktru_for_create(
        cookie,
        ktru_code,
        ktru_name=ktru_name,
        timeout=timeout,
        cache_dir=ktru_cache_dir,
        allow_stub=allow_stub,
    )
    fill_report: dict[str, Any] | None = None
    if notice_characteristics is not None:
        payload, fill_report = build_notice_create_payload(
            ktru_item,
            notice_characteristics,
            unit_from_line=unit_text,
        )
    else:
        payload = build_create_payload(ktru_item, unit_from_line=unit_text)
    draft_id = create_draft(cookie, payload, timeout=timeout)
    item = get_draft(cookie, draft_id, timeout=timeout)
    if notice_characteristics is not None:
        apply_result = apply_notice_to_draft_item(item, notice_characteristics, ktru_item)
        fill_remaining_required_from_template(item, ktru_item, apply_result)
        fill_report = apply_result.as_dict()
    elif item.get("ktruCharacteristics"):
        fill_required_characteristics(item, match_rows, ktru_search_item=ktru_item)
    ensure_item_ktru_fields(item, ktru_item)
    apply_notice_delivery(
        item,
        delivery_place=delivery_place,
        price_per_unit=price_per_unit,
        delivery_days=delivery_days,
        quantity_text=quantity_text,
    )
    country = resolve_country_for_draft(
        ktru_code=ktru_code,
        title=product_title,
        path=country_origin_path or default_country_origin_path(),
    )
    info_types: dict[str, Any] = {}
    try:
        from rts_national_mode import okpd2_code_from_ktru

        okpd2 = okpd2_code_from_ktru(ktru_code, ktru_item)
        if okpd2:
            info_types = fetch_national_mode_info_types(cookie, okpd2, timeout=timeout)
    except RtsPpApiError:
        info_types = {}
    apply_draft_defaults(item, country=country, info_types_payload=info_types or None)
    _fix_characteristic_value_ids(item, ktru_item)
    update_draft(cookie, item, timeout=timeout)
    item = get_draft(cookie, draft_id, timeout=timeout)
    protected_id = str(item.get("protectedId") or "").strip()
    if not protected_id:
        found = draft_search_by_id(cookie, draft_id, timeout=timeout)
        if found:
            protected_id = str(found.get("protectedId") or "").strip()
    edit_url = draft_edit_url(protected_id, draft_id=draft_id)
    out: dict[str, Any] = {
        "rts_draft_id": draft_id,
        "protected_id": protected_id,
        "edit_url": edit_url,
        "drafts_list_url": OFFERS_DRAFTS_URL,
        "ktru_code": ktru_code,
    }
    if notice_characteristics is not None:
        from notice_to_rts_pp import compare_notice_to_draft

        out["verify_report"] = compare_notice_to_draft(
            notice_characteristics,
            item,
            ktru_item=ktru_item,
        )
    if not protected_id:
        out["registry_warning"] = (
            "РТС не вернул protectedId для формы редактирования. "
            f"Откройте список черновиков и найдите позицию по КТРУ {ktru_code} "
            f"(id {draft_id}). Обновите cookie с запроса draft/search в браузере."
        )
    if fill_report is not None:
        out["fill_report"] = fill_report
    return out

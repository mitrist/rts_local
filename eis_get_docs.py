"""
Запросы к веб-сервису ЕИС getDocsIP (интеграция для физлиц по токену ЕРУЗ).

В проекте: папка «Схемы 16.1 итерация 3(1)» → …/GetDocsIndividualPerson/
getDocsIP-ws-api.xsd и WebServiceGetDocsIP.wsdl.

По Альбому ТФФ (вер. 16.1, п. 2.9.17) без двухсторонней TLS домен продакшена для getDocsIP — int.zakupki.gov.ru
(переходный период допускал int44.zakupki.gov.ru параллельно; WSDL из архива схем может ещё указывать int44).

POST https://int.zakupki.gov.ru/eis-integration/services/getDocsIP
Проверка канала без SOAP: GET .../getDocsIP?wsdl в браузере или подкоманда fetch-wsdl.

Домен int44-ttls-cert.zakupki.gov.ru — для методов отдачи с клиентским сертификатом ЕИС (не getDocsIP).
Для юридических лиц отдельно описан сервис getDocsLE (другая схема SOAP, свой сертификат),
он не является «правильным вместо» getDocsIP при работе через individualPerson_token.

Переопределение URL (например до устаревающего int44): переменная EIS_GET_DOCS_IP_URL или --service-url
(удобно при туннеле до ЕИС, например localhost + stunnel).

Помогает получить ответ ЕИС по реестровому номеру закупки или по региону/дате.

С учётом альбома схем (16.1):
- index/createDateTime — xs:dateTime с часовым поясом локальной машины;
- dataInfo может содержать несколько archiveUrl; обрабатываются noData/errorInfo;
- getDocsByOrgRegion: либо documentType44, либо documentType223; exactDate — с опциональным поясом (+03:00 и т.п.);
- добавлен сценарий getPreparedPart → подкоманда prepared-status (docRequestUid = id исходного запроса).

Эндпоинт XSD (при доступной сети): .../getDocsIP?xsd=getDocsIP-ws-api.xsd
Пример SOAP: https://habr.com/ru/articles/869934/
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import time
import uuid
import xml.sax.saxutils as xml_esc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import urllib3
import xmltodict
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util import ssl_ as urllib3_ssl
from urllib3.util.retry import Retry

SOAP_ACTION = "http://zakupki.gov.ru/fz44/queue/ws/get-docs-ip"
DEFAULT_GET_DOCS_SERVICE_URL = "https://int.zakupki.gov.ru/eis-integration/services/getDocsIP"
LEGACY_GET_DOCS_INT44_URL = "https://int44.zakupki.gov.ru/eis-integration/services/getDocsIP"
NS = "http://zakupki.gov.ru/fz44/get-docs-ip/ws"


def normalize_service_url(url: str) -> str:
    u = url.strip().rstrip("/")
    if u.lower().endswith("?wsdl"):
        u = u[:-5].rstrip("/")
        print("Подсказка: из URL убран суффикс ?wsdl (SOAP POST идёт на адрес без ?wsdl).")
    return u


def resolve_service_url(cli_url: str | None, *, legacy_int44: bool = False) -> str:
    if cli_url is not None and cli_url.strip():
        return normalize_service_url(cli_url)
    raw = os.environ.get("EIS_GET_DOCS_IP_URL", "").strip()
    if raw:
        return normalize_service_url(raw)
    if legacy_int44:
        return LEGACY_GET_DOCS_INT44_URL
    return DEFAULT_GET_DOCS_SERVICE_URL


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class _TLSHTTPAdapter(HTTPAdapter):
    """HTTPAdapter с фиксированным ssl_context (TLS 1.2 / SECLEVEL для проблемных сетей)."""

    def __init__(self, ssl_context: ssl.SSLContext, **kwargs: Any) -> None:
        self._ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, connections: Any, maxsize: int, block: bool = False, **kwargs: Any) -> Any:
        kwargs["ssl_context"] = self._ssl_context
        return super().init_poolmanager(connections, maxsize, block, **kwargs)


def default_retry_configuration() -> Retry:
    return Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.8,
        status_forcelist=(502, 503, 504),
        allowed_methods=("HEAD", "GET", "POST", "OPTIONS"),
        raise_on_status=False,
    )


def build_http_session(
    *,
    verify_tls: bool,
    ca_bundle: Path | None,
    ssl_legacy: bool,
    tls_max_12: bool,
) -> requests.Session:
    """
    По умолчанию — проверка цепочки сертификатов как в браузере.
    --ssl-legacy: DEFAULT:@SECLEVEL=1 (часто нужно под старые шлюзы / OpenSSL 3).
    --tls-max-12: только TLS 1.2 (если провайдер рвёт TLS 1.3).
    --no-verify: без проверки сертификата (только осознанно; есть риск MITM).
    """
    session = requests.Session()
    session.headers["User-Agent"] = DEFAULT_UA

    retries = default_retry_configuration()
    bundle_resolved = None
    if ca_bundle is not None and verify_tls:
        bundle_resolved = str(ca_bundle.expanduser().resolve())

    if not verify_tls:
        session.verify = False
        urllib3.disable_warnings(category=InsecureRequestWarning)

    use_custom_tls = ssl_legacy or tls_max_12 or (not verify_tls)

    if use_custom_tls:
        if verify_tls:
            ctx = urllib3_ssl.create_urllib3_context()
            if bundle_resolved:
                ctx.load_verify_locations(cafile=bundle_resolved)
        else:
            ctx = urllib3_ssl.create_urllib3_context(
                cert_reqs=ssl.CERT_NONE,
                check_hostname=False,
            )
        if ssl_legacy:
            try:
                ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            except ssl.SSLError:
                pass
        if tls_max_12:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        session.mount("https://", _TLSHTTPAdapter(ssl_context=ctx, max_retries=retries))
    else:
        session.mount("https://", HTTPAdapter(max_retries=retries))
        if bundle_resolved:
            session.verify = bundle_resolved

    return session


def parse_env_txt(env_path: Path) -> dict[str, str]:
    """Построчный key=value из env.txt; ключи в нижнем регистре. Пустые строки и # — пропуск."""
    result: dict[str, str] = {}
    if not env_path.is_file():
        return result
    if not os.access(env_path, os.R_OK):
        return result
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return result
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        key = k.strip().lower()
        if key:
            result[key] = v.strip()
    return result


def load_guid_eruz(env_path: Path) -> str:
    """Читает guid_eruz из env.txt (guid_eruz=UUID или одна строка UUID) или из окружения."""
    if os.environ.get("GUID_ERUZ"):
        return os.environ["GUID_ERUZ"].strip()
    if os.environ.get("EIS_GUID_ERUZ_TOKEN"):
        return os.environ["EIS_GUID_ERUZ_TOKEN"].strip()
    if not env_path.is_file():
        raise FileNotFoundError(
            f"Нет файла {env_path}. Задайте GUID_ERUZ или EIS_GUID_ERUZ_TOKEN в окружении "
            "или создайте env.txt с строкой guid_eruz=<uuid>."
        )
    kv = parse_env_txt(env_path)
    for kk in ("guid_eruz", "guid"):
        v = kv.get(kk, "").strip()
        if v:
            return v
    # Совместимость: один рядок в файле — guid_eruz=X или сырое значение токена
    norm = env_path.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    if norm and "\n" not in norm:
        if "=" in norm:
            key, _, val = norm.partition("=")
            if key.strip().lower() in ("guid_eruz", "guid", "token"):
                return val.strip()
        return norm
    raise FileNotFoundError(
        f"В {env_path} не найдена строка guid_eruz=<uuid> (или guid=<uuid>). "
        "Добавьте её отдельной строкой; для Telegram используйте telegram_bot= и telegram_chat_id=."
    )


def _xe(s: str) -> str:
    """Экранирование текста в элементы SOAP (защита от & < > в номерах закупок)."""
    return xml_esc.escape(s, entities={'"': "&quot;", "'": "&apos;"})


def package_index_datetime() -> str:
    """xs:dateTime для index/createDateTime — с часовым поясом (см. getDocsIP-ws-api.xsd)."""
    return datetime.now().astimezone().replace(microsecond=0).isoformat(timespec="seconds")


def _index_block(request_id: str, create_dt: str, mode: str) -> str:
    return (
        f"<index><id>{request_id}</id>"
        f"<createDateTime>{create_dt}</createDateTime>"
        f"<mode>{mode}</mode></index>"
    )


def envelope(token: str, body_inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:ws="{NS}">'
        "<soapenv:Header>"
        f"<individualPerson_token>{_xe(token)}</individualPerson_token>"
        "</soapenv:Header>"
        "<soapenv:Body>"
        f"{body_inner}"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


def build_reestr_request(
    token: str,
    reestr_number: str,
    *,
    subsystem_type: str,
    request_id: str | None,
    mode: str,
) -> str:
    rid = request_id or str(uuid.uuid4())
    now = package_index_datetime()
    inner = (
        "<ws:getDocsByReestrNumberRequest>"
        f"{_index_block(rid, now, mode)}"
        "<selectionParams>"
        f"<subsystemType>{_xe(subsystem_type)}</subsystemType>"
        f"<reestrNumber>{_xe(reestr_number)}</reestrNumber>"
        "</selectionParams>"
        "</ws:getDocsByReestrNumberRequest>"
    )
    return envelope(token, inner)


def build_org_region_request(
    token: str,
    *,
    org_region: str,
    subsystem_type: str,
    document_type_44: str | None,
    document_type_223: str | None,
    exact_date: str,
    request_id: str | None,
    mode: str,
) -> str:
    rid = request_id or str(uuid.uuid4())
    now = package_index_datetime()
    if document_type_223:
        doc_el = f"<documentType223>{_xe(document_type_223)}</documentType223>"
    else:
        d44 = document_type_44 or "epNotificationEF2020"
        doc_el = f"<documentType44>{_xe(d44)}</documentType44>"
    inner = (
        "<ws:getDocsByOrgRegionRequest>"
        f"{_index_block(rid, now, mode)}"
        "<selectionParams>"
        f"<orgRegion>{_xe(org_region)}</orgRegion>"
        f"<subsystemType>{_xe(subsystem_type)}</subsystemType>"
        f"{doc_el}"
        "<periodInfo>"
        f"<exactDate>{exact_date.strip()}</exactDate>"
        "</periodInfo>"
        "</selectionParams>"
        "</ws:getDocsByOrgRegionRequest>"
    )
    return envelope(token, inner)


def build_prepared_part_request(
    token: str,
    doc_request_uid: str,
    *,
    request_id: str | None,
    mode: str,
) -> str:
    """getPreparedPart — статус больших выгрузок по docRequestUid (= index/id исходного запроса getDocs*)."""
    rid = request_id or str(uuid.uuid4())
    now = package_index_datetime()
    inner = (
        "<ws:getPreparedPartRequest>"
        f"{_index_block(rid, now, mode)}"
        f"<docRequestUid>{doc_request_uid.strip()}</docRequestUid>"
        "</ws:getPreparedPartRequest>"
    )
    return envelope(token, inner)


def build_get_nsi_request(
    token: str,
    *,
    nsi_kind: str,
    nsi_code_44: str | None,
    nsi_code_223: str | None,
    request_id: str | None,
    mode: str,
) -> str:
    """Запрос getNsi (справочник из ХД) — см. getDocsIP-ws-api.xsd; для смок-теста удобен nsiAllList."""
    rid = request_id or str(uuid.uuid4())
    now = package_index_datetime()
    if nsi_code_223:
        code_el = f"<nsiCode223>{_xe(nsi_code_223.strip())}</nsiCode223>"
    else:
        c44 = (nsi_code_44 or "nsiAllList").strip()
        code_el = f"<nsiCode44>{_xe(c44)}</nsiCode44>"
    inner = (
        "<ws:getNsiRequest>"
        f"{_index_block(rid, now, mode)}"
        "<selectionParams>"
        f"{code_el}"
        f"<nsiKind>{_xe(nsi_kind.strip())}</nsiKind>"
        "</selectionParams>"
        "</ws:getNsiRequest>"
    )
    return envelope(token, inner)


def parse_soap(xml_bytes: bytes) -> dict[str, Any]:
    return xmltodict.parse(xml_bytes, process_namespaces=False)


def _unwrap_envelope(parsed: dict[str, Any]) -> dict[str, Any]:
    for key in ("soap:Envelope", "soapenv:Envelope"):
        if key in parsed:
            return parsed[key]
    raise ValueError("Ответ не похож на SOAP Envelope")


def _soap_body(parsed: dict[str, Any]) -> dict[str, Any]:
    env = _unwrap_envelope(parsed)
    for key in ("soap:Body", "soapenv:Body"):
        if key in env:
            return env[key]
    raise ValueError("В ответе нет SOAP Body")


def find_fault(body: dict[str, Any]) -> str | None:
    for key, val in body.items():
        if key.endswith("Fault") and isinstance(val, dict):
            reason = val.get("faultstring") or val.get("Reason") or val.get("faultcode")
            return str(reason) if reason else json.dumps(val, ensure_ascii=False)
    return None


def find_response_payload(body: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    for key, val in body.items():
        if key.endswith("Response") and isinstance(val, dict):
            return key, val
    return None, None


def extract_data_info(payload: dict[str, Any]) -> dict[str, Any] | None:
    di = payload.get("dataInfo")
    if isinstance(di, dict):
        return di
    return None


def post_get_docs(
    session: requests.Session, service_url: str, soap_xml: str, timeout: float
) -> requests.Response:
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": SOAP_ACTION,
        "Accept": "text/xml, application/xml;q=0.9, */*;q=0.8",
    }
    return session.post(service_url, data=soap_xml.encode("utf-8"), headers=headers, timeout=timeout)


def download_archive_maybe(
    session: requests.Session, archive_url: str, token: str, timeout: float
) -> tuple[bytes | None, str | None]:
    """
    Скачивание compound/archiveUrl без исключений на типичных ошибках канала или 404 истёкшей ссылки.
    Возвращает (байты архива или None, текст ошибки или None).
    """
    headers = {"individualPerson_token": token, "Accept": "*/*"}
    u_preview = archive_url[:180] + ("…" if len(archive_url) > 180 else "")
    try:
        r = session.get(archive_url, headers=headers, timeout=timeout)
        r.raise_for_status()
        if not r.content:
            return None, f"Пустое тело ответа archiveUrl: {u_preview}"
        return r.content, None
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", "?") if e.response is not None else "?"
        return None, f"archiveUrl HTTP {code}: {u_preview}"
    except requests.Timeout as e:
        return None, f"archiveUrl timeout ({timeout} с): {u_preview}"
    except requests.RequestException as e:
        return None, f"archiveUrl: {type(e).__name__}: {e} — {u_preview}"


def download_archive(session: requests.Session, archive_url: str, token: str, timeout: float) -> bytes:
    data, err = download_archive_maybe(session, archive_url, token, timeout)
    if err or data is None:
        raise RuntimeError(err or "Не удалось скачать архив ЕИС")
    return data


def _as_xml_list(val: Any) -> list[Any]:
    if val is None:
        return []
    return val if isinstance(val, list) else [val]


def extract_archive_urls_from_data_info(data_info: dict[str, Any] | None) -> list[str]:
    """Из dataInfo архив может приходить одной ссылкой или списком (схема: maxOccurs 10 / 1000)."""
    if not isinstance(data_info, dict):
        return []
    raw = data_info.get("archiveUrl")
    out: list[str] = []
    for item in _as_xml_list(raw):
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict) and item.get("#text"):
            out.append(str(item["#text"]).strip())
    return out



def print_summary(
    data_info: dict[str, Any] | None,
    *,
    rts_noticed_at: datetime | None,
    eis_wall_time: datetime,
) -> None:
    if data_info is None:
        print("dataInfo: отсутствует в ответе")
        return
    urls = extract_archive_urls_from_data_info(data_info)
    print("Разбор dataInfo по схеме getDocsIP-ws-api.xsd:")
    if data_info.get("noData"):
        print("  noData: true (документы отсутствуют)")
    err = data_info.get("errorInfo")
    if isinstance(err, dict):
        print(f"  errorInfo: код {err.get('code')} — {err.get('message')}")
    if urls:
        print(f"  archiveUrl: {len(urls)} ссыл(ок) на архив(ы)")
        for i, u in enumerate(urls, start=1):
            print(f"    [{i}] {u[:120]}{'…' if len(u) > 120 else ''}")
    if not urls and not data_info.get("noData") and not isinstance(data_info.get("errorInfo"), dict):
        print(f"  (сырой объект: {json.dumps(data_info, ensure_ascii=False)[:900]})")

    # Остальные поля без дубля archiveUrl-длинные URL
    skip_detail = frozenset({"archiveUrl", "noData", "errorInfo"})
    for k in sorted(x for x in data_info.keys() if x not in skip_detail):
        print(f"  {k}: {data_info[k]}")

    if rts_noticed_at is not None:
        delta = eis_wall_time - rts_noticed_at
        print(
            f"\nСравнение с --rts-noticed-at: интервал до момента ответа клиенту (~ЕИС) "
            f"≈ {delta.total_seconds():.1f} с ({delta})."
        )


def save_downloaded_archives(
    session: requests.Session,
    urls: list[str],
    token: str,
    timeout: float,
    base_path: Path | None,
) -> None:
    if not urls:
        return
    base = base_path if base_path is not None else Path("eis_archive.zip")
    suffix = base.suffix if base.suffix else ".zip"
    if len(urls) == 1:
        dests = [base.with_suffix(suffix) if base.suffix else base]
    else:
        stem = base.stem
        parent = base.parent
        dests = [parent / f"{stem}_{n}{suffix}" for n in range(1, len(urls) + 1)]

    for u, dest in zip(urls, dests):
        zp = download_archive(session, u, token, timeout)
        dest.write_bytes(zp)
        print(f"Архив сохранён: {dest.resolve()} ({len(zp)} байт)")

def run_once(
    session: requests.Session,
    service_url: str,
    soap_xml: str,
    token: str,
    *,
    timeout: float,
    download: bool,
    out_zip: Path | None,
    rts_noticed_at: datetime | None,
    print_raw_on_error: bool,
) -> tuple[int, dict[str, Any] | None]:
    wall_local = datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
    try:
        resp = post_get_docs(session, service_url, soap_xml, timeout)
    except requests.exceptions.SSLError as e:
        print("TLS/SSL ошибка при обращении к ЕИС:", e)
        print(
            "\nЧто попробовать (по одной опции или вместе):\n"
            "  --tls-max-12              ограничить TLS 1.2\n"
            "  --ssl-legacy              шифры DEFAULT:@SECLEVEL=1\n"
            "  --ssl-legacy --tls-max-12 совместить оба режима\n"
            "  --no-verify               без проверки сертификата (MITM между вами и ЕИС останется незаметным)\n"
            "Проверьте также: переменную HTTPS_PROXY, отключите «HTTPS-скан» в антивирусе/корп. фаерволе,\n"
            "или проверьте в браузере WSDL этого же хоста: "
            + service_url.split("?", 1)[0]
            + "?wsdl (если не открывается — блок канала или неверная база в --service-url)."
        )
        return 10, None
    except requests.exceptions.ConnectionError as e:
        print("Сетевая ошибка при обращении к ЕИС:", e)
        print(
            "Частые причины: сброс соединения (Win 10054), блокировка хоста (по умолчанию int.zakupki.gov.ru), "
            "прокси, антивирусное сканирование HTTPS. Проверьте GET того же узла …/getDocsIP?wsdl в браузере."
        )
        print("При необходимости: повторите с --tls-max-12 или --ssl-legacy --tls-max-12.")
        return 12, None
    print(f"HTTP {resp.status_code}")

    try:
        parsed = parse_soap(resp.content)
    except Exception as e:
        print("Не удалось разобрать XML ответа:", e)
        if print_raw_on_error:
            print(resp.text[:4000])
        return 2, None

    body = _soap_body(parsed)
    fault = find_fault(body)
    if fault:
        print("SOAP Fault:", fault)
        if print_raw_on_error:
            print(resp.text[:4000])
        return 3, None

    _, payload = find_response_payload(body)
    if not payload:
        frag = json.dumps(body, ensure_ascii=False, indent=2)[:4000]
        print("Неожиданное тело ответа:", frag)
        return 4, None

    data_info = extract_data_info(payload)
    print_summary(data_info, rts_noticed_at=rts_noticed_at, eis_wall_time=wall_local)

    urls = extract_archive_urls_from_data_info(data_info)
    if download and urls:
        try:
            save_downloaded_archives(
                session, urls, token, max(timeout * 4, 120.0), out_zip
            )
        except requests.exceptions.SSLError as e:
            print("TLS/SSL при загрузке архива:", e)
            return 11, data_info
    elif download and data_info:
        print(
            "Скачивание запрошено, но archiveUrl нет "
            "(возможно noData/errorInfo или подготовка ещё идёт — см. команда prepared-status)."
        )

    return 0, data_info


def _collect_urls_from_prepared_data_info(data_info: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("preparedPartInfo", "preparedNotDownloadPartInfo"):
        for row in _as_xml_list(data_info.get(key)):
            if isinstance(row, dict):
                u = row.get("url")
                if isinstance(u, str) and u.strip():
                    urls.append(u.strip())
    return urls


def collect_prepared_archive_urls(data_info: dict[str, Any] | None) -> list[str]:
    """Публичная обёртка: url из preparedPartInfo / preparedNotDownloadPartInfo (getPreparedPart)."""
    if not isinstance(data_info, dict):
        return []
    return _collect_urls_from_prepared_data_info(data_info)


def parse_soap_response_body(resp: requests.Response, *, print_raw_on_error: bool) -> tuple[int, dict[str, Any] | None]:
    """
    Разбор SOAP Body без печати метаданных. Коды возврата как у run_once: 0 ок, 2/3/4 ошибки.
    При успехе — внутренний dict ответа (*Response), например getDocsByOrgRegionResponse.
    """
    try:
        parsed = parse_soap(resp.content)
    except Exception as e:
        print("Не удалось разобрать XML ответа:", e)
        if print_raw_on_error:
            print(resp.text[:4000])
        return 2, None
    body = _soap_body(parsed)
    fault = find_fault(body)
    if fault:
        print("SOAP Fault:", fault)
        if print_raw_on_error:
            print(resp.text[:4000])
        return 3, None
    _, payload = find_response_payload(body)
    if not payload:
        frag = json.dumps(body, ensure_ascii=False, indent=2)[:4000]
        print("Неожидательное тело ответа:", frag)
        return 4, None
    return 0, payload


def extract_index_request_id(payload: dict[str, Any]) -> str | None:
    """Идентификатор пакета из index/id (тот же GUID, что передаётся в getPreparedPart как docRequestUid)."""
    idx = payload.get("index")
    if isinstance(idx, dict):
        raw = idx.get("id")
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None
    return None


def run_prepared_status(
    session: requests.Session,
    service_url: str,
    token: str,
    doc_request_uid: str,
    *,
    mode: str,
    timeout: float,
    download: bool,
    out_zip: Path | None,
    print_raw_on_error: bool,
) -> int:
    soap_xml = build_prepared_part_request(
        token, doc_request_uid, request_id=None, mode=mode
    )
    try:
        resp = post_get_docs(session, service_url, soap_xml, timeout)
    except requests.exceptions.SSLError as e:
        print("TLS/SSL ошибка:", e)
        return 10
    print(f"HTTP {resp.status_code}")
    try:
        parsed = parse_soap(resp.content)
    except Exception as e:
        print("Не удалось разобрать XML ответа:", e)
        if print_raw_on_error:
            print(resp.text[:4000])
        return 2
    body = _soap_body(parsed)
    fault = find_fault(body)
    if fault:
        print("SOAP Fault:", fault)
        return 3
    _, payload = find_response_payload(body)
    if not payload:
        print("Неожиданное тело:", json.dumps(body, ensure_ascii=False)[:2000])
        return 4

    print(f"docRequestUid из ответа: {payload.get('docRequestUid')}")
    err = payload.get("errorInfo")
    if isinstance(err, dict):
        print(f"errorInfo: {err.get('code')} — {err.get('message')}")
        return 5

    di = payload.get("dataInfo")
    if not isinstance(di, dict):
        print("В ответе нет dataInfo.")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:3500])
        return 6

    print(f"isPreparationCompleted: {di.get('isPreparationCompleted')}")
    urls = _collect_urls_from_prepared_data_info(di)
    if urls:
        print(f"Подготовлено ссылок для скачивания (url): {len(urls)}")
    if download and urls:
        try:
            save_downloaded_archives(
                session, urls, token, max(timeout * 4, 120.0), out_zip
            )
        except requests.exceptions.SSLError as e:
            print("TLS/SSL при загрузке:", e)
            return 11
    elif download and not urls:
        print("Скачивание запрошено, но url в блоках подготовки пока нет.")

    return 0


def run_fetch_wsdl(session: requests.Session, service_url: str, timeout: float) -> int:
    """GET WSDL — без individualPerson_token; проверка TLS и доступности узла (Альбом ТФФ п. 2.9.17, int.zakupki)."""
    wsdl_url = normalize_service_url(service_url) + "?wsdl"
    print(f"GET {wsdl_url}")
    try:
        resp = session.get(
            wsdl_url,
            timeout=timeout,
            headers={"Accept": "text/xml, application/xml, */*"},
        )
    except requests.exceptions.SSLError as e:
        print("TLS/SSL при загрузке WSDL:", e)
        return 10
    except requests.exceptions.ConnectionError as e:
        print("Сетевая ошибка при загрузке WSDL:", e)
        return 12
    print(f"HTTP {resp.status_code}")
    print(f"Content-Type: {resp.headers.get('Content-Type', '')}")
    snippet = (resp.text or "")[:300].replace("\r", "").replace("\n", " ")
    if snippet.strip():
        print(f"Response preview: {snippet}{'...' if len(resp.text) > 300 else ''}")
    if resp.status_code != 200:
        return 1
    if "definitions" not in resp.text and "wsdl" not in resp.text.lower():
        print("Предупреждение: ответ не похож на WSDL (проверьте URL и редиректы).")
        return 2
    return 0


def _print_get_nsi_data_info(data_info: dict[str, Any] | None) -> bool:
    """Печатает dataInfo для getNsi; возвращает False, если есть errorInfo (ответ ЕИС — отказ по бизнес-правилам)."""
    if not isinstance(data_info, dict):
        print("dataInfo: нет или не объект")
        return False
    if data_info.get("noData"):
        print("noData: true")
    err_raw = data_info.get("errorInfo")
    if isinstance(err_raw, dict):
        code = err_raw.get("code")
        msg = err_raw.get("message")
        print(f"errorInfo: {code} — {msg}")
        if str(code) == "5" or (isinstance(msg, str) and "Токены для сервисов отдачи" in msg):
            print(
                "Подсказка: ответ ЕИС не про «просроченный» формат GUID, а про учёт в подсистеме отдачи.\n"
                "  Токен может быть действительным в ЕРУЗ для других целей, но пока не включён для этих "
                "SOAP-сервисов (или не тот контур).\n"
                "  Что проверить: раздел ЛК с интеграцией/токеном именно для «отдачи» документов; "
                "что подставили тот же UUID, который показывает ЛК после выпуска; при тестовом токере — "
                "запрос с ключом `--mode TEST`; подождите несколько часов после первой регистрации токена.\n"
                "  Если всё уже сделано — уточните в поддержке ЕИС по тексту ошибки и коду 5 для getDocsIP."
            )
    archives = data_info.get("nsiArchiveInfo")
    rows = _as_xml_list(archives)
    if rows:
        print(f"nsiArchiveInfo: {len(rows)} записей (архив справочника)")
        for i, row in enumerate(rows[:5], start=1):
            if isinstance(row, dict):
                u = row.get("archiveUrl", "")
                n = row.get("archiveName", "")
                print(f"  [{i}] {n}: {str(u)[:140]}{'…' if len(str(u)) > 140 else ''}")
        if len(rows) > 5:
            print(f"  … и ещё {len(rows) - 5}")
        return not isinstance(err_raw, dict)
    if not data_info.get("noData") and not isinstance(err_raw, dict):
        print(f"Неожиданное dataInfo: {json.dumps(data_info, ensure_ascii=False)[:800]}")
        return False
    return not isinstance(err_raw, dict)


def run_probe_nsi(
    session: requests.Session,
    service_url: str,
    token: str,
    *,
    nsi_kind: str,
    nsi_code_44: str | None,
    nsi_code_223: str | None,
    mode: str,
    timeout: float,
    print_raw_on_error: bool,
) -> int:
    soap_xml = build_get_nsi_request(
        token,
        nsi_kind=nsi_kind,
        nsi_code_44=nsi_code_44,
        nsi_code_223=nsi_code_223,
        request_id=None,
        mode=mode,
    )
    print("SOAP getNsi -> перечень справочников (nsiAllList) или свой --nsi-code44 / --nsi-code223")
    try:
        resp = post_get_docs(session, service_url, soap_xml, timeout)
    except requests.exceptions.SSLError as e:
        print("TLS/SSL:", e)
        return 10
    except requests.exceptions.ConnectionError as e:
        print("Сетевая ошибка:", e)
        return 12
    print(f"HTTP {resp.status_code}")
    try:
        parsed = parse_soap(resp.content)
    except Exception as e:
        print("Парсинг XML:", e)
        if print_raw_on_error:
            print(resp.text[:4000])
        return 2
    body = _soap_body(parsed)
    fault = find_fault(body)
    if fault:
        print("SOAP Fault:", fault)
        if print_raw_on_error:
            print(resp.text[:4000])
        return 3
    _, payload = find_response_payload(body)
    if not payload:
        print("Нет ответа *Response:", json.dumps(body, ensure_ascii=False)[:2000])
        return 4
    di = payload.get("dataInfo") if isinstance(payload, dict) else None
    if _print_get_nsi_data_info(di):
        return 0
    return 5


def parse_rts_time(s: str) -> datetime:
    """Парсит ISO-дату; допускает формат без таймзоны (локальное время)."""
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo:
            return dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"Некорректная дата/время: {s!r}") from e


def main() -> int:
    p = argparse.ArgumentParser(
        description="SOAP getDocsIP client (zakupki EIS): reestr/region/getNsi probes.",
        epilog=(
            "Place global flags BEFORE the subcommand, e.g. "
            "python eis_get_docs.py --timeout 60 fetch-wsdl"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parent / "env.txt",
        help="Файл с guid_eruz=... (по умолчанию env.txt рядом со скриптом)",
    )
    p.add_argument("--timeout", type=float, default=60.0, help="Таймаут HTTP, сек")
    p.add_argument("--subsystem-type", default="PRIZ", help="Обычно PRIZ для подсистемы извещений (см. xsd)")
    p.add_argument("--mode", default="PROD", choices=("PROD", "TEST"))
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="Не проверять серверный сертификат TLS (только если доверяете каналу; иначе MITM)",
    )
    p.add_argument(
        "--ssl-legacy",
        action="store_true",
        help="Разрешить слабее набор шифров (DEFAULT:@SECLEVEL=1); помогает OpenSSL 3 при старых шлюзах",
    )
    p.add_argument(
        "--tls-max-12",
        action="store_true",
        dest="tls_max_12",
        help="Ограничить соединение TLS 1.2 (если handshake на TLS 1.3 обрывается)",
    )
    p.add_argument("--cacert", type=Path, default=None, help="Свой bundle CA (.pem)")
    p.add_argument(
        "--legacy-int44",
        action="store_true",
        help="SOAP на https://int44.zakupki.gov.ru/.../getDocsIP (старый адрес из WSDL; узел может быть отключён)",
    )
    p.add_argument(
        "--service-url",
        default=None,
        metavar="URL",
        help=(
            "URL getDocsIP без ?wsdl (переменная EIS_GET_DOCS_IP_URL; "
            "по умолчанию https://int.zakupki.gov.ru/eis-integration/services/getDocsIP; см. --legacy-int44)"
        ),
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "fetch-wsdl",
        help="Проверка канала без токена ЕРУЗ: GET …/getDocsIP?wsdl",
    )

    pns = sub.add_parser(
        "probe-nsi",
        help="Лёгкий SOAP getNsi (нужен guid ЕРУЗ): по умолчанию справочник nsiAllList",
    )
    pns.add_argument(
        "--nsi-kind",
        default="inc",
        choices=("all", "inc"),
        help="Вид выгрузки (inc обычно меньше по объёму, чем all)",
    )
    pns.add_argument(
        "--nsi-code44",
        default="nsiAllList",
        metavar="CODE",
        help="Код справочника 44-ФЗ (по умолчанию nsiAllList — перечень справочников ЕИС)",
    )
    pns.add_argument(
        "--nsi-code223",
        default=None,
        metavar="CODE",
        help="Если задан — запрашивается справочник 223-ФЗ вместо nsiCode44",
    )
    pns.add_argument("--print-raw-on-error", action="store_true")

    pr = sub.add_parser("by-reestr", help="Архив документов по реестровому номеру (44-ФЗ)")
    pr.add_argument("reestr_number", help="Номер закупки, например 0373200009425000001")
    pr.add_argument(
        "--poll",
        type=float,
        metavar="SEC",
        help="Интервал повтора, сек — пока archiveUrl не появится или не исчерпан --max-polls",
    )
    pr.add_argument("--max-polls", type=int, default=30, help="Максимум итераций при --poll")

    rg = sub.add_parser("by-region", help="По региону заказчика и дате (тип документа)")
    rg.add_argument("org_region", help="Код региона, напр. 72")
    rg.add_argument("exact_date", help="exactDate в periodInfo: YYYY-MM-DD или с поясом, напр. 2023-02-10+03:00")
    rg.add_argument(
        "--document-type44",
        default="epNotificationEF2020",
        help=(
            "Элемент documentType44 (код из справочника nsiDocumentTypes через getNsi; "
            "для ЭЗТ ч.12 ст.93 часто epNotificationEZT2020 — проверьте по probe-nsi --nsi-code44 nsiDocumentTypes)"
        ),
    )
    rg.add_argument(
        "--document-type223",
        default=None,
        metavar="CODE",
        help="Элемент documentType223 для 223-ФЗ вместо documentType44",
    )

    pst = sub.add_parser(
        "prepared-status",
        help=(
            "getPreparedPart по docRequestUid (= index/id исходного getDocs*-запроса; "
            "крупные архивы — асинхронная подготовка)"
        ),
    )
    pst.add_argument(
        "doc_request_uid",
        help="UUID из вашего первого запроса (совпадает с <index><id>...</id>)",
    )
    pst.add_argument("--download", action="store_true")
    pst.add_argument("--out-zip", type=Path, help="Базовое имя для одного или нескольких архивов")
    pst.add_argument(
        "--print-raw-on-error",
        action="store_true",
        help="Фрагмент ответа при ошибке SOAP/парсинга",
    )

    for sp in (pr, rg):
        sp.add_argument("--download", action="store_true", help="Скачать архив по archiveUrl")
        sp.add_argument("--out-zip", type=Path, help="Путь к zip (умолчанию eis_archive.zip)")
        sp.add_argument(
            "--rts-noticed-at",
            type=parse_rts_time,
            help='Когда заметили публикацию на РТС, ISO: "2026-04-30T14:05:00"',
        )
        sp.add_argument(
            "--print-raw-on-error",
            action="store_true",
            help="Фрагмент ответа при ошибке SOAP/парсинга",
        )

    args = p.parse_args()
    session = build_http_session(
        verify_tls=not args.no_verify,
        ca_bundle=args.cacert,
        ssl_legacy=args.ssl_legacy,
        tls_max_12=args.tls_max_12,
    )
    service_url = resolve_service_url(args.service_url, legacy_int44=args.legacy_int44)
    print(f"getDocsIP -> {service_url}")

    if args.cmd == "fetch-wsdl":
        return run_fetch_wsdl(session, service_url, args.timeout)

    try:
        token = load_guid_eruz(args.env_file)
    except FileNotFoundError as e:
        print(str(e))
        return 1

    if args.cmd == "probe-nsi":
        return run_probe_nsi(
            session,
            service_url,
            token,
            nsi_kind=args.nsi_kind,
            nsi_code_44=None if args.nsi_code223 else args.nsi_code44,
            nsi_code_223=args.nsi_code223,
            mode=args.mode,
            timeout=args.timeout,
            print_raw_on_error=args.print_raw_on_error,
        )

    if args.cmd == "prepared-status":
        return run_prepared_status(
            session,
            service_url,
            token,
            args.doc_request_uid,
            mode=args.mode,
            timeout=args.timeout,
            download=args.download,
            out_zip=args.out_zip,
            print_raw_on_error=args.print_raw_on_error,
        )

    if args.cmd == "by-reestr":
        soap_xml = build_reestr_request(
            token,
            args.reestr_number,
            subsystem_type=args.subsystem_type,
            request_id=None,
            mode=args.mode,
        )
    elif args.cmd == "by-region":
        soap_xml = build_org_region_request(
            token,
            org_region=args.org_region,
            subsystem_type=args.subsystem_type,
            document_type_44=args.document_type44,
            document_type_223=args.document_type223,
            exact_date=args.exact_date,
            request_id=None,
            mode=args.mode,
        )
    else:
        print(f"Неизвестная подкоманда: {args.cmd}")
        return 9

    if args.cmd == "by-reestr" and args.poll is not None:
        last_code = 0
        for i in range(args.max_polls):
            print(f"\n--- попытка {i + 1}/{args.max_polls} ---")
            soap_xml = build_reestr_request(
                token,
                args.reestr_number,
                subsystem_type=args.subsystem_type,
                request_id=str(uuid.uuid4()),
                mode=args.mode,
            )
            code, data_info = run_once(
                session,
                service_url,
                soap_xml,
                token,
                timeout=args.timeout,
                download=False,
                out_zip=args.out_zip,
                rts_noticed_at=args.rts_noticed_at,
                print_raw_on_error=args.print_raw_on_error,
            )
            last_code = code
            archive_urls = extract_archive_urls_from_data_info(data_info)
            if archive_urls:
                if args.download:
                    save_downloaded_archives(
                        session,
                        archive_urls,
                        token,
                        max(args.timeout * 4, 120.0),
                        args.out_zip,
                    )
                return code
            time.sleep(max(args.poll, 0.1))
        print(
            "Не дождались archiveUrl. Увеличьте --max-polls или --poll; "
            "для крупных выгрузок проверьте: prepared-status с "
            "<index/id> первого запроса по схеме getPreparedPart."
        )
        return last_code

    code, _ = run_once(
        session,
        service_url,
        soap_xml,
        token,
        timeout=args.timeout,
        download=args.download,
        out_zip=args.out_zip,
        rts_noticed_at=args.rts_noticed_at,
        print_raw_on_error=args.print_raw_on_error,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())

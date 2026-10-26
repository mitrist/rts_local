#!/usr/bin/env python3
"""Скачать все страницы контрактов Мастер-Зак (order567) и собрать таблицу.

Эндпоинт: POST https://master-zak.ru/nmc-api/api/order567/data/contracts
Тело запроса — как в deploy/zak.txt (фильтры, КТРУ, top). Поле page скрипт
подставляет сам: на странице до 50 контрактов, в ответе total (в примере 10000).

Без сессии API отвечает 401 «Требуется аутентификация». Скопируйте из DevTools
заголовок того же POST, который уже открывается в браузере:

  Authorization: Bearer ...
  и/или Cookie: ...

Пример:

  python tools/fetch_masterzak_contracts.py ^
    --payload deploy/zak.txt ^
    --authorization-file deploy/masterzak_authorization.txt ^
    --out exports/masterzak_contracts.csv

Каждый ответ сохраняется отдельно (page_0001.json, ...), таблица — CSV
с разделителем «;» и BOM, чтобы Excel открыл кириллицу.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://master-zak.ru/nmc-api/api/order567/data/contracts"
RETRY_STATUS = {429, 500, 502, 503, 504}
MSK = timezone(timedelta(hours=3))
SIGN_BUCKET_MAX = 300

COLUMNS: list[tuple[str, str]] = [
    ("page", "Страница"),
    ("number", "Номер контракта"),
    ("contract_id", "ID контракта"),
    ("uid", "UID позиции"),
    ("sign_date", "Дата заключения"),
    ("publish_date", "Дата публикации"),
    ("execution_end_date", "Дата окончания исполнения"),
    ("status", "Статус"),
    ("procedure", "Способ закупки"),
    ("law", "Закон"),
    ("nds", "НДС"),
    ("contract_max_price", "НМЦК контракта"),
    ("unit_price", "Цена единицы"),
    ("quantity", "Количество"),
    ("unit", "Ед. изм."),
    ("price_index", "Индекс цены"),
    ("tru_name", "Наименование ТРУ"),
    ("okpd2", "ОКПД2"),
    ("ktru", "КТРУ"),
    ("full_match", "Полное совпадение"),
    ("organizer_inn", "ИНН заказчика"),
    ("organizer_kpp", "КПП заказчика"),
    ("organizer_name", "Заказчик"),
    ("supplier_inn", "ИНН поставщика"),
    ("supplier_kpp", "КПП поставщика"),
    ("supplier_name", "Поставщик"),
    ("region", "Регион"),
    ("has_penalty", "Неустойка"),
    ("contract_link", "Ссылка"),
    ("countries", "Страны"),
    ("characteristics", "Характеристики"),
]


def fmt_num(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text.replace(".", ",")
    return str(value).strip()


def fmt_bool(value: Any) -> str:
    if value is True:
        return "Да"
    if value is False:
        return "Нет"
    return ""


def _join(items: list[Any], getter) -> str:
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(getter(item) or "").strip()
        if text:
            parts.append(text)
    return " | ".join(parts)


def contract_row(contract: dict[str, Any], page: int) -> dict[str, str]:
    tru = contract.get("tru") if isinstance(contract.get("tru"), dict) else {}
    suppliers = contract.get("suppliers") if isinstance(contract.get("suppliers"), list) else []
    characteristics = tru.get("characteristics") if isinstance(tru.get("characteristics"), list) else []
    countries = tru.get("countries") if isinstance(tru.get("countries"), list) else []
    def _characteristic(item: dict[str, Any]) -> str:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            return f"{name}: {value}"
        return name or value

    char_text = _join(characteristics, _characteristic)
    return {
        "page": str(page),
        "number": str(contract.get("number") or ""),
        "contract_id": fmt_num(contract.get("contractId")),
        "uid": str(contract.get("uid") or ""),
        "sign_date": str(contract.get("contractSignDate") or ""),
        "publish_date": str(contract.get("contractPublishDate") or ""),
        "execution_end_date": str(contract.get("executionEndDate") or ""),
        "status": str(contract.get("contractStatus") or ""),
        "procedure": str(contract.get("pws") or ""),
        "law": fmt_num(contract.get("law")),
        "nds": fmt_num(contract.get("nds")),
        "contract_max_price": fmt_num(contract.get("maxPrice")),
        "unit_price": fmt_num(tru.get("price")),
        "quantity": fmt_num(tru.get("quantityValue")),
        "unit": str(tru.get("nationalCode") or ""),
        "price_index": fmt_num(tru.get("priceIndex")),
        "tru_name": str(tru.get("truName") or ""),
        "okpd2": str(tru.get("okpD2Code") or ""),
        "ktru": str(tru.get("ktruCode") or ""),
        "full_match": fmt_bool(tru.get("isFullMatch")),
        "organizer_inn": str(contract.get("organizerINN") or ""),
        "organizer_kpp": str(contract.get("organizerKPP") or ""),
        "organizer_name": str(contract.get("organizerName") or ""),
        "supplier_inn": _join(suppliers, lambda item: item.get("inn")),
        "supplier_kpp": _join(suppliers, lambda item: item.get("kpp")),
        "supplier_name": _join(
            suppliers,
            lambda item: item.get("shortName") or item.get("name"),
        ),
        "region": str(contract.get("regionName") or contract.get("deliveryRegions") or ""),
        "has_penalty": fmt_bool(contract.get("hasPenalty")),
        "contract_link": str(contract.get("contractLink") or ""),
        "countries": _join(
            countries,
            lambda item: item.get("countryName") or item.get("countryCode"),
        ),
        "characteristics": char_text,
    }


def rows_from_response(payload: dict[str, Any], page: int) -> list[dict[str, str]]:
    contracts = payload.get("contracts")
    if not isinstance(contracts, list):
        return []
    return [contract_row(item, page) for item in contracts if isinstance(item, dict)]


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[key for key, _title in COLUMNS],
            delimiter=";",
            extrasaction="ignore",
        )
        writer.writerow({key: title for key, title in COLUMNS})
        writer.writerows(rows)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} должен быть JSON-объектом")
    return data


def header_from_dump(text: str, header_name: str) -> str:
    """DevTools copy: имя заголовка на одной строке, значение на следующей."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    target = header_name.lower()
    for index, line in enumerate(lines[:-1]):
        name = line[:-1] if line.endswith(":") else line
        if name.lower() == target and ":" not in line:
            return lines[index + 1].strip()
    return ""


def read_secret(value: str | None, file_path: str | None, header_name: str) -> str:
    if file_path:
        text = Path(file_path).read_text(encoding="utf-8")
        dumped = header_from_dump(text, header_name)
        if dumped:
            return dumped
        text = text.strip()
        prefix = header_name.lower() + ":"
        if text.lower().startswith(prefix):
            text = text.split(":", 1)[1].strip()
        return text
    return (value or "").strip()


def build_headers(authorization: str, cookie: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://master-zak.ru",
        "Referer": "https://master-zak.ru/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if authorization:
        headers["Authorization"] = authorization
    if cookie:
        headers["Cookie"] = cookie
    return headers


def post_page(
    session: requests.Session,
    url: str,
    body: dict[str, Any],
    *,
    timeout: float,
    retries: int,
    delay: float,
) -> dict[str, Any]:
    last_error = "нет ответа"
    for attempt in range(1, retries + 1):
        try:
            response = session.post(url, json=body, timeout=timeout)
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(delay * attempt)
            continue
        if response.status_code in RETRY_STATUS:
            last_error = f"HTTP {response.status_code}"
            time.sleep(delay * attempt)
            continue
        if response.status_code == 401:
            message = ""
            try:
                message = str(response.json().get("message") or "")
            except ValueError:
                message = response.text[:200]
            raise SystemExit(
                "API ответил 401. Скопируйте из браузера заголовок Authorization "
                "или Cookie рабочего POST и передайте --authorization / --cookie. "
                + message
            )
        if response.status_code >= 400:
            raise SystemExit(
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise SystemExit(f"Ответ не JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SystemExit("Ответ API не объект")
        return data
    raise SystemExit(f"Страница не получена после {retries} попыток: {last_error}")


def expected_last_page(total: int, top: int, start_page: int) -> int:
    """Последняя страница с учётом стыка: последний контракт страницы N повторяется на N+1."""
    if total <= 0 or top <= 0:
        return start_page
    step = max(top - 1, 1)
    extra = math.ceil(max(total - top, 0) / step)
    return start_page + extra


def fetch_all(
    payload: dict[str, Any],
    *,
    url: str,
    headers: dict[str, str],
    pages_dir: Path,
    start_page: int,
    timeout: float,
    retries: int,
    delay: float,
    resume: bool,
) -> tuple[list[dict[str, str]], int]:
    top = int(payload.get("top") or 50)
    if top <= 0:
        raise SystemExit("В payload поле top должно быть больше 0")
    pages_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(headers)

    def page_path(page: int) -> Path:
        return pages_dir / f"page_{page:04d}.json"

    first = load_json(page_path(start_page)) if resume and page_path(start_page).is_file() else None
    if first is None:
        body = deepcopy(payload)
        body["page"] = start_page
        body["top"] = top
        first = post_page(session, url, body, timeout=timeout, retries=retries, delay=delay)
        page_path(start_page).write_text(
            json.dumps(first, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        time.sleep(delay)

    total = int(first.get("total") or 0)
    # Запас сверх стыка страниц: окно выдачи иногда сдвигается, и 200 страниц не набирают total.
    last_page = expected_last_page(total, top, start_page) + 40
    print(
        f"total={total}, top={top}, страницы {start_page}..{last_page}",
        flush=True,
    )

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    stall = 0
    for page in range(start_page, last_page + 1):
        path = page_path(page)
        if resume and path.is_file() and page != start_page:
            data = load_json(path)
        elif page == start_page:
            data = first
        else:
            body = deepcopy(payload)
            body["page"] = page
            body["top"] = top
            data = post_page(session, url, body, timeout=timeout, retries=retries, delay=delay)
            page_rows_now = rows_from_response(data, page)
            if page_rows_now:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            elif path.is_file():
                path.unlink()
            time.sleep(delay)

        page_rows = rows_from_response(data, page)
        added = 0
        for row in page_rows:
            key = row["uid"] or row["number"]
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(row)
            added += 1
        print(
            f"page {page}/{last_page}: {len(page_rows)} в ответе, +{added}, всего {len(rows)}",
            flush=True,
        )
        if len(rows) >= total > 0:
            break
        if page > start_page and added == 0:
            stall += 1
            if stall >= 2:
                print(f"страница {page} без новых контрактов, дальше не иду", flush=True)
                break
        else:
            stall = 0
        if not page_rows and page < last_page:
            print(f"пустая страница {page}, дальше не иду", flush=True)
            break
    return rows, total


def parse_api_datetime(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MSK)


def format_api_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_sign_range(start: datetime, end: datetime) -> datetime | None:
    """Середина интервала, по возможности на полуночи МСК."""
    if end <= start:
        return None
    mid = start + (end - start) / 2
    aligned = mid.replace(hour=0, minute=0, second=0, microsecond=0)
    if start < aligned < end:
        return aligned
    if start < mid < end:
        return mid
    return None


def apply_sign_range(payload: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    body = deepcopy(payload)
    filters = dict(body.get("filters") or {})
    filters["contractSignDateFrom"] = format_api_datetime(start)
    filters["contractSignDateTo"] = format_api_datetime(end)
    body["filters"] = filters
    body["page"] = 0
    return body


def probe_total(
    session: requests.Session,
    url: str,
    body: dict[str, Any],
    *,
    timeout: float,
    retries: int,
    delay: float,
) -> int:
    query = deepcopy(body)
    query["page"] = 0
    query["top"] = 1
    data = post_page(session, url, query, timeout=timeout, retries=retries, delay=delay)
    time.sleep(delay)
    return int(data.get("total") or 0)


def iter_sign_buckets(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    start: datetime,
    end: datetime,
    *,
    timeout: float,
    retries: int,
    delay: float,
    depth: int = 0,
) -> Iterator[tuple[datetime, datetime, int]]:
    if depth > 16 or end <= start:
        return
    total = probe_total(
        session,
        url,
        apply_sign_range(payload, start, end),
        timeout=timeout,
        retries=retries,
        delay=delay,
    )
    print(f"probe {start.date()}..{end.date()} total={total}", flush=True)
    if total <= 0:
        return
    span = end - start
    mid = split_sign_range(start, end)
    if total <= SIGN_BUCKET_MAX or span <= timedelta(days=1) or mid is None:
        yield start, end, total
        return
    yield from iter_sign_buckets(
        session, url, payload, start, mid,
        timeout=timeout, retries=retries, delay=delay, depth=depth + 1,
    )
    yield from iter_sign_buckets(
        session, url, payload, mid, end,
        timeout=timeout, retries=retries, delay=delay, depth=depth + 1,
    )


def fetch_by_sign_date(
    payload: dict[str, Any],
    *,
    url: str,
    headers: dict[str, str],
    pages_dir: Path,
    timeout: float,
    retries: int,
    delay: float,
    resume: bool,
) -> tuple[list[dict[str, str]], int]:
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    start_raw = str(filters.get("contractSignDateFrom") or "")
    end_raw = str(filters.get("contractSignDateTo") or filters.get("executionEndDateTo") or "")
    if not start_raw or not end_raw:
        raise SystemExit("В payload нет contractSignDateFrom и даты окончания интервала")
    start = parse_api_datetime(start_raw)
    end = parse_api_datetime(end_raw)
    session = requests.Session()
    session.headers.update(headers)
    buckets = list(
        iter_sign_buckets(
            session, url, payload, start, end,
            timeout=timeout, retries=retries, delay=delay,
        )
    )
    declared = sum(total for _start, _end, total in buckets)
    print(f"интервалов: {len(buckets)}, сумма total={declared}", flush=True)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, (bucket_start, bucket_end, total) in enumerate(buckets, start=1):
        label = f"{bucket_start.date()}_{bucket_end.date()}"
        print(f"[{index}/{len(buckets)}] {label} total={total}", flush=True)
        bucket_rows, _bucket_total = fetch_all(
            apply_sign_range(payload, bucket_start, bucket_end),
            url=url,
            headers=headers,
            pages_dir=pages_dir / label,
            start_page=0,
            timeout=timeout,
            retries=retries,
            delay=delay,
            resume=resume,
        )
        for row in bucket_rows:
            key = row["uid"] or row["number"]
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(row)
        print(f"уникальных всего: {len(rows)}", flush=True)
    return rows, declared


def rows_from_files(paths: list[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, path in enumerate(paths, start=1):
        data = load_json(path)
        page = index
        if path.stem.startswith("page_"):
            suffix = path.stem.split("_", 1)[-1]
            if suffix.isdigit():
                page = int(suffix)
        for row in rows_from_response(data, page):
            key = row["uid"] or row["number"]
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(row)
    return rows


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, default=ROOT / "deploy" / "zak.txt")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--authorization", default="", help="Значение заголовка Authorization целиком")
    parser.add_argument("--authorization-file", default="", help="Файл с Bearer-токеном или строкой Authorization")
    parser.add_argument("--cookie", default="", help="Значение заголовка Cookie")
    parser.add_argument("--cookie-file", default="")
    parser.add_argument("--start-page", type=int, default=0, help="Первая страница API, нумерация с 0")
    parser.add_argument(
        "--by-sign-date",
        action="store_true",
        help="Обойти интервалы даты заключения: один запрос API обрезает выдачу на 10000",
    )
    parser.add_argument("--delay", type=float, default=0.4, help="Пауза между запросами, секунды")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--pages-dir", type=Path, default=None, help="Куда писать JSON каждой страницы")
    parser.add_argument("--out", type=Path, default=ROOT / "exports" / "masterzak_contracts.csv")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--from-json",
        nargs="*",
        type=Path,
        help="Собрать CSV из уже сохранённых ответов, без запросов к API",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.from_json:
        rows = rows_from_files(args.from_json)
        write_csv(rows, args.out)
        print(f"строк: {len(rows)}")
        print(f"таблица: {args.out}")
        return 0

    payload = load_json(args.payload)
    authorization = read_secret(args.authorization, args.authorization_file or None, "Authorization")
    cookie = read_secret(args.cookie, args.cookie_file or None, "Cookie")
    if not authorization and not cookie:
        raise SystemExit(
            "Нужна сессия Мастер-Зак: --authorization, --authorization-file, "
            "--cookie или --cookie-file"
        )

    ktru = str(payload.get("ktruCode") or "ktru").replace("/", "_")
    pages_dir = args.pages_dir or (ROOT / "exports" / "masterzak_contracts" / ktru)
    headers = build_headers(authorization, cookie)
    if args.by_sign_date:
        rows, total = fetch_by_sign_date(
            payload,
            url=args.url,
            headers=headers,
            pages_dir=pages_dir / "by_sign_date",
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
            resume=args.resume,
        )
    else:
        rows, total = fetch_all(
            payload,
            url=args.url,
            headers=headers,
            pages_dir=pages_dir,
            start_page=args.start_page,
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
            resume=args.resume,
        )
    write_csv(rows, args.out)
    print(f"строк: {len(rows)} из total={total}")
    print(f"страницы: {pages_dir}")
    print(f"таблица: {args.out}")
    if total and len(rows) < total:
        print(
            f"Внимание: собрано {len(rows)} из {total}. "
            "Повторите команду: уже сохранённые страницы будут пропущены.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

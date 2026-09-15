#!/usr/bin/env python3
"""
Поллер ЕИС (getDocsIP): регион + тип документа 44-ФЗ + дата -> архив(ы) -> фильтр КТРУ, дедуп по реестровому номеру, Telegram.

Журнал eis_ktru_changelog.json: первая фиксация пары (закупка | КТРУ), задержка до обработки, хранение ~7 дней (changelog_retention_days).
По умолчанию в Telegram (env.txt и EIS_/RTS_) уходит только текст по новым позициям; полная сводка как раньше: --full-env-telegram-digest.

Перед боевым запуском проверьте тип документа:
  python eis_get_docs.py probe-nsi --nsi-code44 nsiDocumentTypes
Частый код ЭЗТ (ч.12 ст.93): epNotificationEZT2020 - сверить со справочником ЕИС.

Пример:
  python eis_ezt_poll.py --org-region 72 --document-type44 epNotificationEZT2020 --ktru 32.97.11.160-00000001 \\
    --telegram-env rts --env-file env.txt --state-file eis_ezt_state.json

Переменные Telegram (опционально): EIS_TELEGRAM_BOT_TOKEN + EIS_TELEGRAM_CHAT_ID с --notify;
  либо в env.txt: telegram_bot=... и telegram_chat_id=... — при появлении новых позиций КТРУ (см. --no-env-telegram-ktru, --full-env-telegram-digest).

"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from eis_ezt_archive import NoticeMatch, collect_matches_from_zip
from eis_get_docs import (
    build_http_session,
    build_org_region_request,
    build_prepared_part_request,
    collect_prepared_archive_urls,
    download_archive_maybe,
    extract_archive_urls_from_data_info,
    extract_data_info,
    extract_index_request_id,
    load_guid_eruz,
    parse_soap_response_body,
    post_get_docs,
    resolve_service_url,
)

from notify import send_telegram, send_telegram_long, telegram_creds_from_env_file

from eis_analytics_db import DEFAULT_DB_NAME, analytics_db_path, record_eis_poll_run
from eis_history_db import record_eis_history_poll_run
from eis_changelog import append_eis_ktru_observations
from eis_ktru_characteristics import KtruPurchaseLine, PHASE2_PARSE_NOTE as EIS_PARSE_PHASE2_NOTE

EIS_STATE_SCHEMA = 1


@dataclass
class EisPollConfig:
    """Параметры поллера ЕИС (CLI и веб)."""

    env_file: Path
    state_file: Path
    org_region: str
    meta_parent: Path | None = None
    detail_json_name: str = "last_eis_poll.json"
    document_type_44: str = "epNotificationEZT2020"
    ktru_csv: str | None = None
    ktru_file: Path | None = None
    exact_date: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    date_suffix_timezone: str = "+03:00"
    subsystem_type: str = "PRIZ"
    mode: str = "PROD"
    timeout: float = 60.0
    poll_interval: float = 5.0
    max_prepared_polls: int = 60
    require_ezt_xml: bool = False
    dry_run: bool = False
    notify_telegram: bool = False
    telegram_env: str = "eis"
    no_verify: bool = False
    ssl_legacy: bool = False
    tls_max_12: bool = False
    cacert: Path | None = None
    legacy_int44: bool = False
    service_url: str | None = None
    print_raw_on_error: bool = False
    save_zips_dir: Path | None = None
    env_telegram_ktru_digest: bool = True
    #: Журнал новых пар закупка+КТРУ (файл в meta_parent): хранение и метрики задержки.
    changelog_filename: str = "eis_ktru_changelog.json"
    changelog_retention_days: int = 7
    analytics_db_name: str = DEFAULT_DB_NAME
    #: Локальная history-БД: полные характеристики с display_label, без операционного changelog.
    history_db_path: Path | None = None
    #: Если True — в Telegram уходит только текст по новым позициям КТРУ (по журналу), не полная сводка.
    telegram_incremental_only: bool = True


@dataclass
class EisPollResult:
    ok: bool
    errors: list[str]
    log_lines: list[str] = field(default_factory=list)
    new_matches: list[NoticeMatch] = field(default_factory=list)
    zips_downloaded: int = 0
    exact_dates: list[str] = field(default_factory=list)
    state_seen_count: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def matches_as_json(self) -> list[dict[str, Any]]:
        return [_notice_match_as_dict(m) for m in self.new_matches]


def _notice_match_as_dict(m: NoticeMatch) -> dict[str, Any]:
    return {
        "purchase_number": m.purchase_number,
        "ktru_codes": m.ktru_codes,
        "purchase_method_codes": m.purchase_method_codes,
        "source_file": m.source_file,
        "publish_dt_in_eis": m.publish_dt_in_eis,
        "publish_dt_in_etp": m.publish_dt_in_etp,
        "customer_ktru_lines_count": len(m.customer_ktru_lines),
        "customer_characteristics_entries": sum(len(x.characteristics) for x in m.customer_ktru_lines),
    }


def _merge_customer_ktru_lines(a: Iterable[KtruPurchaseLine], b: Iterable[KtruPurchaseLine]) -> list[KtruPurchaseLine]:
    order: list[str] = []
    buckets: dict[str, KtruPurchaseLine] = {}

    def _ingest(block: Iterable[KtruPurchaseLine]) -> None:
        for ln in block:
            if ln.line_key not in buckets:
                order.append(ln.line_key)
                buckets[ln.line_key] = ln
                continue
            cur = buckets[ln.line_key]
            pair_seen = {(c.match_key, c.value_display) for c in cur.characteristics}
            merged_chars = list(cur.characteristics)
            for c in ln.characteristics:
                key_pair = (c.match_key, c.value_display)
                if key_pair in pair_seen:
                    continue
                pair_seen.add(key_pair)
                merged_chars.append(c)
            buckets[ln.line_key] = KtruPurchaseLine(
                line_key=cur.line_key,
                purchase_number=(cur.purchase_number or "").strip() or (ln.purchase_number or "").strip(),
                object_sid=(cur.object_sid or "").strip() or (ln.object_sid or "").strip(),
                object_external_sid=(cur.object_external_sid or "").strip() or (ln.object_external_sid or "").strip(),
                position_index=min(cur.position_index, ln.position_index),
                ktru_code=(cur.ktru_code or "").strip() or (ln.ktru_code or "").strip(),
                position_display_name=(cur.position_display_name or "").strip()
                or (ln.position_display_name or "").strip(),
                price_per_unit_display=(cur.price_per_unit_display or "").strip()
                or (ln.price_per_unit_display or "").strip(),
                line_sum_display=(cur.line_sum_display or "").strip() or (ln.line_sum_display or "").strip(),
                notice_total_display=(cur.notice_total_display or "").strip()
                or (ln.notice_total_display or "").strip(),
                customer_organization_display=(cur.customer_organization_display or "").strip()
                or (ln.customer_organization_display or "").strip(),
                characteristics=merged_chars,
            )

    _ingest(a)
    _ingest(b)
    return [buckets[k] for k in order]


def _notice_publish_caption(m: NoticeMatch) -> str:
    if m.publish_dt_in_eis:
        return f"ЕИС: {m.publish_dt_in_eis}"
    if m.publish_dt_in_etp:
        return f"ЭТП: {m.publish_dt_in_etp}"
    return "публикация: —"


def _agg_publish_line(bag: dict[str, Any]) -> str:
    eis_e = bag.get("eis") or ""
    etp_e = bag.get("etp") or ""
    if eis_e:
        return f"ЕИС: {eis_e}"
    if etp_e:
        return f"ЭТП: {etp_e}"
    return "публикация: —"


def _notice_agg_merge_into(
    notice_agg: dict[str, dict[str, Any]], m: NoticeMatch
) -> None:
    pn = (m.purchase_number or "").strip()
    dedupe_key = pn if pn else f"FILE:{m.source_file}:{','.join(m.ktru_codes)}"
    if dedupe_key not in notice_agg:
        notice_agg[dedupe_key] = {"eis": "", "etp": "", "ktru": set()}
    bag = notice_agg[dedupe_key]
    bag["ktru"].update(m.ktru_codes)
    if m.publish_dt_in_eis and not bag["eis"]:
        bag["eis"] = m.publish_dt_in_eis
    if m.publish_dt_in_etp and not bag["etp"]:
        bag["etp"] = m.publish_dt_in_etp


def _notice_run_dedupe_key(m: NoticeMatch) -> str:
    """Тот же ключ, что для state/new_matches (номер закупки или файл+КТРУ)."""
    pn = (m.purchase_number or "").strip()
    if pn:
        return pn
    return f"FILE:{m.source_file}:{','.join(m.ktru_codes)}"


def _merge_notice_run_roll(prev: NoticeMatch, m: NoticeMatch) -> NoticeMatch:
    """Слияние дублей одной закупки из нескольких архивов/ZIP одного опроса."""
    merged_lines = _merge_customer_ktru_lines(prev.customer_ktru_lines, m.customer_ktru_lines)
    return NoticeMatch(
        purchase_number=(prev.purchase_number or "").strip() or (m.purchase_number or "").strip(),
        ktru_codes=sorted(set(prev.ktru_codes) | set(m.ktru_codes)),
        purchase_method_codes=sorted(set(prev.purchase_method_codes) | set(m.purchase_method_codes)),
        source_file=prev.source_file if prev.source_file.strip() else m.source_file,
        publish_dt_in_eis=(prev.publish_dt_in_eis or "").strip()
        or (m.publish_dt_in_eis or "").strip(),
        publish_dt_in_etp=(prev.publish_dt_in_etp or "").strip()
        or (m.publish_dt_in_etp or "").strip(),
        customer_ktru_lines=merged_lines,
    )


def _sorted_notice_rows(rows: Iterable[NoticeMatch]) -> list[NoticeMatch]:
    return sorted(
        rows,
        key=lambda m: (
            (m.purchase_number or "").strip().lower(),
            m.source_file or "",
            ",".join(m.ktru_codes),
        ),
    )


def _incremental_ktru_telegram_text(
    entries: list[dict[str, Any]],
    cfg: EisPollConfig,
    exact_dates_list: list[str],
    zips_total: int,
) -> str:
    lines = [
        "ЕИС: новые позиции КТРУ",
        f"Регион {cfg.org_region}, тип {cfg.document_type_44}",
        f"Архивов: {zips_total}, дата(ы): {', '.join(exact_dates_list)}",
        "",
    ]
    for e in entries:
        lat = e.get("latency_seconds")
        if isinstance(lat, (int, float)):
            ls = f"{lat:.3g} с"
        else:
            ls = "—"
        lines.append(
            f"• КТРУ {e.get('ktru', '—')} · {e.get('purchase_number', '—')}\n"
            f"  публ.: {e.get('publish_display_msk', '—')} ({e.get('publish_source', '—')})\n"
            f"  получ.: {e.get('received_display_msk', '—')} · Δ {ls}"
        )
    return "\n".join(lines)


def _full_env_digest_text(
    notice_agg: dict[str, dict[str, Any]],
    all_ktru_this_run: set[str],
    cfg: EisPollConfig,
    exact_dates_list: list[str],
    zips_total: int,
    new_matches_count: int,
) -> str:
    notice_keys = sorted(notice_agg.keys(), key=lambda x: (len(x), x))
    notice_lines = [
        (
            f"{k} | {_agg_publish_line(notice_agg[k])} | "
            f"КТРУ: {', '.join(sorted(notice_agg[k]['ktru'])) or '—'}"
        )
        for k in notice_keys
    ]
    ktru_lines = sorted(all_ktru_this_run) if all_ktru_this_run else ["(нет подходящих позиций КТРУ)"]
    return "\n".join(
        [
            f"ЕИС: регион {cfg.org_region}, тип {cfg.document_type_44}",
            f"Даты: {', '.join(exact_dates_list)}",
            f"Архивов: {zips_total}, новых закупок: {new_matches_count}",
            f"Уникальных КТРУ в отфильтрованной выдаче: {len(all_ktru_this_run)}",
            "",
            "Извещения (отфильтрованная выдача):",
            *(notice_lines if notice_lines else ["(нет извещений с подходящим КТРУ)"]),
            "",
            "Уникальные КТРУ:",
            *ktru_lines,
        ]
    )


def _xml_bool(val: Any) -> bool:
    if val is True:
        return True
    if isinstance(val, str) and val.strip().lower() == "true":
        return True
    return False


def _data_no_data(di: dict[str, Any] | None) -> bool:
    if not isinstance(di, dict):
        return False
    return _xml_bool(di.get("noData"))


def _data_error(di: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(di, dict):
        return None
    e = di.get("errorInfo")
    return e if isinstance(e, dict) else None


def load_eis_state(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, dict):
        return set()
    raw = data.get("seen_reestr")
    if isinstance(raw, list):
        return {str(x).strip() for x in raw if str(x).strip()}
    return set()


def save_eis_state(path: Path, seen: set[str]) -> None:
    payload = {
        "schema": EIS_STATE_SCHEMA,
        "seen_reestr": sorted(seen, key=lambda x: (len(x), x)),
        "updated_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_ktru_prefixes(cli_list: str | None, ktru_file: Path | None) -> list[str]:
    out: list[str] = []
    if cli_list:
        out.extend(x.strip() for x in cli_list.split(",") if x.strip())
    if ktru_file and ktru_file.is_file():
        for line in ktru_file.read_text(encoding="utf-8").splitlines():
            s = line.split("#", 1)[0].strip()
            if s:
                out.append(s)
    return out


def resolve_telegram_creds(pref: str) -> tuple[str, str]:
    if pref == "eis":
        tok = os.environ.get("EIS_TELEGRAM_BOT_TOKEN", "").strip()
        cid = os.environ.get("EIS_TELEGRAM_CHAT_ID", "").strip()
        return tok, cid
    tok = os.environ.get("RTS_TELEGRAM_BOT_TOKEN", "").strip()
    cid = os.environ.get("RTS_TELEGRAM_CHAT_ID", "").strip()
    return tok, cid


def fetch_archive_zip_blobs(
    session: requests.Session,
    service_url: str,
    token: str,
    *,
    org_region: str,
    subsystem_type: str,
    document_type_44: str,
    exact_date: str,
    mode: str,
    timeout: float,
    poll_interval: float,
    max_prepared_polls: int,
    print_raw_on_error: bool,
    log_lines: list[str],
) -> tuple[list[bytes], str | None]:
    """
    Вызывает getDocsByOrgRegion, при отсутствии archiveUrl - цикл getPreparedPart, скачивает все архивы.
    Возвращает (список ZIP в памяти, сообщение об ошибке или None).
    """
    doc_request_uid = str(uuid.uuid4())
    dl_timeout = max(timeout * 4, 120.0)

    def _fetch_blobs(url_list: list[str]) -> tuple[list[bytes], str | None]:
        blobs: list[bytes] = []
        failures: list[str] = []
        for u in url_list:
            data, err = download_archive_maybe(session, u, token, dl_timeout)
            if data is not None:
                blobs.append(data)
            elif err:
                failures.append(err)
                log_lines.append(f"Ошибка скачивания архива: {err}")
        if not blobs:
            return [], ("; ".join(failures) if failures else None)
        return blobs, None

    soap = build_org_region_request(
        token,
        org_region=org_region,
        subsystem_type=subsystem_type,
        document_type_44=document_type_44,
        document_type_223=None,
        exact_date=exact_date,
        request_id=doc_request_uid,
        mode=mode,
    )

    resp = post_get_docs(session, service_url, soap, timeout)
    code, payload = parse_soap_response_body(resp, print_raw_on_error=print_raw_on_error)
    if code != 0 or not isinstance(payload, dict):
        return [], f"getDocsByOrgRegion: разбор ответа, код {code}"

    uid = extract_index_request_id(payload) or doc_request_uid
    if uid != doc_request_uid:
        log_lines.append(
            f"Примечание: index/id в ответе ({uid}) отличается от отправленного UUID, берем значение из ответа."
        )

    di = extract_data_info(payload)
    if _data_error(di):
        return [], f"dataInfo errorInfo: {_data_error(di)}"

    urls_immediate = extract_archive_urls_from_data_info(di)
    if urls_immediate:
        return _fetch_blobs(urls_immediate)

    if _data_no_data(di):
        log_lines.append(
            f"ЕИС noData: регион {org_region}, тип {document_type_44}, дата {exact_date}"
        )
        return [], None

    for attempt in range(max(1, max_prepared_polls)):
        if attempt > 0:
            time.sleep(max(poll_interval, 0.1))
        soap_p = build_prepared_part_request(token, uid, request_id=None, mode=mode)
        r2 = post_get_docs(session, service_url, soap_p, timeout)
        c2, pay2 = parse_soap_response_body(r2, print_raw_on_error=print_raw_on_error)
        if c2 != 0 or not isinstance(pay2, dict):
            return [], f"getPreparedPart: разбор ответа, код {c2}, попытка {attempt + 1}"
        if isinstance(pay2.get("errorInfo"), dict):
            return [], f"getPreparedPart errorInfo: {pay2.get('errorInfo')}"
        di_p = extract_data_info(pay2)
        if not isinstance(di_p, dict):
            continue
        urls_p = collect_prepared_archive_urls(di_p)
        if urls_p:
            return _fetch_blobs(urls_p)
        if _xml_bool(di_p.get("isPreparationCompleted")):
            log_lines.append(
                f"ЕИС: подготовка завершена без архивов (регион {org_region}, "
                f"тип {document_type_44}, дата {exact_date})"
            )
            return [], None

    return [], f"Таймаут: за {max_prepared_polls} попыток не получены ссылки на архив (docRequestUid={uid})."


def _write_detail_json(meta_parent: Path, detail_name: str, payload: dict[str, Any]) -> None:
    try:
        meta_parent.mkdir(parents=True, exist_ok=True)
        (meta_parent / detail_name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _write_sidecar_meta(meta_parent: Path | None, payload: dict[str, Any]) -> None:
    """Старый файл eis_ezt_last_run.json рядом со state для совместимости с CLI."""
    if meta_parent is None:
        return
    try:
        m = meta_parent / "eis_ezt_last_run.json"
        m.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def daterange_inclusive(d0: date, d1: date) -> list[date]:
    if d1 < d0:
        d0, d1 = d1, d0
    out: list[date] = []
    cur = d0
    while cur <= d1:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def build_exact_dates_list(
    exact_date: str | None,
    date_from: date | None,
    date_to: date | None,
    date_suffix_timezone: str,
) -> list[str]:
    today = datetime.now().astimezone().date()
    exact_dates_list: list[str] = []
    if exact_date:
        ed = exact_date.strip()
        if "+" not in ed and "Z" not in ed.upper():
            suf = date_suffix_timezone.strip()
            if not suf.startswith(("+", "-")):
                suf = "+" + suf
            base = ed.split("T", 1)[0].split("+", 1)[0]
            ed = f"{base}{suf}"
        exact_dates_list = [ed]
    else:
        d0 = date_from or today
        d1 = date_to or today
        suf = date_suffix_timezone.strip()
        if not suf.startswith(("+", "-")):
            suf = "+" + suf
        for d in daterange_inclusive(d0, d1):
            exact_dates_list.append(d.isoformat() + suf)
    return exact_dates_list


def run_eis_poll(cfg: EisPollConfig) -> EisPollResult:
    log_lines: list[str] = []
    errors: list[str] = []
    ktru_prefixes = load_ktru_prefixes(cfg.ktru_csv, cfg.ktru_file)
    if not ktru_prefixes:
        if cfg.history_db_path is not None:
            log_lines.append("Исторический backfill: без фильтра КТРУ (вся выдача по типу документа).")
        else:
            log_lines.append(
                "Предупреждение: фильтр КТРУ пуст; в выдачу попадут все объекты из архива, подошедшие по XML."
            )

    exact_dates_list = build_exact_dates_list(
        cfg.exact_date,
        cfg.date_from,
        cfg.date_to,
        cfg.date_suffix_timezone,
    )

    session = build_http_session(
        verify_tls=not cfg.no_verify,
        ca_bundle=cfg.cacert,
        ssl_legacy=cfg.ssl_legacy,
        tls_max_12=cfg.tls_max_12,
    )
    service_url = resolve_service_url(cfg.service_url, legacy_int44=cfg.legacy_int44)
    meta_root = cfg.meta_parent if cfg.meta_parent is not None else cfg.state_file.parent

    try:
        token = load_guid_eruz(cfg.env_file)
    except FileNotFoundError as e:
        err_msg = str(e)
        mr = cfg.meta_parent if cfg.meta_parent is not None else cfg.state_file.parent
        detail_fail = {
            "at": datetime.now(timezone.utc).isoformat(),
            "ok": False,
            "errors": [err_msg],
            "log": err_msg,
            "log_lines": [err_msg],
            "matches": [],
            "meta": {},
        }
        _write_detail_json(mr, cfg.detail_json_name, detail_fail)
        return EisPollResult(
            ok=False,
            errors=[err_msg],
            log_lines=[err_msg],
            exact_dates=exact_dates_list,
            meta={},
        )

    seen = load_eis_state(cfg.state_file) if not cfg.dry_run else set()
    new_matches: list[NoticeMatch] = []
    #: Вся выдача с фильтром КТРУ за этот опрос (ключ как в state): для отчёта и веб, не только new.
    filtered_run_matches: dict[str, NoticeMatch] = {}
    all_ktru_this_run: set[str] = set()
    notice_agg: dict[str, dict[str, Any]] = {}
    zips_total = 0

    log_lines.append(f"getDocsIP -> {service_url}")

    for exact in exact_dates_list:
        log_lines.append(f"\n--- exactDate {exact} ---")
        blobs, err = fetch_archive_zip_blobs(
            session,
            service_url,
            token,
            org_region=cfg.org_region,
            subsystem_type=cfg.subsystem_type,
            document_type_44=cfg.document_type_44,
            exact_date=exact,
            mode=cfg.mode,
            timeout=cfg.timeout,
            poll_interval=cfg.poll_interval,
            max_prepared_polls=cfg.max_prepared_polls,
            print_raw_on_error=cfg.print_raw_on_error,
            log_lines=log_lines,
        )
        if err:
            log_lines.append(err)
            errors.append(f"{exact}: {err}")
            continue
        if not blobs:
            log_lines.append("Нет архивов (noData или пустая выдача).")
            continue
        zips_total += len(blobs)
        for i, zb in enumerate(blobs, start=1):
            if cfg.save_zips_dir:
                cfg.save_zips_dir.mkdir(parents=True, exist_ok=True)
                daypart = exact.split("+", 1)[0].replace("-", "")
                zp = cfg.save_zips_dir / f"{daypart}_{i}.zip"
                zp.write_bytes(zb)
                log_lines.append(f"Сохранён архив: {zp}")
            found = collect_matches_from_zip(
                zb,
                ktru_prefixes=ktru_prefixes,
                require_ezt_xml_hint=cfg.require_ezt_xml,
            )
            for m in found:
                all_ktru_this_run.update(m.ktru_codes)
                _notice_agg_merge_into(notice_agg, m)
                dedupe_key = _notice_run_dedupe_key(m)
                if dedupe_key not in filtered_run_matches:
                    filtered_run_matches[dedupe_key] = m
                else:
                    filtered_run_matches[dedupe_key] = _merge_notice_run_roll(
                        filtered_run_matches[dedupe_key], m
                    )
                if dedupe_key in seen:
                    continue
                new_matches.append(m)
                if not cfg.dry_run:
                    seen.add(dedupe_key)

    filtered_sorted = _sorted_notice_rows(filtered_run_matches.values())
    seen_line_meta: set[str] = set()
    customer_characteristics_flat: list[dict[str, Any]] = []
    for m_row in filtered_sorted:
        for kline in m_row.customer_ktru_lines:
            if kline.line_key in seen_line_meta:
                continue
            seen_line_meta.add(kline.line_key)
            customer_characteristics_flat.append(kline.as_dict())

    poll_completed_utc = datetime.now(timezone.utc).replace(microsecond=0)
    changelog_new: list[dict[str, Any]] = []
    analytics_db = analytics_db_path(meta_root, cfg.analytics_db_name)
    analytics_run_id: int | None = None
    if not cfg.dry_run:
        if cfg.history_db_path is None:
            changelog_new = append_eis_ktru_observations(
                meta_root / cfg.changelog_filename,
                filtered_sorted,
                poll_completed_utc,
                retention_days=cfg.changelog_retention_days,
            )
        try:
            if cfg.history_db_path is not None:
                analytics_run_id = record_eis_history_poll_run(
                    cfg.history_db_path,
                    filtered_sorted,
                    finished_at_utc=poll_completed_utc,
                    org_region=cfg.org_region,
                    document_type_44=cfg.document_type_44,
                    ktru_filter=cfg.ktru_csv,
                    exact_dates=exact_dates_list,
                    zips_downloaded=zips_total,
                    ok=len(errors) == 0,
                    errors=errors,
                )
                log_lines.append(
                    f"SQLite history: run_id={analytics_run_id} -> {cfg.history_db_path}"
                )
            else:
                analytics_run_id = record_eis_poll_run(
                    analytics_db,
                    filtered_sorted,
                    finished_at_utc=poll_completed_utc,
                    org_region=cfg.org_region,
                    document_type_44=cfg.document_type_44,
                    ktru_filter=cfg.ktru_csv,
                    exact_dates=exact_dates_list,
                    zips_downloaded=zips_total,
                    ok=len(errors) == 0,
                    errors=errors,
                )
                log_lines.append(f"SQLite analytics: run_id={analytics_run_id} -> {analytics_db}")
        except Exception as exc:  # noqa: BLE001
            log_lines.append(f"SQLite analytics warning: {exc}")

    summary_lines = [
        f"ЕИС EZT poll: регион {cfg.org_region}, тип {cfg.document_type_44}",
        f"Архивов скачано: {zips_total}, новых по КТРУ: {len(new_matches)}",
    ]
    for m in new_matches:
        summary_lines.append(
            f"* {m.purchase_number or '(нет purchaseNumber)'} | {_notice_publish_caption(m)} | "
            f"КТРУ: {', '.join(m.ktru_codes) or '-'} | {m.source_file}"
        )
    log_lines.extend(["", *summary_lines])

    if cfg.env_telegram_ktru_digest and not cfg.dry_run:
        env_bot, env_chat = telegram_creds_from_env_file(cfg.env_file)
        if env_bot and env_chat:
            if cfg.telegram_incremental_only:
                if changelog_new:
                    digest = _incremental_ktru_telegram_text(
                        changelog_new, cfg, exact_dates_list, zips_total
                    )
                    try:
                        send_telegram_long(digest, env_bot, env_chat, cfg.timeout)
                        log_lines.append("\nTelegram (env.txt): отправлены только новые позиции КТРУ.")
                    except Exception as exc:  # noqa: BLE001
                        err_s = str(exc)
                        log_lines.append(f"\nTelegram (env.txt) error: {err_s}")
                        errors.append(err_s)
                else:
                    log_lines.append("\nTelegram (env.txt): новых позиций КТРУ нет — отправка пропущена.")
            else:
                digest = _full_env_digest_text(
                    notice_agg,
                    all_ktru_this_run,
                    cfg,
                    exact_dates_list,
                    zips_total,
                    len(new_matches),
                )
                try:
                    send_telegram_long(digest, env_bot, env_chat, cfg.timeout)
                    log_lines.append("\nTelegram (env.txt): отправлена полная сводка КТРУ.")
                except Exception as exc:  # noqa: BLE001
                    err_s = str(exc)
                    log_lines.append(f"\nTelegram (env.txt) error: {err_s}")
                    errors.append(err_s)
        elif env_bot and not env_chat:
            log_lines.append(
                "\nTelegram (env.txt): указан бот, но нет chat_id — добавьте telegram_chat_id=... в env.txt "
                "или TELEGRAM_CHAT_ID в окружении."
            )

    tg_tok, tg_chat = resolve_telegram_creds(cfg.telegram_env)
    if cfg.notify_telegram and not cfg.dry_run:
        block = ""
        if cfg.telegram_incremental_only:
            if changelog_new:
                block = _incremental_ktru_telegram_text(
                    changelog_new, cfg, exact_dates_list, zips_total
                )
            else:
                log_lines.append("\nTelegram: новых позиций КТРУ — уведомление не отправлялось.")
        elif new_matches:
            block = "\n".join(summary_lines)
        if block.strip() and tg_tok and tg_chat:
            try:
                send_telegram(block, tg_tok, tg_chat, cfg.timeout)
                log_lines.append("\nTelegram: отправлено.")
            except Exception as exc:  # noqa: BLE001
                err_s = str(exc)
                log_lines.append(f"\nTelegram error: {err_s}")
                errors.append(err_s)

    if not cfg.dry_run:
        save_eis_state(cfg.state_file, seen)
        log_lines.append(f"State: {len(seen)} реестровых номеров -> {cfg.state_file}")

    lines_in_run = sum(
        len(m.customer_ktru_lines)
        if m.customer_ktru_lines
        else max(1, len(m.ktru_codes))
        for m in filtered_sorted
    )
    sidecar_meta = {
        "org_region": cfg.org_region,
        "document_type_44": cfg.document_type_44,
        "exact_dates": exact_dates_list,
        "zips_downloaded": zips_total,
        "new_filtered": len(new_matches),
        "found_filtered": len(filtered_sorted),
        "lines_in_run": lines_in_run,
        "incremental_new_ktru": len(changelog_new),
        "poll_completed_at": poll_completed_utc.isoformat().replace("+00:00", "Z"),
        "changelog_file": cfg.changelog_filename,
        "analytics_db": str(analytics_db),
        "analytics_run_id": analytics_run_id,
        "errors": errors,
        "telegram_env": cfg.telegram_env,
        "notify_telegram": cfg.notify_telegram,
        "env_telegram_ktru_digest": cfg.env_telegram_ktru_digest,
        "telegram_incremental_only": cfg.telegram_incremental_only,
        "unique_ktru_in_run": len(all_ktru_this_run),
    }
    _write_sidecar_meta(meta_root, sidecar_meta)

    # matches — вся отфильтрованная выдача опроса; matches_new — закупки, впервые в state.
    detail = {
        "at": poll_completed_utc.isoformat().replace("+00:00", "Z"),
        "ok": len(errors) == 0,
        "errors": errors,
        "log": "\n".join(log_lines),
        "log_lines": log_lines,
        "matches": [_notice_match_as_dict(m) for m in filtered_sorted],
        "matches_new": [_notice_match_as_dict(m) for m in new_matches],
        "customer_characteristics": customer_characteristics_flat,
        "parse_phase2_note": EIS_PARSE_PHASE2_NOTE,
        "meta": sidecar_meta | {"service_url": service_url},
    }
    _write_detail_json(meta_root, cfg.detail_json_name, detail)

    return EisPollResult(
        ok=len(errors) == 0,
        errors=errors,
        log_lines=log_lines,
        new_matches=new_matches,
        zips_downloaded=zips_total,
        exact_dates=exact_dates_list,
        state_seen_count=len(seen),
        meta=detail["meta"],
    )


def parse_date_iso(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Ожидалась дата YYYY-MM-DD или DD.MM.YYYY, получено {s!r}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="ЕИС: getDocsByOrgRegion + архивы + фильтр КТРУ (ЭЗТ), state и Telegram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parent / "env.txt")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--subsystem-type", default="PRIZ")
    p.add_argument("--mode", default="PROD", choices=("PROD", "TEST"))
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--ssl-legacy", action="store_true")
    p.add_argument("--tls-max-12", action="store_true", dest="tls_max_12")
    p.add_argument("--cacert", type=Path, default=None)
    p.add_argument("--legacy-int44", action="store_true")
    p.add_argument("--service-url", default=None)
    p.add_argument("--org-region", required=True, help="КЛАДР региона заказчика, напр. 72")
    p.add_argument(
        "--document-type44",
        default="epNotificationEZT2020",
        metavar="CODE",
        help="Код типа документа для 44-ФЗ (справочник nsiDocumentTypes). По умолчанию ЭЗТ-извещение.",
    )
    p.add_argument(
        "--date",
        dest="exact_date",
        metavar="DATE",
        default=None,
        help="Один день exactDate, напр. 2026-05-01+03:00 или 2026-05-01 (перекрывает --date-from/--date-to)",
    )
    p.add_argument("--date-from", type=parse_date_iso, default=None)
    p.add_argument("--date-to", type=parse_date_iso, default=None)
    p.add_argument(
        "--date-suffix-timezone",
        default="+03:00",
        help="При переборе --date-from/--date-to добавляется к ISO-дате (exactDate для ЕИС)",
    )
    p.add_argument("--poll-interval", type=float, default=5.0, help="Пауза между вызовами getPreparedPart, сек")
    p.add_argument("--max-prepared-polls", type=int, default=60)
    p.add_argument("--ktru", default=None, metavar="PREFIXES", help="Коды/префиксы КТРУ через запятую")
    p.add_argument("--ktru-file", type=Path, default=None)
    p.add_argument(
        "--require-ezt-xml",
        action="store_true",
        help="Отбрасывать XML без маркеров ЭЗТ (notificationEZT / epNotificationEZT и т.п.)",
    )
    p.add_argument("--state-file", type=Path, default=Path(__file__).resolve().parent / "eis_ezt_state.json")
    p.add_argument("--telegram-env", choices=("eis", "rts"), default="eis", help="Какую пару переменных TELEGRAM брать")
    p.add_argument("--dry-run", action="store_true", help="Не писать state и не слать Telegram")
    p.add_argument("--no-notify", action="store_true", help="Не вызывать Telegram (EIS_/RTS_ переменные)")
    p.add_argument(
        "--no-env-telegram-ktru",
        action="store_true",
        help="Не слать в Telegram сводку КТРУ из токена/chat_id в env.txt",
    )
    p.add_argument("--print-raw-on-error", action="store_true")
    p.add_argument(
        "--save-zips-dir",
        type=Path,
        default=None,
        help="Если указано — сохранять сырые zip по каждому дню (YYYYMMDD_N.zip)",
    )
    p.add_argument(
        "--full-env-telegram-digest",
        action="store_true",
        help="Слать полную сводку в Telegram из env.txt как раньше (по умолчанию — только новые позиции КТРУ).",
    )
    p.add_argument(
        "--analytics-db",
        default=os.environ.get("EIS_ANALYTICS_DB", DEFAULT_DB_NAME),
        help=f"Имя/путь SQLite-базы аналитики (по умолчанию {DEFAULT_DB_NAME}; dry-run не пишет).",
    )
    args = p.parse_args()

    cfg = EisPollConfig(
        env_file=args.env_file,
        state_file=args.state_file,
        meta_parent=args.state_file.parent,
        org_region=args.org_region,
        document_type_44=args.document_type44,
        ktru_csv=args.ktru,
        ktru_file=args.ktru_file,
        exact_date=args.exact_date,
        date_from=args.date_from,
        date_to=args.date_to,
        date_suffix_timezone=args.date_suffix_timezone,
        subsystem_type=args.subsystem_type,
        mode=args.mode,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        max_prepared_polls=args.max_prepared_polls,
        require_ezt_xml=args.require_ezt_xml,
        dry_run=args.dry_run,
        notify_telegram=not args.no_notify,
        telegram_env=args.telegram_env,
        no_verify=args.no_verify,
        ssl_legacy=args.ssl_legacy,
        tls_max_12=args.tls_max_12,
        cacert=args.cacert,
        legacy_int44=args.legacy_int44,
        service_url=args.service_url,
        print_raw_on_error=args.print_raw_on_error,
        save_zips_dir=args.save_zips_dir,
        env_telegram_ktru_digest=not args.no_env_telegram_ktru,
        analytics_db_name=args.analytics_db,
        telegram_incremental_only=not args.full_env_telegram_digest,
    )
    result = run_eis_poll(cfg)
    for line in result.log_lines:
        print(line)
    if result.errors:
        print("Ошибки:", "; ".join(result.errors), file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

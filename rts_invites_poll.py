#!/usr/bin/env python3
"""
Poll RTS-Tender supplier LK for IncomingInvite jqGrid list (manual session via Cookie).

После входа в ЛК браузером по ЭП скопируйте значение заголовка Cookie в файл
(опция --cookie-file, по умолчанию cookies_rts.txt рядом со скриптом).
Не коммитьте этот файл.

Первый запуск: пока state пустой, все текущие строки считаются «новыми».
Чтобы не слать уведомления по старым приглашениям, один раз выполните с --no-notify
(сохранится state без Telegram/Webhook/Ingest), затем работайте без этого флага.

Полный текст письма (--fetch-bodies): для каждой строки jqGrid выполняется GET MessageView.aspx;
тело извлекается из разметки (div.message). Учитывайте N запросов и паузу ~0.35 с между ними.

Уведомления (любые из включённых по env или флагах):
  RTS_TELEGRAM_BOT_TOKEN + RTS_TELEGRAM_CHAT_ID — Telegram Bot API sendMessage.
  RTS_ALERT_WEBHOOK_URL — POST JSON массива новых записей на ваш URL.

Опционально приём на ВМ:
  RTS_INGEST_URL + RTS_INGEST_API_KEY — POST JSON только при появлении новых приглашений.

jqGrid параметры см. вкладку Network на MessagesList.aspx?Type=IncomingInvite.
Пример успешного JSON: ключи page,total,records,rows[{id,cell:[...]}].
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from notify import send_telegram

from rts_lot_items import (
    discover_trade_numeric_id_from_auction_view,
    extract_trade_view_urls,
    fetch_lot_items_search_paged,
    format_lot_items_response_plain,
)

DEFAULT_ORIGIN = "https://app.rts-tender.ru"
DEFAULT_MESSAGES_PATH = "/supplier/lk/Private/MessagesList.aspx"
DEFAULT_JQGRID_ID = "BaseMainContent_MainContent_MessagesContent_jqgMessages"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
STATE_SCHEMA_VERSION = 1


@dataclass
class InviteRow:
    invite_id: str
    lk_url: str
    flag_read: str | None
    sender: str | None
    theme: str | None
    sent_at: str | None


def _jqgrid_cell_parts(row: dict[str, Any]) -> list[str]:
    """Список ячеек: jqGrid может отдавать cell массивом или объектом {имя колонки: значение}."""
    raw = row.get("cell")
    if isinstance(raw, list):
        return ["" if x is None else str(x) for x in raw]
    if isinstance(raw, dict):
        return ["" if v is None else str(v) for v in raw.values()]
    return []


def _unwrap_cell_href_or_text(fragment: str) -> str:
    """Содержимое ячейки или href из <a href="…">."""
    s = (fragment or "").strip()
    if not s:
        return ""
    m = re.search(r'href\s*=\s*"([^"]+)"', s, re.I)
    if m:
        return html.unescape(m.group(1).replace("&amp;", "&")).strip()
    m = re.search(r"href\s*=\s*'([^']+)'", s, re.I)
    if m:
        return html.unescape(m.group(1).replace("&amp;", "&")).strip()
    return html.unescape(s.replace("&amp;", "&")).strip()


def _to_absolute_supplier_url(path_or_url: str) -> str:
    path_or_url = path_or_url.strip().replace("\u0026", "&").replace("&amp;", "&")
    if path_or_url.startswith("//"):
        return "https:" + path_or_url
    if path_or_url.startswith("/"):
        return DEFAULT_ORIGIN + path_or_url
    if path_or_url.lower().startswith("http"):
        return path_or_url
    return DEFAULT_ORIGIN + "/" + path_or_url.lstrip("/")


def _strip_cell_plain(s: str | None) -> str | None:
    if not s:
        return None
    t = re.sub(r"<[^>]+>", "", str(s))
    t = html.unescape(t).strip()
    return t or None


def parse_jqgrid_row(row: dict[str, Any]) -> InviteRow | None:
    rid = row.get("id")
    if rid is None:
        rid = row.get("Id")
    if rid is None:
        return None
    invite_id_str = str(rid).strip()

    parts = _jqgrid_cell_parts(row)
    unwraps = [_unwrap_cell_href_or_text(p) for p in parts]

    lk_url_opt: str | None = None
    for u in unwraps:
        if "messageview.aspx" in u.lower():
            lk_url_opt = _to_absolute_supplier_url(u)
            break
    if lk_url_opt is None:
        for u in unwraps:
            low = u.lower()
            if "/supplier/" in low and ".aspx" in low:
                lk_url_opt = _to_absolute_supplier_url(u)
                break

    message_view_fallback = (
        f"{DEFAULT_ORIGIN}/supplier/lk/Private/MessageView.aspx?"
        f"Type=IncomingInvite&Id={invite_id_str}"
    )
    lk_url = lk_url_opt or message_view_fallback

    link_idx = next((i for i, u in enumerate(unwraps) if "messageview.aspx" in u.lower()), -1)
    if link_idx < 0:
        link_idx = next((i for i, u in enumerate(unwraps) if lk_url_opt and u and u in lk_url_opt), -1)
    flag_read = sender = theme = sent_at = None
    if link_idx >= 0 and link_idx + 1 < len(parts):
        seq = parts[link_idx + 1 : link_idx + 5]
        seq.extend([""] * 4)
        flag_read = _strip_cell_plain(seq[0])
        sender = _strip_cell_plain(seq[1])
        theme = _strip_cell_plain(seq[2])
        sent_at = _strip_cell_plain(seq[3])
    elif len(parts) >= 6:
        flag_read = _strip_cell_plain(parts[2])
        sender = _strip_cell_plain(parts[3])
        theme = _strip_cell_plain(parts[4])
        sent_at = _strip_cell_plain(parts[5])
    elif len(parts) >= 5:
        flag_read = _strip_cell_plain(parts[1])
        sender = _strip_cell_plain(parts[2])
        theme = _strip_cell_plain(parts[3])
        sent_at = _strip_cell_plain(parts[4])

    return InviteRow(
        invite_id=invite_id_str,
        lk_url=lk_url,
        flag_read=flag_read,
        sender=sender,
        theme=theme,
        sent_at=sent_at,
    )


def load_cookie_header(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"Файл пустой или отсутствует Cookie: {path}")
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    return raw


def load_state(path: Path) -> tuple[set[str], dict[str, Any]]:
    if not path.is_file():
        return set(), {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set(), {}
    if not isinstance(data, dict):
        return set(), {}
    seen = data.get("seen_ids")
    if isinstance(seen, list):
        ids = {str(x) for x in seen}
    elif isinstance(seen, dict):
        ids = set(seen.keys())
    else:
        ids = set()
    return ids, data


def save_state(path: Path, seen_ids: set[str]) -> None:
    payload = {
        "schema": STATE_SCHEMA_VERSION,
        "seen_ids": sorted(seen_ids, key=lambda x: int(x) if x.isdigit() else 0),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_query_params(
    *,
    jqgrid_id: str,
    date_from: str,
    date_to: str,
    only_not_read: bool,
    nd_ms: int,
    rows_per_page: int,
    page: int,
    sidx: str,
    sord: str,
) -> dict[str, str | int | bool]:
    return {
        "Type": "IncomingInvite",
        "jqGridID": jqgrid_id,
        "auto_SendingDateTimeFrom": date_from,
        "auto_SendingDateTimeTo": date_to,
        "auto_IsOnlyNotRead": str(only_not_read).lower(),
        "auto_SearchString": "",
        "nd": nd_ms,
        "rows": rows_per_page,
        "page": page,
        "sidx": sidx,
        "sord": sord,
    }


def format_date_window(days: int) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=max(1, days))
    def fmt(d: datetime) -> str:
        return d.strftime("%d.%m.%Y")

    return fmt(start), fmt(end)


def looks_like_login_or_error_html(body: str) -> bool:
    if not body or body.lstrip().startswith("{"):
        return False
    low = body.lower()
    hints = ("login.aspx", "/auth/sso", "aspnetform", "<!doctype html", "<html")
    return any(h in low for h in hints)


def fetch_all_pages(session: requests.Session, url_base: str, params_base: dict, timeout: float) -> tuple[int, list[InviteRow]]:
    nd = int(time.time() * 1000)
    params_full = dict(params_base)
    params_full["nd"] = nd
    page = 1
    rows_per_page = int(params_base["rows"])
    all_records: list[InviteRow] = []
    records_total = 0

    while True:
        params = dict(params_full)
        params["page"] = page
        r = session.get(
            url_base,
            params=params,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{DEFAULT_ORIGIN}/supplier/lk/Private/MessagesList.aspx?Type=IncomingInvite",
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} при опросе jqGrid")
        text = r.text.strip()
        if looks_like_login_or_error_html(text):
            raise RuntimeError(
                "Похоже на ответ страницы логина/HTML вместо JSON. Обновите cookie после входа по ЭП в браузере."
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Ответ не JSON (сессия или формат изменились?): {e}") from e

        records_total = int(payload.get("records") or 0)
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            rows = []

        for row in rows:
            if not isinstance(row, dict):
                continue
            parsed = parse_jqgrid_row(row)
            if parsed:
                all_records.append(parsed)

        if records_total <= 0:
            break
        if len(all_records) >= records_total:
            break
        if len(rows) < rows_per_page:
            break
        page += 1
        time.sleep(0.3)

    return records_total, all_records


def fetch_message_view_html(session: requests.Session, lk_url: str, timeout: float) -> str:
    url = lk_url if lk_url.startswith("http") else (DEFAULT_ORIGIN + lk_url)
    r = session.get(
        url,
        headers={"Accept": "text/html,application/xhtml+xml,*/*"},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise RuntimeError(f"MessageView HTTP {r.status_code}")
    return r.text


def _html_inner_to_plain(fragment: str) -> str:
    """Текст тела письма: <br> → перевод строки, теги убрать, сущности раскодировать."""
    t = re.sub(r"(?is)<br\s*/?>", "\n", fragment)
    t = re.sub(r"(?is)</p\s*>", "\n", t)
    t = re.sub(r"(?is)<p[^>]*>", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    lines = [" ".join(part.split()) for part in t.splitlines()]
    return "\n".join(lines).strip()


def _extract_message_body_inner_html(page_html: str) -> str:
    """Сырой HTML внутри div с классом message (первое совпадение)."""
    for pat in (
        r'(?is)<div\s+class="message"\s*>(.*?)</div>',
        r"(?is)<div\s+class='message'\s*>(.*?)</div>",
        r'(?is)<div\s+[^>]*\bclass\s*=\s*["\'][^"\']*\bmessage\b[^"\']*["\'][^>]*>(.*?)</div>',
    ):
        m = re.search(pat, page_html)
        if m:
            return m.group(1)
    return ""


def parse_message_view_html(page_html: str) -> dict[str, str]:
    """Разбор HTML MessageView: тело письма (div.message) + номер извещения / ссылка на платформу."""
    out: dict[str, str] = {}

    inner = _extract_message_body_inner_html(page_html)
    out["body_text"] = _html_inner_to_plain(inner) if inner else ""

    notification_numbers: list[str] = []
    for m in re.finditer(r"notificationNumber=([A-Za-z0-9._-]{6,})", page_html):
        notification_numbers.append(m.group(1))
    for m in re.finditer(r'notificationNumber"\s*:\s*"([^"]+)"', page_html):
        notification_numbers.append(m.group(1))

    hrefs_platform: list[str] = []
    for m in re.finditer(r'https://platform\.rts-tender\.ru[^"\'<>\s]+', page_html):
        hrefs_platform.append(m.group(0))

    if notification_numbers:
        out["notification_number"] = max(set(notification_numbers), key=len)
    if hrefs_platform:
        out["procedure_url_hint"] = hrefs_platform[0][:512]
    return out


def enrichment_from_parsed(parsed: dict[str, Any]) -> dict[str, str]:
    r: dict[str, str] = {}
    if parsed.get("notification_number"):
        r["notification_number"] = str(parsed["notification_number"])
    if parsed.get("procedure_url_hint"):
        r["procedure_url_hint"] = str(parsed["procedure_url_hint"])
    return r


def enrich_from_message_html(session: requests.Session, lk_url: str, timeout: float) -> dict[str, str]:
    """Пробует вытащить notificationNumber из HTML карточки сообщения (отдельный GET)."""
    try:
        raw = fetch_message_view_html(session, lk_url, timeout)
    except RuntimeError:
        return {}
    return enrichment_from_parsed(parse_message_view_html(raw))


def merge_lot_items_enrichment(
    session: requests.Session,
    parsed_message: dict[str, Any],
    *,
    timeout: float,
    page_length: int,
) -> dict[str, Any]:
    """
    По ссылкам Trade/View из HTML письма: числовой TradeId → POST lot-items/search/paged.
    """
    urls = parsed_message.get("_trade_urls")
    if not isinstance(urls, list) or not urls:
        return {}
    for u in urls:
        if not isinstance(u, str) or not u.strip():
            continue
        tid = discover_trade_numeric_id_from_auction_view(session, u.strip(), timeout=timeout)
        if tid is None:
            continue
        try:
            lot_js = fetch_lot_items_search_paged(
                session, tid, page_length=page_length, timeout=timeout
            )
            return {
                "trade_numeric_id": tid,
                "trade_view_url_used": u.strip(),
                "lot_items_total_rows": lot_js.get("TotalRows"),
                "lot_items_plain": format_lot_items_response_plain(lot_js),
            }
        except RuntimeError:
            continue
    return {}


def send_webhook(url_webhook: str, payload: list[dict[str, Any]], timeout: float) -> None:
    r = requests.post(url_webhook, json=payload, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"Webhook HTTP {r.status_code}: {r.text[:400]}")


def send_ingest(base: str | None, api_key: str | None, payload: dict[str, Any], timeout: float) -> None:
    if not base:
        return
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    r = requests.post(base, json=payload, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"Ingest HTTP {r.status_code}: {r.text[:400]}")


def events_to_plain(items: list[InviteRow]) -> list[dict[str, Any]]:
    return [
        {
            "invite_id": x.invite_id,
            "theme": x.theme,
            "sent_at": x.sent_at,
            "sender": x.sender,
            "lk_url": x.lk_url,
        }
        for x in items
    ]


@dataclass
class PollConfig:
    cookie_path: Path
    state_path: Path
    meta_parent: Path | None = None
    timeout: float = 60.0
    rows_per_page: int = 50
    days_window: int = 90
    date_from: str | None = None
    date_to: str | None = None
    only_not_read: bool = False
    jqgrid_id: str = DEFAULT_JQGRID_ID
    sidx: str = "SendingDate"
    sord: str = "desc"
    dry_run: bool = False
    enrich: bool = False
    no_notify: bool = False
    print_all: bool = False
    fetch_bodies_all: bool = False
    fetch_lot_items: bool = False
    lot_items_page_length: int = 50


@dataclass
class PollStructured:
    date_from: str = ""
    date_to: str = ""
    records_total: int = 0
    fetched_count: int = 0
    seen_before: int = 0
    known_after_save: int = 0
    new_invites: list[dict[str, Any]] = field(default_factory=list)
    enriched_events: list[dict[str, Any]] = field(default_factory=list)
    all_rows: list[dict[str, Any]] = field(default_factory=list)
    state_saved: bool = False
    dry_run: bool = False
    telegram_sent: bool = False
    webhook_sent: bool = False
    ingest_sent: bool = False
    notify_errors: list[str] = field(default_factory=list)
    invite_bodies: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PollResult:
    ok: bool
    error: str | None
    stdout: str
    stderr: str
    structured: PollStructured


def save_cookie_to_file(cookie_path: Path, raw_cookie: str) -> None:
    txt = raw_cookie.strip()
    if txt.lower().startswith("cookie:"):
        txt = txt.split(":", 1)[1].strip()
    if not txt:
        raise ValueError("Cookie пустой")
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_path.write_text(txt + ("\n" if not txt.endswith("\n") else ""), encoding="utf-8")


def write_last_poll_meta(meta_dir: Path, result: PollResult) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_path = meta_dir / "last_poll.json"
    ne = (
        len(result.structured.enriched_events)
        if result.structured.enriched_events
        else len(result.structured.new_invites)
    )
    payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "ok": result.ok,
        "error": result.error,
        "records_total": result.structured.records_total,
        "fetched_count": result.structured.fetched_count,
        "new_count": ne,
        "seen_before": result.structured.seen_before,
        "state_saved": result.structured.state_saved,
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_poll(cfg: PollConfig) -> PollResult:
    out: list[str] = []
    err: list[str] = []
    st = PollStructured(dry_run=cfg.dry_run)
    meta_dir = cfg.meta_parent

    try:
        cookie_header = load_cookie_header(cfg.cookie_path)
    except FileNotFoundError:
        msg = (
            f"Нет файла cookie: {cfg.cookie_path}\n"
            "После входа в app.rts-tender.ru по ЭП сохраните заголовок Cookie."
        )
        err.append(msg)
        res = PollResult(False, msg.strip(), "", "\n".join(err), st)
        if meta_dir:
            write_last_poll_meta(meta_dir, res)
        return res
    except ValueError as ex:
        err.append(str(ex))
        res = PollResult(False, str(ex), "", "\n".join(err), st)
        if meta_dir:
            write_last_poll_meta(meta_dir, res)
        return res

    if cfg.date_from and cfg.date_to:
        df, dt_str = cfg.date_from, cfg.date_to
    else:
        df, dt_str = format_date_window(cfg.days_window)
    st.date_from, st.date_to = df, dt_str

    session = requests.Session()
    session.headers["User-Agent"] = DEFAULT_UA
    session.headers["Cookie"] = cookie_header

    params_base = build_query_params(
        jqgrid_id=cfg.jqgrid_id,
        date_from=df,
        date_to=dt_str,
        only_not_read=cfg.only_not_read,
        nd_ms=0,
        rows_per_page=cfg.rows_per_page,
        page=1,
        sidx=cfg.sidx,
        sord=cfg.sord,
    )
    url_base = DEFAULT_ORIGIN + DEFAULT_MESSAGES_PATH

    out.append(f"Окно дат (auto_*): {df} .. {dt_str}")

    try:
        records_total, invites = fetch_all_pages(session, url_base, params_base, cfg.timeout)
    except RuntimeError as ex:
        err.append(str(ex))
        res = PollResult(False, str(ex), "\n".join(out), "\n".join(err), st)
        if meta_dir:
            write_last_poll_meta(meta_dir, res)
        return res

    st.records_total = records_total
    st.fetched_count = len(invites)
    seen_ids, _ = load_state(cfg.state_path)
    st.seen_before = len(seen_ids)

    if records_total != len(invites):
        out.append(
            f"ВНИМАНИЕ: jqGrid records={records_total}, разобрано строк jqGrid→приглашений={len(invites)}."
        )

    current_ids = {x.invite_id for x in invites}
    new_items = [x for x in invites if x.invite_id not in seen_ids]
    st.new_invites = events_to_plain(new_items)

    out.append(f"jqGrid: records(total)={records_total}, загружено строк={len(invites)}")

    if cfg.print_all:
        st.all_rows = [vars(x) for x in invites]
        for it in invites:
            out.append(json.dumps(vars(it), ensure_ascii=False))

    parsed_by_id: dict[str, dict[str, Any]] = {}

    def ensure_message_parsed(inv: InviteRow) -> dict[str, Any]:
        if inv.invite_id in parsed_by_id:
            return parsed_by_id[inv.invite_id]
        raw = fetch_message_view_html(session, inv.lk_url, cfg.timeout)
        p: dict[str, Any] = dict(parse_message_view_html(raw))
        trade_urls = extract_trade_view_urls(raw)
        if trade_urls:
            p["_trade_urls"] = trade_urls
        parsed_by_id[inv.invite_id] = p
        time.sleep(0.35)
        return p

    if cfg.fetch_bodies_all:
        out.append("")
        if invites:
            out.append(f"Текст всех приглашений в окне дат (GET MessageView): {len(invites)} шт.")
            for it in invites:
                try:
                    p = ensure_message_parsed(it)
                    body = (p.get("body_text") or "").strip()
                    rec: dict[str, Any] = {
                        "invite_id": it.invite_id,
                        "sent_at": it.sent_at,
                        "theme": it.theme,
                        "lk_url": it.lk_url,
                        "body_text": body,
                    }
                    if cfg.fetch_lot_items:
                        li = merge_lot_items_enrichment(
                            session, p, timeout=cfg.timeout, page_length=cfg.lot_items_page_length
                        )
                        if li:
                            rec.update(li)
                    st.invite_bodies.append(rec)
                    out.append("")
                    out.append("=" * 72)
                    out.append(f"id={it.invite_id} | {it.sent_at or ''} | {(it.theme or '')[:200]}")
                    out.append(it.lk_url)
                    out.append("-" * 72)
                    if body:
                        out.extend(body.splitlines())
                    else:
                        out.append("(текст не найден: проверьте разметку MessageView / сессию)")
                except Exception as exc:
                    err.append(f"текст приглашения id={it.invite_id}: {exc}")
        elif records_total > 0:
            out.append(
                f"Загрузка текстов: jqGrid reports records={records_total}, но ни одной строки не удалось "
                "разобрать в приглашение (несовместимый формат cell jqGrid?)."
            )
        else:
            out.append("Загрузка текстов: в выбранном окне дат записей нет (records=0).")

    enriched_events: list[dict[str, Any]] = []

    if new_items and not cfg.no_notify:
        out.append(f"Новые приглашения ({len(new_items)}):")
        for it in new_items:
            out.append(
                f"  id={it.invite_id} | {it.sent_at or ''} | {(it.theme or '')[:160]}\n    {it.lk_url}"
            )
            ev: dict[str, Any] = events_to_plain([it])[0]
            if cfg.enrich:
                try:
                    parsed = ensure_message_parsed(it)
                    extra = enrichment_from_parsed(parsed)
                    ev.update(extra)
                    if extra:
                        out.append(f"    enrich: {extra}")
                    if cfg.fetch_lot_items:
                        lim = merge_lot_items_enrichment(
                            session,
                            parsed,
                            timeout=cfg.timeout,
                            page_length=cfg.lot_items_page_length,
                        )
                        if lim:
                            ev.update(lim)
                            out.append(f"    lot-items trade_id={lim.get('trade_numeric_id')} rows={lim.get('lot_items_total_rows')}")
                        elif parsed.get("_trade_urls"):
                            err.append(
                                f"lot-items id={it.invite_id}: не удалось получить числовой TradeId или API"
                            )
                except Exception as exc:
                    err.append(f"enrich error id={it.invite_id}: {exc}")
            enriched_events.append(ev)
        st.enriched_events = enriched_events
    elif new_items and cfg.no_notify:
        out.append(
            f"Новых id (для state): {len(new_items)} — без уведомлений (--no-notify)."
        )
        for it in new_items:
            enriched_events.append(events_to_plain([it])[0])
        st.enriched_events = enriched_events
    else:
        out.append("Новых приглашений нет.")

    telegram_token = os.environ.get("RTS_TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat = os.environ.get("RTS_TELEGRAM_CHAT_ID", "").strip()
    webhook_url = os.environ.get("RTS_ALERT_WEBHOOK_URL", "").strip()
    ingest_url = os.environ.get("RTS_INGEST_URL", "").strip()
    ingest_key = os.environ.get("RTS_INGEST_API_KEY", "").strip()

    if new_items and not cfg.no_notify and enriched_events:
        tg_lines = []
        for ev in enriched_events:
            t = (
                f"Приглашение RTS id={ev.get('invite_id')}\n"
                f"{ev.get('sent_at') or ''}\n{ev.get('theme') or ''}\n{ev.get('lk_url')}\n"
            )
            if ev.get("notification_number"):
                t += f"Номер извещения: {ev['notification_number']}\n"
            if ev.get("procedure_url_hint"):
                t += f"Ссылка (платформа): {ev['procedure_url_hint'][:300]}"
            if ev.get("lot_items_plain"):
                lip = str(ev["lot_items_plain"])[:2800]
                if len(str(ev["lot_items_plain"])) > len(lip):
                    lip += "\n…"
                t += "\n\nПозиции лота (КТРУ):\n" + lip
            tg_lines.append(t.strip())
        text_block = "\n\n---\n\n".join(tg_lines)

        if telegram_token and telegram_chat:
            try:
                send_telegram(text_block, telegram_token, telegram_chat, cfg.timeout)
                out.append("Telegram: отправлено.")
                st.telegram_sent = True
            except Exception as exc:
                st.notify_errors.append(f"Telegram: {exc}")
                err.append(f"Telegram error: {exc}")
        elif webhook_url:
            try:
                send_webhook(webhook_url, enriched_events, cfg.timeout)
                out.append("Webhook: отправлено.")
                st.webhook_sent = True
            except Exception as exc:
                st.notify_errors.append(f"Webhook: {exc}")
                err.append(f"Webhook error: {exc}")

        if ingest_url:
            try:
                send_ingest(
                    ingest_url,
                    ingest_key if ingest_key else None,
                    {"events": enriched_events},
                    cfg.timeout,
                )
                out.append("Ingest: отправлено.")
                st.ingest_sent = True
            except Exception as exc:
                st.notify_errors.append(f"Ingest: {exc}")
                err.append(f"Ingest error: {exc}")

    if not cfg.dry_run and invites:
        next_seen = seen_ids | current_ids
        save_state(cfg.state_path, next_seen)
        st.known_after_save = len(next_seen)
        st.state_saved = True
        out.append(f"State сохранён: {len(next_seen)} id в {cfg.state_path}")
    elif cfg.dry_run:
        out.append("(dry-run: state не обновлён)")

    res = PollResult(True, None, "\n".join(out), "\n".join(err), st)
    if meta_dir:
        write_last_poll_meta(meta_dir, res)
    return res


def main() -> int:
    parser = argparse.ArgumentParser(description="Опрос IncomingInvite RTS-Tender jqGrid.")
    parser.add_argument(
        "--cookie-file",
        type=Path,
        default=Path(__file__).resolve().parent / "cookies_rts.txt",
        help="Файл со строкой Cookie (как после входа по ЭП в браузере)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(__file__).resolve().parent / "rts_invites_state.json",
        help="Файл с известными id приглашений",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--rows-per-page", type=int, default=50, metavar="N")
    parser.add_argument("--days-window", type=int, default=90, metavar="N", help="От N дней до сегодня (auto_SendingDateTime*)")
    parser.add_argument("--date-from", type=str, default=None, metavar="DD.MM.YYYY", help="Переопределить начало окна дат")
    parser.add_argument("--date-to", type=str, default=None, metavar="DD.MM.YYYY")
    parser.add_argument("--only-not-read", action="store_true")
    parser.add_argument("--jqgrid-id", default=DEFAULT_JQGRID_ID)
    parser.add_argument("--sidx", default="SendingDate")
    parser.add_argument("--sord", default="desc")
    parser.add_argument("--dry-run", action="store_true", help="Не записывать state, только показать дифф")
    parser.add_argument("--enrich", action="store_true", help="GET MessageView для новых строк — поиск номера извещения в HTML")
    parser.add_argument(
        "--fetch-bodies",
        action="store_true",
        dest="fetch_bodies_all",
        help="Для каждой строки окна GET MessageView и вывести тело письма (текст из div.message)",
    )
    parser.add_argument(
        "--lot-items",
        action="store_true",
        dest="fetch_lot_items",
        help=(
            "После enrich: ссылки Trade/View из письма → TradeId → POST lot-items/search/paged "
            "(позиции и характеристики КТРУ). Автоматически включает --enrich."
        ),
    )
    parser.add_argument(
        "--lot-items-page-length",
        type=int,
        default=50,
        metavar="N",
        help="Размер страницы для lot-items/search/paged",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Не отправлять Telegram/Webhook/Ingest и не печатать блок «новые» (полезно для первичного сохранения state)",
    )
    parser.add_argument(
        "--print-all",
        action="store_true",
        help="Печать всех строк jqGrid как JSON строк",
    )
    args = parser.parse_args()
    enrich_effective = args.enrich or args.fetch_lot_items

    cfg = PollConfig(
        cookie_path=args.cookie_file,
        state_path=args.state_file,
        meta_parent=args.state_file.resolve().parent,
        timeout=args.timeout,
        rows_per_page=args.rows_per_page,
        days_window=args.days_window,
        date_from=args.date_from,
        date_to=args.date_to,
        only_not_read=args.only_not_read,
        jqgrid_id=args.jqgrid_id,
        sidx=args.sidx,
        sord=args.sord,
        dry_run=args.dry_run,
        enrich=enrich_effective,
        no_notify=args.no_notify,
        print_all=args.print_all,
        fetch_bodies_all=args.fetch_bodies_all,
        fetch_lot_items=args.fetch_lot_items,
        lot_items_page_length=max(5, min(args.lot_items_page_length, 200)),
    )
    result = run_poll(cfg)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.error:
        return 2 if "Нет файла cookie" in result.error else 10
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


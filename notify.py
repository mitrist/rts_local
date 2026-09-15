"""Отправка уведомлений в Telegram и MAX (общий модуль для RTS и ЕИС)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

_LOG = logging.getLogger("notify")

MAX_MESSAGES_URL = "https://platform-api.max.ru/messages"


def html_link(url: str, label: str) -> str:
    from html import escape

    u = (url or "").strip()
    if not u:
        return label
    return f'<a href="{escape(u, quote=True)}">{escape(label)}</a>'


def html_bold(text: str) -> str:
    from html import escape

    return f"<b>{escape(text)}</b>"


def markdown_bold(text: str) -> str:
    safe = (text or "").replace("\\", "\\\\").replace("*", "\\*")
    return f"**{safe}**"


def markdown_link(url: str, label: str) -> str:
    u = (url or "").strip()
    if not u:
        return label
    safe = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"[{safe}]({u})"


def send_telegram(
    text: str,
    token: str,
    chat_id: str,
    timeout: float,
    *,
    parse_mode: str | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, object] = {"chat_id": chat_id, "text": text[:4000]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
        payload["disable_web_page_preview"] = True
    r = requests.post(url, json=payload, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:400]}")


def send_telegram_long(
    text: str,
    token: str,
    chat_id: str,
    timeout: float,
    *,
    limit: int = 3800,
    parse_mode: str | None = None,
) -> None:
    """Несколько sendMessage, если текст длиннее лимита API (по переводам строк)."""
    _send_long_chunks(
        text,
        lambda chunk: send_telegram(chunk, token, chat_id, timeout, parse_mode=parse_mode),
        limit=limit,
    )


def send_telegram_html_long(text: str, token: str, chat_id: str, timeout: float, *, limit: int = 3800) -> None:
    send_telegram_long(text, token, chat_id, timeout, limit=limit, parse_mode="HTML")


def send_max(
    text: str,
    token: str,
    chat_id: str,
    timeout: float,
    *,
    text_format: str | None = None,
) -> None:
    body: dict[str, object] = {"text": text[:4000]}
    if text_format:
        body["format"] = text_format
    r = requests.post(
        MAX_MESSAGES_URL,
        params={"chat_id": chat_id},
        headers={"Authorization": token, "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    if r.status_code != 200:
        raise RuntimeError(f"MAX HTTP {r.status_code}: {r.text[:400]}")


def send_max_long(
    text: str,
    token: str,
    chat_id: str,
    timeout: float,
    *,
    limit: int = 3800,
    text_format: str | None = None,
) -> None:
    _send_long_chunks(
        text,
        lambda chunk: send_max(chunk, token, chat_id, timeout, text_format=text_format),
        limit=limit,
    )


def send_max_markdown_long(text: str, token: str, chat_id: str, timeout: float, *, limit: int = 3800) -> None:
    send_max_long(text, token, chat_id, timeout, limit=limit, text_format="markdown")


def _send_long_chunks(text: str, send_chunk, *, limit: int) -> None:
    t = text.strip()
    if not t:
        return
    while t:
        if len(t) <= limit:
            send_chunk(t)
            return
        cut = t.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        send_chunk(t[:cut].strip())
        t = t[cut:].lstrip("\n")


def _env_file_kv(env_path: Path) -> dict[str, str]:
    from eis_get_docs import parse_env_txt

    if not env_path.is_file():
        return {}
    if not os.access(env_path, os.R_OK):
        _LOG.warning("Нет доступа на чтение %s — используйте переменные окружения или chmod 640", env_path)
        return {}
    return parse_env_txt(env_path)


def _first_env(*names: str, kv: dict[str, str] | None = None) -> str:
    for name in names:
        v = os.environ.get(name, "").strip()
        if v:
            return v
        if kv is not None:
            v = kv.get(name.lower(), "").strip()
            if v:
                return v
    return ""


def telegram_creds_from_env_file(env_path: Path) -> tuple[str, str]:
    """
    Токен и chat_id: TELEGRAM_* / telegram_* в окружении (в т.ч. EnvironmentFile)
    или строки в env.txt: telegram_bot=... и telegram_chat_id=...
    """
    kv = _env_file_kv(env_path)
    bot = _first_env(
        "TELEGRAM_BOT_TOKEN",
        "EIS_ENV_TELEGRAM_BOT",
        "telegram_bot",
        "telegram_bot_token",
        "bot_token",
        kv=kv,
    )
    chat = _first_env(
        "TELEGRAM_CHAT_ID",
        "EIS_ENV_TELEGRAM_CHAT_ID",
        "telegram_chat_id",
        "telegram_chat",
        "chat_id",
        kv=kv,
    )
    return bot, chat


def resolve_telegram_chat_id(env_path: Path, override: str = "") -> str:
    custom = str(override or "").strip()
    if custom:
        return custom
    _, chat = telegram_creds_from_env_file(env_path)
    return chat


def resolve_max_chat_id(env_path: Path, override: str = "") -> str:
    custom = str(override or "").strip()
    if custom:
        return custom
    _, chat = max_creds_from_env_file(env_path)
    return chat


def max_creds_from_env_file(env_path: Path) -> tuple[str, str]:
    """Токен и chat_id MAX из env.txt или MAX_* / max_* в окружении."""
    kv = _env_file_kv(env_path)
    token = _first_env("MAX_BOT_TOKEN", "max_bot_token", kv=kv)
    chat_id = _first_env("MAX_CHAT_ID", "max_chat_id", kv=kv)
    return token, chat_id

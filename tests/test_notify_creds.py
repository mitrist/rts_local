"""Загрузка учётных данных мессенджеров без чтения недоступного файла."""

from __future__ import annotations

import os
from pathlib import Path

import notify


def test_telegram_creds_from_os_environ_when_file_unreadable(tmp_path, monkeypatch):
    secret = tmp_path / "eis.env"
    secret.write_text("telegram_bot=file-token\ntelegram_chat_id=file-chat\n", encoding="utf-8")
    secret.chmod(0o600)
    if os.name != "nt":
        secret.chmod(0o600)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat")

    bot, chat = notify.telegram_creds_from_env_file(secret)
    assert bot == "env-token"
    assert chat == "env-chat"


def test_telegram_creds_from_readable_file(tmp_path, monkeypatch):
    env_file = tmp_path / "eis.env"
    env_file.write_text("telegram_bot=file-token\ntelegram_chat_id=file-chat\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    bot, chat = notify.telegram_creds_from_env_file(env_file)
    assert bot == "file-token"
    assert chat == "file-chat"

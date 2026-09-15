"""Параметры исторического backfill ЕИС (локальный CLI)."""

from __future__ import annotations

from datetime import date

# Закреплённый период выгрузки (можно переопределить флагами CLI).
HISTORY_PERIOD_FROM = date(2026, 1, 1)
HISTORY_PERIOD_TO = date(2026, 5, 31)

# Закупка товаров по ч. 12 ст. 93 44-ФЗ (ЭЗТ), PRIZ documentType44.
HISTORY_DOCUMENT_TYPE_44 = "epNotificationEZT2020"
HISTORY_DOCUMENT_TYPES_44: tuple[str, ...] = (HISTORY_DOCUMENT_TYPE_44,)


def history_document_types(raw: str | None = None) -> list[str]:
    """CSV в raw или полный набор по умолчанию."""
    text = (raw or "").strip()
    if not text:
        return list(HISTORY_DOCUMENT_TYPES_44)
    out: list[str] = []
    seen: set[str] = set()
    for part in text.split(","):
        item = part.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out

"""Catalog of analyst tags for EIS↔PP pair labeling (scenarios + participation intent)."""

from __future__ import annotations

from typing import Any

# scenario_tag: behavior hypothesis for customer/supplier around the pair
SCENARIO_TAGS: tuple[tuple[str, str], ...] = (
    ("", "— не задан —"),
    ("S1_pre_pp", "S1 · ПП до извещения"),
    ("S2_post_pp", "S2 · ПП сразу после извещения"),
    ("S3_reuse", "S3 · Повтор variant (есть prehistory)"),
    ("S4_first_pub", "S4 · Первая публикация variant"),
    ("S5_near_miss", "S5 · Near-miss (почти exact)"),
    ("S6_noise", "S6 · Шум / случайное совпадение"),
    ("S7_republish", "S7 · Переиздание после пустой закупки"),
)

# participation_intent: would the analyst join this procedure
PARTICIPATION_INTENTS: tuple[tuple[str, str], ...] = (
    ("", "— не задан —"),
    ("would_join", "Готов участвовать"),
    ("would_skip", "Не участвую"),
    ("unsure", "Не уверен"),
)

_SCENARIO_CODES = frozenset(code for code, _ in SCENARIO_TAGS if code)
_INTENT_CODES = frozenset(code for code, _ in PARTICIPATION_INTENTS if code)
_SCENARIO_LABELS = {code: label for code, label in SCENARIO_TAGS if code}
_INTENT_LABELS = {code: label for code, label in PARTICIPATION_INTENTS if code}


def normalize_scenario_tag(raw: str | None) -> str:
    code = str(raw or "").strip()
    return code if code in _SCENARIO_CODES else ""


def normalize_participation_intent(raw: str | None) -> str:
    code = str(raw or "").strip()
    return code if code in _INTENT_CODES else ""


def scenario_tag_label(code: str | None) -> str:
    text = str(code or "").strip()
    return _SCENARIO_LABELS.get(text, "")


def participation_intent_label(code: str | None) -> str:
    text = str(code or "").strip()
    return _INTENT_LABELS.get(text, "")


def tagging_ui_context() -> dict[str, Any]:
    return {
        "scenario_tags": [{"code": c, "label": l} for c, l in SCENARIO_TAGS],
        "participation_intents": [{"code": c, "label": l} for c, l in PARTICIPATION_INTENTS],
    }

"""Known equivalent KTRU characteristic labels (EIS notice vs RTS PP card)."""

from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")

# Each group lists labels that refer to the same characteristic in different sources.
CHARACTERISTIC_LABEL_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("Наличие дисплея", "Наличие ЖК-дисплея"),
)

_ALIAS_INDEX: dict[str, frozenset[str]] = {}


def _norm_label(value: str) -> str:
    return _WS_RE.sub(" ", (value or "").strip()).lower()


def _build_alias_index() -> dict[str, frozenset[str]]:
    index: dict[str, frozenset[str]] = {}
    for group in CHARACTERISTIC_LABEL_ALIAS_GROUPS:
        keys = frozenset(_norm_label(label) for label in group if str(label or "").strip())
        if len(keys) < 2:
            continue
        for key in keys:
            index[key] = keys
    return index


def label_alias_keys(label: str) -> frozenset[str]:
    """Normalized label plus known alias equivalents."""
    base = _norm_label(label)
    if not base:
        return frozenset()
    global _ALIAS_INDEX
    if not _ALIAS_INDEX:
        _ALIAS_INDEX = _build_alias_index()
    aliases = _ALIAS_INDEX.get(base)
    if aliases:
        return aliases
    return frozenset({base})


def labels_are_aliases(left: str, right: str) -> bool:
    left_keys = label_alias_keys(left)
    right_keys = label_alias_keys(right)
    return bool(left_keys & right_keys)

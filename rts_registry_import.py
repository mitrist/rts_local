"""Parse RTS supplier registry JSON and import into rts_preoffers.json."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DRAFT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

from rts_preoffers import _clean, load_preoffers_raw, normalize_preoffer_payload, upsert_preoffer

RTS_OFFER_ITEM_EDIT_URL = (
    "https://offers.rts-tender.ru/supplier/price-lists/positions/offer-item/create/{position_id}"
)

MAX_IMPORT_POSITIONS = 500
MAX_IMPORT_JSON_BYTES = 2 * 1024 * 1024

_MATH_NOTATION_RU = {
    "ge": "не менее",
    "gt": "более",
    "le": "не более",
    "lt": "менее",
    "eq": "равно",
}


@dataclass(frozen=True, slots=True)
class RegistryImportItem:
    offer_id: str
    title: str
    ktru_code: str
    rts_url: str
    sku: str
    enabled: bool
    characteristics: list[dict[str, str]]
    source_position_id: int
    offer_status: str | None

    def as_preoffer_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "title": self.title,
            "ktru_code": self.ktru_code,
            "rts_url": self.rts_url,
            "sku": self.sku,
            "enabled": self.enabled,
            "characteristics": self.characteristics,
        }

    def preview_row(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "ktru_code": self.ktru_code,
            "title": self.title,
            "rts_url": self.rts_url,
            "enabled": self.enabled,
            "chars_count": len(self.characteristics),
            "offer_status": self.offer_status or "—",
            "source_position_id": self.source_position_id,
        }


@dataclass
class RegistryImportResult:
    items: list[RegistryImportItem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    filtered_unpublished: list[str] = field(default_factory=list)
    total_in_file: int = 0


def rts_offer_edit_url(position_id: int | str) -> str:
    return RTS_OFFER_ITEM_EDIT_URL.format(position_id=int(position_id))


def offer_id_for_position(position_id: int | str) -> str:
    return f"rts-pos-{int(position_id)}"


def _format_math_bound(value: object, notation: object, *, prefix: str) -> str:
    v = _clean(value)
    if not v:
        return ""
    note = _clean(notation).lower()
    word = _MATH_NOTATION_RU.get(note, note or prefix)
    return f"{word} {v}".strip()


def _format_characteristic_value(raw: dict[str, Any]) -> str:
    qd = _clean(raw.get("qualityDescription"))
    if qd:
        return qd
    cv = _clean(raw.get("concreteValue"))
    if cv:
        return cv
    min_v = _clean(raw.get("minValue"))
    max_v = _clean(raw.get("maxValue"))
    min_note = _clean(raw.get("minMathNotation"))
    max_note = _clean(raw.get("maxMathNotation"))
    okei = _clean(raw.get("okeiFullName"))
    parts: list[str] = []
    if min_v:
        parts.append(_format_math_bound(min_v, min_note, prefix="мин."))
    if max_v:
        parts.append(_format_math_bound(max_v, max_note, prefix="макс."))
    if min_v and max_v and len(parts) == 2:
        text = f"от {min_v} до {max_v}"
        if okei:
            text = f"{text} {okei}"
        return text
    text = " ".join(parts).strip()
    if okei and text:
        text = f"{text} {okei}"
    return text


def _parse_characteristics(raw_chars: object) -> list[dict[str, str]]:
    if not isinstance(raw_chars, list):
        return []
    out: list[dict[str, str]] = []
    for ch in raw_chars:
        if not isinstance(ch, dict):
            continue
        label = _clean(ch.get("name"))
        values_raw = ch.get("characteristicValues")
        value_parts: list[str] = []
        if isinstance(values_raw, list):
            for val in values_raw:
                if not isinstance(val, dict):
                    continue
                if not label:
                    label = _clean(val.get("characteristicName"))
                formatted = _format_characteristic_value(val)
                if formatted and formatted not in value_parts:
                    value_parts.append(formatted)
        if not label and not value_parts:
            continue
        out.append({"label": label or "(без имени)", "value": "; ".join(value_parts) or "—"})
    return out


def _offer_status(raw: dict[str, Any]) -> str:
    offer = raw.get("offer")
    if isinstance(offer, dict):
        return _clean(offer.get("status"))
    return ""


def _is_published(status: str) -> bool:
    return status == "Published"


def _extract_positions(data: object) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        page = data.get("page")
        if isinstance(page, list):
            return [x for x in page if isinstance(x, dict)]
    return []


def _position_to_item(raw: dict[str, Any]) -> RegistryImportItem | None:
    pos_id = raw.get("id")
    if pos_id is None or str(pos_id).strip() == "":
        return None
    try:
        position_id = int(pos_id)
    except (TypeError, ValueError):
        return None
    ktru = raw.get("ktru")
    if not isinstance(ktru, dict):
        return None
    ktru_code = _clean(ktru.get("code"))
    if not ktru_code:
        return None
    title = _clean(raw.get("name")) or _clean(ktru.get("name")) or ktru_code
    status = _offer_status(raw)
    return RegistryImportItem(
        offer_id=offer_id_for_position(position_id),
        title=title,
        ktru_code=ktru_code,
        rts_url=rts_offer_edit_url(position_id),
        sku=_clean(raw.get("protectedOfferItemId")),
        enabled=_is_published(status),
        characteristics=_parse_characteristics(raw.get("ktruCharacteristics")),
        source_position_id=position_id,
        offer_status=status or None,
    )


def parse_registry_json(
    text: str,
    *,
    only_published: bool = True,
) -> RegistryImportResult:
    if len(text.encode("utf-8")) > MAX_IMPORT_JSON_BYTES:
        raise ValueError(
            f"JSON слишком большой (лимит {MAX_IMPORT_JSON_BYTES // (1024 * 1024)} МБ)."
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный JSON: {exc}") from exc

    positions = _extract_positions(data)
    result = RegistryImportResult(total_in_file=len(positions))
    if len(positions) > MAX_IMPORT_POSITIONS:
        raise ValueError(f"Слишком много позиций в файле (лимит {MAX_IMPORT_POSITIONS}).")

    for idx, raw in enumerate(positions, start=1):
        item = _position_to_item(raw)
        if item is None:
            name = _clean(raw.get("name")) or f"строка {idx}"
            pid = raw.get("id")
            result.skipped.append(f"Позиция {name} (id={pid}): нет id или кода КТРУ")
            continue
        if only_published and not _is_published(item.offer_status or ""):
            result.filtered_unpublished.append(
                f"{item.title} (id={item.source_position_id}, статус {item.offer_status or '—'})"
            )
            continue
        result.items.append(item)
    return result


def _offer_snapshot(offer: dict[str, Any]) -> str:
    normalized = normalize_preoffer_payload(offer)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def apply_registry_import(
    path: Path,
    items: list[RegistryImportItem],
) -> tuple[int, int]:
    """Upsert offers; return (written_count, unchanged_count)."""
    existing: dict[str, str] = {}
    for raw in load_preoffers_raw(path).get("offers", []):
        if not isinstance(raw, dict):
            continue
        oid = _clean(raw.get("offer_id"))
        if oid:
            existing[oid] = _offer_snapshot(raw)

    written = 0
    unchanged = 0
    for item in items:
        payload = item.as_preoffer_dict()
        before = existing.get(item.offer_id)
        upsert_preoffer(path, payload)
        after = _offer_snapshot(payload)
        if before == after:
            unchanged += 1
        else:
            written += 1
    return written, unchanged


def preview_rows_from_items(items: list[RegistryImportItem]) -> list[dict[str, Any]]:
    return [item.preview_row() for item in items]


def payload_from_items(items: list[RegistryImportItem]) -> list[dict[str, Any]]:
    return [item.as_preoffer_dict() for item in items]


def items_from_payload(payload: object) -> list[RegistryImportItem]:
    if not isinstance(payload, list):
        raise ValueError("Нет данных для импорта — сначала выполните предпросмотр.")
    items: list[RegistryImportItem] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        oid = _clean(raw.get("offer_id"))
        if not oid.startswith("rts-pos-"):
            continue
        try:
            position_id = int(oid.removeprefix("rts-pos-"))
        except ValueError:
            continue
        items.append(
            RegistryImportItem(
                offer_id=oid,
                title=_clean(raw.get("title")),
                ktru_code=_clean(raw.get("ktru_code")),
                rts_url=_clean(raw.get("rts_url")),
                sku=_clean(raw.get("sku")),
                enabled=bool(raw.get("enabled")),
                characteristics=[
                    {"label": _clean(ch.get("label")), "value": _clean(ch.get("value"))}
                    for ch in (raw.get("characteristics") or [])
                    if isinstance(ch, dict)
                ],
                source_position_id=position_id,
                offer_status=_clean(raw.get("offer_status")) or None,
            )
        )
    if not items:
        raise ValueError("Нет данных для импорта — сначала выполните предпросмотр.")
    return items


def _draft_path(meta_dir: Path, token: str) -> Path:
    if not _DRAFT_TOKEN_RE.match(token):
        raise ValueError("Некорректный токен черновика импорта.")
    return meta_dir / "registry_import_drafts" / f"{token}.json"


def save_registry_import_draft(
    meta_dir: Path,
    *,
    preview: list[dict[str, Any]],
    payload: list[dict[str, Any]],
    filtered: list[str],
    skipped: list[str],
) -> str:
    token = secrets.token_urlsafe(16)
    draft_dir = meta_dir / "registry_import_drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "preview": preview,
        "payload": payload,
        "filtered": filtered,
        "skipped": skipped,
    }
    path = _draft_path(meta_dir, token)
    text = json.dumps(body, ensure_ascii=False)
    json.loads(text)
    path.write_text(text, encoding="utf-8")
    return token


def load_registry_import_draft(meta_dir: Path, token: str) -> dict[str, Any] | None:
    path = _draft_path(meta_dir, token)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def delete_registry_import_draft(meta_dir: Path, token: str) -> None:
    try:
        path = _draft_path(meta_dir, token)
    except ValueError:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

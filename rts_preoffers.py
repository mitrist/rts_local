"""Local RTS pre-offer catalog used for operator recommendations."""

from __future__ import annotations

import csv
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_WS_RE = re.compile(r"\s+")


def normalize_text(value: object) -> str:
    return _WS_RE.sub(" ", str(value or "").replace("\xa0", " ").strip()).lower()


def _clean(value: object) -> str:
    return str(value or "").replace("\xa0", " ").strip()


@dataclass(frozen=True, slots=True)
class RtsPreofferCharacteristic:
    match_key: str
    label: str
    value: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RtsPreofferCharacteristic":
        label = str(raw.get("label") or raw.get("display_label") or "").strip()
        mk = str(raw.get("match_key") or "").strip()
        if not mk and label:
            mk = f"name:{normalize_text(label)}"
        return cls(
            match_key=normalize_text(mk),
            label=label or "(без имени)",
            value=str(raw.get("value") or raw.get("display_value") or raw.get("value_display") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class RtsPreoffer:
    offer_id: str
    title: str
    rts_url: str
    sku: str
    ktru_code: str
    enabled: bool = True
    characteristics: list[RtsPreofferCharacteristic] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RtsPreoffer":
        chars_raw = raw.get("characteristics")
        chars = [
            RtsPreofferCharacteristic.from_dict(ch)
            for ch in (chars_raw if isinstance(chars_raw, list) else [])
            if isinstance(ch, dict)
        ]
        return cls(
            offer_id=str(raw.get("offer_id") or raw.get("id") or "").strip(),
            title=str(raw.get("title") or raw.get("name") or "").strip(),
            rts_url=str(raw.get("rts_url") or raw.get("url") or "").strip(),
            sku=str(raw.get("sku") or "").strip(),
            ktru_code=str(raw.get("ktru_code") or "").strip(),
            enabled=bool(raw.get("enabled", True)),
            characteristics=chars,
        )


def load_preoffers(path: Path) -> list[RtsPreoffer]:
    data = load_preoffers_raw(path)
    raw_offers = data.get("offers")
    if not isinstance(raw_offers, list):
        return []
    offers = [RtsPreoffer.from_dict(x) for x in raw_offers if isinstance(x, dict)]
    return [x for x in offers if x.enabled and x.offer_id and x.ktru_code]


def load_preoffers_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"offers": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"offers": []}
    if isinstance(data, list):
        return {"offers": data}
    if not isinstance(data, dict):
        return {"offers": []}
    if not isinstance(data.get("offers"), list):
        data["offers"] = []
    return data


def save_preoffers_raw(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(data.get("offers"), list):
        data = {**data, "offers": []}
    tmp = path.with_name(f"{path.name}.tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    json.loads(text)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def normalize_preoffer_payload(raw: dict[str, Any], *, existing_offer_id: str | None = None) -> dict[str, Any]:
    offer_id = _clean(raw.get("offer_id")) or _clean(existing_offer_id) or f"rts-{uuid.uuid4().hex[:12]}"
    ktru_code = _clean(raw.get("ktru_code"))
    title = _clean(raw.get("title") or raw.get("name"))
    chars_raw = raw.get("characteristics")
    chars: list[dict[str, str]] = []
    if isinstance(chars_raw, list):
        for ch in chars_raw:
            if not isinstance(ch, dict):
                continue
            label = _clean(ch.get("label") or ch.get("display_label"))
            value = _clean(ch.get("value") or ch.get("value_display") or ch.get("display_value"))
            if not label and not value:
                continue
            match_key = normalize_text(ch.get("match_key") or (f"name:{normalize_text(label)}" if label else ""))
            chars.append({"match_key": match_key, "label": label or match_key, "value": value})
    return {
        "offer_id": offer_id,
        "title": title,
        "rts_url": _clean(raw.get("rts_url") or raw.get("url")),
        "sku": _clean(raw.get("sku")),
        "ktru_code": ktru_code,
        "enabled": bool(raw.get("enabled", True)),
        "characteristics": chars,
    }


def preoffer_by_id(path: Path, offer_id: str) -> dict[str, Any] | None:
    oid = _clean(offer_id)
    for offer in load_preoffers_raw(path).get("offers", []):
        if isinstance(offer, dict) and _clean(offer.get("offer_id")) == oid:
            return normalize_preoffer_payload(offer, existing_offer_id=oid)
    return None


def upsert_preoffer(path: Path, raw_offer: dict[str, Any]) -> dict[str, Any]:
    data = load_preoffers_raw(path)
    offer = normalize_preoffer_payload(raw_offer)
    replaced = False
    out: list[dict[str, Any]] = []
    for item in data.get("offers", []):
        if not isinstance(item, dict):
            continue
        if _clean(item.get("offer_id")) == offer["offer_id"]:
            out.append(offer)
            replaced = True
        else:
            out.append(normalize_preoffer_payload(item, existing_offer_id=_clean(item.get("offer_id"))))
    if not replaced:
        out.append(offer)
    data["offers"] = out
    save_preoffers_raw(path, data)
    return offer


def disable_preoffer(path: Path, offer_id: str) -> bool:
    data = load_preoffers_raw(path)
    changed = False
    out: list[dict[str, Any]] = []
    oid = _clean(offer_id)
    for item in data.get("offers", []):
        if not isinstance(item, dict):
            continue
        offer = normalize_preoffer_payload(item, existing_offer_id=_clean(item.get("offer_id")))
        if offer["offer_id"] == oid:
            offer["enabled"] = False
            changed = True
        out.append(offer)
    if changed:
        data["offers"] = out
        save_preoffers_raw(path, data)
    return changed


def delete_preoffer(path: Path, offer_id: str) -> bool:
    """Remove a pre-offer from the catalog JSON (not only disable)."""
    data = load_preoffers_raw(path)
    oid = _clean(offer_id)
    if not oid:
        return False
    kept: list[dict[str, Any]] = []
    removed = False
    for item in data.get("offers", []):
        if not isinstance(item, dict):
            continue
        if _clean(item.get("offer_id")) == oid:
            removed = True
            continue
        kept.append(normalize_preoffer_payload(item, existing_offer_id=_clean(item.get("offer_id"))))
    if not removed:
        return False
    data["offers"] = kept
    save_preoffers_raw(path, data)
    return True


def preoffers_by_ktru(offers: list[RtsPreoffer], ktru_code: str) -> list[RtsPreoffer]:
    code = str(ktru_code or "").strip()
    return [x for x in offers if x.ktru_code == code]


def _offer_id_for_ktru(ktru_code: str) -> str:
    return f"rts-{ktru_code}"


def parse_ktru_csv_rows(path: Path) -> list[dict[str, str]]:
    """Строки CSV: КТРУ;Название предложения;SKU (разделитель ;)."""
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        if not reader.fieldnames:
            return rows
        fields = {_clean(h) for h in reader.fieldnames}
        ktru_key = next((h for h in reader.fieldnames if _clean(h) == "КТРУ"), None)
        title_key = next((h for h in reader.fieldnames if _clean(h) == "Название предложения"), None)
        sku_key = next((h for h in reader.fieldnames if _clean(h) == "SKU"), None)
        if not ktru_key or not title_key or not sku_key:
            raise ValueError(
                "Ожидаются колонки: КТРУ;Название предложения;SKU "
                f"(найдено: {', '.join(sorted(fields)) or '—'})."
            )
        for raw in reader:
            if not isinstance(raw, dict):
                continue
            ktru_code = _clean(raw.get(ktru_key))
            title = _clean(raw.get(title_key))
            sku = _clean(raw.get(sku_key))
            if not ktru_code:
                continue
            rows.append({"ktru_code": ktru_code, "title": title, "sku": sku or title})
    return rows


def import_preoffers_from_ktru_csv(
    path: Path,
    *,
    catalog_path: Path,
    skip_existing_ktru: bool = True,
    enabled: bool = True,
) -> dict[str, int]:
    """Добавляет позиции из ктру.csv в rts_preoffers.json."""
    parsed = parse_ktru_csv_rows(path)
    data = load_preoffers_raw(catalog_path)
    existing_ktru = {
        _clean(item.get("ktru_code"))
        for item in data.get("offers", [])
        if isinstance(item, dict) and _clean(item.get("ktru_code"))
    }
    seen_in_csv: set[str] = set()
    added = 0
    skipped_existing = 0
    skipped_duplicate_row = 0

    for row in parsed:
        ktru_code = row["ktru_code"]
        if ktru_code in seen_in_csv:
            skipped_duplicate_row += 1
            continue
        seen_in_csv.add(ktru_code)
        if skip_existing_ktru and ktru_code in existing_ktru:
            skipped_existing += 1
            continue
        offer = normalize_preoffer_payload(
            {
                "offer_id": _offer_id_for_ktru(ktru_code),
                "ktru_code": ktru_code,
                "title": row["title"] or ktru_code,
                "sku": row["sku"],
                "rts_url": "",
                "enabled": enabled,
                "characteristics": [],
            }
        )
        data.setdefault("offers", []).append(offer)
        existing_ktru.add(ktru_code)
        added += 1

    if added:
        save_preoffers_raw(catalog_path, data)
    return {
        "added": added,
        "skipped_existing": skipped_existing,
        "skipped_duplicate_row": skipped_duplicate_row,
        "total_in_csv": len(parsed),
    }

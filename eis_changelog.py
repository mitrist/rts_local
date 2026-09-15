"""
Журнал первых нахождений пар (закупка + код КТРУ) для тестового периода и метрик задержки.

Файл: RTS_META_DIR / eis_ktru_changelog.json (или meta_parent из EisPollConfig).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eis_ezt_archive import NoticeMatch


CHANGELOG_SCHEMA = 1

MSK = timezone(timedelta(hours=3), "MSK")


def _purchase_dedupe_key(m: NoticeMatch) -> str:
    pn = (m.purchase_number or "").strip()
    if pn:
        return pn
    return f"FILE:{m.source_file}:{','.join(m.ktru_codes)}"


def parse_publish_datetime_utc(raw: str) -> datetime | None:
    """Разбор строки из XML (publishDTInEIS / publishDTInETP) в UTC-aware."""
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    normalized = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    mf = re.search(r"\.\d+", normalized)
    if mf:
        normalized = normalized[: mf.start()] + normalized[mf.end() :].strip()
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def dt_to_msk_clean(dt: datetime) -> str:
    """Дата/время в Москве, до секунд, без суффикса часового пояса."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).replace(microsecond=0).strftime("%d.%m.%Y %H:%M:%S")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def load_changelog_raw(path: Path) -> tuple[dict[str, Any], set[str]]:
    if not path.is_file():
        return {"schema": CHANGELOG_SCHEMA, "seen_pair_keys": [], "entries": []}, set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"schema": CHANGELOG_SCHEMA, "seen_pair_keys": [], "entries": []}, set()
    if not isinstance(data, dict):
        return {"schema": CHANGELOG_SCHEMA, "seen_pair_keys": [], "entries": []}, set()
    seen_raw = data.get("seen_pair_keys")
    keys: set[str] = set(
        x for x in (seen_raw if isinstance(seen_raw, list) else []) if isinstance(x, str) and x.strip()
    )
    entries = data.get("entries")
    if entries is None:
        data["entries"] = []
    elif not isinstance(entries, list):
        data["entries"] = []
    data.setdefault("schema", CHANGELOG_SCHEMA)
    data["seen_pair_keys"] = sorted(keys)
    return data, keys


def _trim_entries_for_retention(
    data: dict[str, Any],
    *,
    retention_days: int,
    received_before: datetime,
) -> None:
    if retention_days <= 0:
        retention_days = 7
    cutoff = received_before - timedelta(days=retention_days)
    kept: list[dict[str, Any]] = []
    for e in data.get("entries", []):
        if not isinstance(e, dict):
            continue
        rau = str(e.get("received_at_utc") or "").strip()
        try:
            rdt = datetime.fromisoformat(rau.replace("Z", "+00:00"))
            if rdt.tzinfo is None:
                rdt = rdt.replace(tzinfo=timezone.utc)
            rdt = rdt.astimezone(timezone.utc)
        except ValueError:
            continue
        if rdt >= cutoff:
            kept.append(e)
    data["entries"] = kept


def append_eis_ktru_observations(
    path: Path,
    filtered_matches: list[NoticeMatch],
    received_at_utc: datetime,
    *,
    retention_days: int = 7,
) -> list[dict[str, Any]]:
    """
    Регистрирует впервые увиденные пары (ключ закупки | КТРУ).
    Возвращает список записей только что добавленных (для Telegram).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data, seen = load_changelog_raw(path)
    rat = received_at_utc.astimezone(timezone.utc).replace(microsecond=0)
    new_rows: list[dict[str, Any]] = []

    for m in filtered_matches:
        dk = _purchase_dedupe_key(m)
        pn_disp = ((m.purchase_number or "").strip() or dk)[:220]
        pub_raw = ((m.publish_dt_in_eis or "").strip() or (m.publish_dt_in_etp or "").strip())
        pub_src = "ЕИС" if (m.publish_dt_in_eis or "").strip() else ("ЭТП" if (m.publish_dt_in_etp or "").strip() else "")
        pub_dt_utc = parse_publish_datetime_utc((m.publish_dt_in_eis or "").strip()) or parse_publish_datetime_utc(
            (m.publish_dt_in_etp or "").strip()
        )
        for ktru in m.ktru_codes:
            kk = str(ktru).strip()
            if not kk:
                continue
            pair_key = f"{dk}|{kk}"
            if pair_key in seen:
                continue
            seen.add(pair_key)
            matched_cl = None
            customer_line_key = ""
            for cl in m.customer_ktru_lines:
                if cl.ktru_code.strip() == kk:
                    matched_cl = cl
                    customer_line_key = cl.line_key
                    break
            char_n = len(matched_cl.characteristics) if matched_cl else None
            notice_sum_txt = ((matched_cl.notice_total_display or "").strip() if matched_cl else "")
            lat: float | None = None
            if pub_dt_utc:
                lat = (rat - pub_dt_utc).total_seconds()
            row = {
                "pair_key": pair_key,
                "purchase_number": pn_disp,
                "ktru": kk,
                "customer_line_key": customer_line_key,
                "publish_source": pub_src or "—",
                "publish_raw": pub_raw,
                "publish_sort_key": (
                    pub_dt_utc.isoformat().replace("+00:00", "Z") if pub_dt_utc else ""
                ),
                "publish_display_msk": dt_to_msk_clean(pub_dt_utc) if pub_dt_utc else "—",
                "received_at_utc": rat.isoformat().replace("+00:00", "Z"),
                "received_display_msk": dt_to_msk_clean(rat),
                "latency_seconds": lat,
                "characteristics_param_count": char_n,
                "notice_total_display": notice_sum_txt,
            }
            data.setdefault("entries", []).append(row)
            new_rows.append(row)

    _trim_entries_for_retention(data, retention_days=retention_days, received_before=rat)
    data["seen_pair_keys"] = sorted(seen)
    tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp")
    try:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp.flush()
        tmp.close()
        os.replace(tmp.name, str(path))
    except OSError:
        try:
            Path(tmp.name).unlink(missing_ok=True)  # type: ignore[arg-type]
        except OSError:
            pass
        raise
    return new_rows


def read_changelog_entries(path: Path, *, max_rows: int = 2000) -> list[dict[str, Any]]:
    data, _ = load_changelog_raw(path)
    entries = [e for e in data.get("entries", []) if isinstance(e, dict)]
    entries.sort(key=lambda e: str(e.get("received_at_utc") or ""), reverse=True)
    return entries[:max_rows]


def entry_publish_sort_iso(e: dict[str, Any]) -> str:
    """ISO UTC для сортировки по дате публикации (лексикографически = хронологически)."""
    sk = str(e.get("publish_sort_key") or "").strip()
    if sk:
        return sk
    pr = str(e.get("publish_raw") or "").strip()
    if not pr:
        return ""
    dt = parse_publish_datetime_utc(pr)
    return dt.isoformat().replace("+00:00", "Z") if dt else ""


def read_changelog_rows_for_dashboard(path: Path, *, max_rows: int = 2000) -> list[dict[str, Any]]:
    """Строки журнала с заполненным publish_sort_key (в т.ч. для старых записей без поля в JSON)."""
    rows = read_changelog_entries(path, max_rows=max_rows)
    out: list[dict[str, Any]] = []
    for raw in rows:
        d = dict(raw)
        d["publish_sort_key"] = entry_publish_sort_iso(d)
        out.append(d)
    return out


def enrich_changelog_rows_from_customer_characteristics(
    rows: list[dict[str, Any]],
    last_eis: dict[str, Any] | None,
) -> None:
    """Дополняет поля Кол-во параметров / сумма извещения из last_eis_poll.customer_characteristics, если в журнале пусто."""
    if last_eis is None or not isinstance(last_eis, dict):
        return
    raw_lines = last_eis.get("customer_characteristics")
    if not isinstance(raw_lines, list):
        return
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    by_line_key: dict[str, dict[str, Any]] = {}
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        pn_item = str(item.get("purchase_number") or "").strip()
        kt = str(item.get("ktru_code") or "").strip()
        if pn_item and kt:
            by_pair[(pn_item, kt)] = item
        lk = str(item.get("line_key") or "").strip()
        if lk:
            by_line_key[lk] = item

    def _counts_from(hit: dict[str, Any]) -> tuple[int | None, str]:
        ch = hit.get("characteristics")
        n = len(ch) if isinstance(ch, list) else None
        nt = str(hit.get("notice_total_display") or "").strip()
        return n, nt

    for row in rows:
        if not isinstance(row, dict):
            continue
        lk_row = str(row.get("customer_line_key") or "").strip()
        hit: dict[str, Any] | None = None
        if lk_row and lk_row in by_line_key:
            hit = by_line_key[lk_row]
        else:
            pn_row = str(row.get("purchase_number") or "").strip()
            kk_row = str(row.get("ktru") or "").strip()
            if pn_row and kk_row:
                hit = by_pair.get((pn_row, kk_row))
        if hit is None:
            continue

        curr_n = row.get("characteristics_param_count")
        curr_sum = str(row.get("notice_total_display") or "").strip()
        n_snap, nt_snap = _counts_from(hit)
        if curr_n is None and n_snap is not None:
            row["characteristics_param_count"] = n_snap

        if not curr_sum and nt_snap:
            row["notice_total_display"] = nt_snap


def format_utc_iso_msk_clean(iso: str | None) -> str:
    if not iso or not str(iso).strip():
        return "—"
    s = str(iso).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt_to_msk_clean(dt)
    except ValueError:
        return s

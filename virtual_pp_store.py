"""Persistence for virtual PP set, RTS ЭЗТ notices, and match events."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eis_analytics_db import connect, _json_dumps, _pp_core_parse_json, _utc_iso
from eis_notice_html_fetch import normalize_eis_notice_link

CHAR_HIT = "hit"
CHAR_MISS = "miss"
CHAR_UNMAPPED = "unmapped_chars"
UNMAPPED_REGION = "unmapped"
_EFFECTIVE_CHAR_STATUS = (
    "CASE WHEN IFNULL(e.char_status_override, '') IN ('hit', 'miss') "
    "THEN e.char_status_override ELSE e.char_status END"
)

_VARIANT_COLS = (
    "ktru_code, variant_no, eis_hash, in_core, characteristics_json, "
    "chars_preview, map_status, map_reason, set_id, updated_at_utc"
)


def _now() -> str:
    return _utc_iso(datetime.now(timezone.utc))


def _variant_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["variant_no"] = int(data.get("variant_no") or 0)
    data["in_core"] = int(data.get("in_core") or 0)
    data["characteristics"] = _pp_core_parse_json(data.get("characteristics_json"), [])
    data["map_status"] = str(data.get("map_status") or "")
    data["map_reason"] = str(data.get("map_reason") or "")
    return data


def record_unknown_region(path: Path, region_raw: str) -> None:
    text = str(region_raw or "").strip()
    if not text:
        return
    now = _now()
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO region_alias_unknown(region_raw, n, last_seen_utc)
            VALUES (?, 1, ?)
            ON CONFLICT(region_raw) DO UPDATE SET
              n = n + 1,
              last_seen_utc = excluded.last_seen_utc
            """,
            (text, now),
        )
        conn.commit()


def list_unknown_regions(path: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    lim = max(1, min(int(limit), 500))
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT region_raw, n, last_seen_utc
            FROM region_alias_unknown
            ORDER BY n DESC, last_seen_utc DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    return [dict(row) for row in rows]


def replace_virtual_pp_set(
    path: Path,
    ktru_code: str,
    set_id: str,
    variants: list[dict[str, Any]],
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    sid = str(set_id or "").strip()
    if not code or not sid:
        raise ValueError("ktru_code и set_id обязательны.")
    now = _now()
    core_n = 0
    belt_n = 0
    with connect(path) as conn:
        conn.execute("DELETE FROM virtual_pp_variants WHERE ktru_code = ?", (code,))
        for row in variants:
            try:
                variant_no = int(row.get("variant_no") or 0)
            except (TypeError, ValueError):
                continue
            if variant_no < 1:
                continue
            in_core = 1 if int(row.get("in_core") or 0) else 0
            if in_core:
                core_n += 1
            else:
                belt_n += 1
            chars = row.get("characteristics")
            if not isinstance(chars, list):
                chars = _pp_core_parse_json(row.get("characteristics_json"), [])
            conn.execute(
                """
                INSERT INTO virtual_pp_variants(
                  ktru_code, variant_no, eis_hash, in_core, characteristics_json,
                  chars_preview, map_status, map_reason, set_id, updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    variant_no,
                    str(row.get("eis_hash") or "").strip(),
                    in_core,
                    _json_dumps(chars)[:32000],
                    str(row.get("chars_preview") or "")[:4000],
                    str(row.get("map_status") or "")[:50],
                    str(row.get("map_reason") or "")[:2000],
                    sid,
                    now,
                ),
            )
        conn.execute(
            """
            INSERT INTO virtual_pp_sets(set_id, ktru_code, created_at_utc, core_n, belt_n)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sid, code, now, core_n, belt_n),
        )
        conn.commit()
    return {"ok": True, "ktru_code": code, "set_id": sid, "core_n": core_n, "belt_n": belt_n}


def latest_virtual_set(path: Path, ktru_code: str) -> dict[str, Any] | None:
    code = str(ktru_code or "").strip()
    if not code or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT set_id, ktru_code, created_at_utc, core_n, belt_n
            FROM virtual_pp_sets
            WHERE ktru_code = ?
            ORDER BY created_at_utc DESC
            LIMIT 1
            """,
            (code,),
        ).fetchone()
    return dict(row) if row else None


def list_virtual_pp_ktru_codes(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ktru_code FROM virtual_pp_variants ORDER BY ktru_code"
        ).fetchall()
    return [str(row["ktru_code"]) for row in rows if str(row["ktru_code"] or "").strip()]


def ktru_html_display_name(path: Path, ktru_code: str) -> str:
    """Most frequent EIS HTML position name for a KTRU, if any."""
    code = str(ktru_code or "").strip()
    if not code or not path.is_file():
        return ""
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT position_display_name AS name, COUNT(*) AS n
            FROM notice_html_ktru_lines
            WHERE ktru_code = ?
              AND TRIM(IFNULL(position_display_name, '')) != ''
            GROUP BY position_display_name
            ORDER BY n DESC, position_display_name ASC
            LIMIT 1
            """,
            (code,),
        ).fetchone()
    return str((row["name"] if row else "") or "").strip()


def list_virtual_pp_variants(path: Path, ktru_code: str) -> list[dict[str, Any]]:
    code = str(ktru_code or "").strip()
    if not code or not path.is_file():
        return []
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT {_VARIANT_COLS}
            FROM virtual_pp_variants
            WHERE ktru_code = ?
            ORDER BY in_core DESC, variant_no ASC
            """,
            (code,),
        ).fetchall()
    return [_variant_from_row(row) for row in rows]


def upsert_virtual_pp_variant(
    path: Path,
    ktru_code: str,
    variant: dict[str, Any],
    *,
    set_id: str = "",
) -> dict[str, Any]:
    """Insert or reuse a virtual-set row by eis_hash. Does not wipe the set."""
    code = str(ktru_code or "").strip()
    if not code:
        raise ValueError("Укажите ktru_code.")
    eis_hash = str(variant.get("eis_hash") or "").strip()
    if not eis_hash:
        raise ValueError("Нет eis_hash варианта.")
    chars = variant.get("characteristics")
    if not isinstance(chars, list):
        chars = _pp_core_parse_json(variant.get("characteristics_json"), [])
    now = _now()
    with connect(path) as conn:
        existing = conn.execute(
            f"""
            SELECT {_VARIANT_COLS}
            FROM virtual_pp_variants
            WHERE ktru_code = ? AND eis_hash = ?
            LIMIT 1
            """,
            (code, eis_hash),
        ).fetchone()
        if existing:
            return _variant_from_row(existing)
        meta = conn.execute(
            """
            SELECT set_id, core_n, belt_n FROM virtual_pp_sets
            WHERE ktru_code = ?
            ORDER BY created_at_utc DESC LIMIT 1
            """,
            (code,),
        ).fetchone()
        sid = str(set_id or (meta["set_id"] if meta else "") or "").strip() or uuid.uuid4().hex[:16]
        max_row = conn.execute(
            "SELECT MAX(variant_no) AS m FROM virtual_pp_variants WHERE ktru_code = ?",
            (code,),
        ).fetchone()
        try:
            next_no = int(max_row["m"] or 0) + 1
        except (TypeError, ValueError):
            next_no = 1
        in_core = 1 if int(variant.get("in_core") or 0) else 0
        conn.execute(
            """
            INSERT INTO virtual_pp_variants(
              ktru_code, variant_no, eis_hash, in_core, characteristics_json,
              chars_preview, map_status, map_reason, set_id, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                next_no,
                eis_hash,
                in_core,
                _json_dumps(chars)[:32000],
                str(variant.get("chars_preview") or "")[:4000],
                str(variant.get("map_status") or "")[:50],
                str(variant.get("map_reason") or "")[:2000],
                sid,
                now,
            ),
        )
        if meta and str(meta["set_id"] or "") == sid:
            if in_core:
                conn.execute(
                    "UPDATE virtual_pp_sets SET core_n = core_n + 1 WHERE set_id = ?",
                    (sid,),
                )
            else:
                conn.execute(
                    "UPDATE virtual_pp_sets SET belt_n = belt_n + 1 WHERE set_id = ?",
                    (sid,),
                )
        else:
            conn.execute(
                """
                INSERT INTO virtual_pp_sets(set_id, ktru_code, created_at_utc, core_n, belt_n)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sid, code, now, 1 if in_core else 0, 0 if in_core else 1),
            )
        conn.commit()
        row = conn.execute(
            f"""
            SELECT {_VARIANT_COLS}
            FROM virtual_pp_variants
            WHERE ktru_code = ? AND variant_no = ?
            """,
            (code, next_no),
        ).fetchone()
    return _variant_from_row(row) if row else {
        "ktru_code": code,
        "variant_no": next_no,
        "eis_hash": eis_hash,
        "in_core": in_core,
        "characteristics": chars,
        "set_id": sid,
    }


def get_virtual_pp_variant(path: Path, ktru_code: str, variant_no: int) -> dict[str, Any] | None:
    try:
        no = int(variant_no)
    except (TypeError, ValueError):
        return None
    for row in list_virtual_pp_variants(path, ktru_code):
        if int(row.get("variant_no") or 0) == no:
            return row
    return None


def list_eis_monitor_ktru_lines(
    path: Path,
    ktru_code: str,
    *,
    days: int = 7,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """HTML-монитор ЕИС: позиции notice_html_ktru_lines за окно дат."""
    code = str(ktru_code or "").strip()
    window = max(1, min(int(days or 7), 90))
    lim = max(1, min(int(limit), 5000))
    if not code or not path.is_file():
        return []
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
              l.line_key,
              l.purchase_number,
              l.ktru_code,
              l.position_index,
              l.position_display_name,
              l.quantity_text,
              l.price_per_unit_text,
              l.line_sum_text,
              l.characteristics_json,
              p.region,
              p.etp_name,
              COALESCE(r.pub_date_utc, p.fetched_at_utc) AS observed_at_utc,
              r.price_text,
              r.customer_name
            FROM notice_html_ktru_lines l
            JOIN notice_html_pages p ON p.purchase_number = l.purchase_number AND p.ok = 1
            LEFT JOIN eis_rss_observations r ON r.reg_number = l.purchase_number
            WHERE l.ktru_code = ?
              AND datetime(COALESCE(r.pub_date_utc, p.fetched_at_utc))
                  >= datetime('now', '-' || ? || ' days')
            ORDER BY observed_at_utc DESC, l.purchase_number DESC, l.position_index ASC
            LIMIT ?
            """,
            (code, window, lim),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["characteristics"] = _pp_core_parse_json(data.get("characteristics_json"), [])
        data.pop("characteristics_json", None)
        out.append(data)
    return out


def upsert_rts_ezt_notice(path: Path, row: dict[str, Any]) -> None:
    number = str(row.get("notification_number") or "").strip()
    if not number:
        return
    now = str(row.get("fetched_at_utc") or "").strip() or _now()
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO rts_ezt_notices(
              notification_number, ktru_code, region_raw, subject_oktmo, nmck,
              url_trade_id, numeric_trade_id, lot_state, lot_state_label,
              participant_has_active_application, publication_date,
              publication_date_raw, trade_name, fetched_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notification_number) DO UPDATE SET
              ktru_code=excluded.ktru_code,
              region_raw=excluded.region_raw,
              subject_oktmo=excluded.subject_oktmo,
              nmck=excluded.nmck,
              url_trade_id=excluded.url_trade_id,
              numeric_trade_id=excluded.numeric_trade_id,
              lot_state=excluded.lot_state,
              lot_state_label=excluded.lot_state_label,
              participant_has_active_application=excluded.participant_has_active_application,
              publication_date=excluded.publication_date,
              publication_date_raw=excluded.publication_date_raw,
              trade_name=excluded.trade_name,
              fetched_at_utc=excluded.fetched_at_utc
            """,
            (
                number,
                str(row.get("ktru_code") or "").strip(),
                str(row.get("region_raw") or "").strip(),
                str(row.get("subject_oktmo") or "").strip(),
                row.get("nmck"),
                str(row.get("url_trade_id") or "").strip(),
                row.get("numeric_trade_id"),
                row.get("lot_state"),
                str(row.get("lot_state_label") or "").strip(),
                1 if row.get("participant_has_active_application") else 0,
                str(row.get("publication_date") or "").strip(),
                str(row.get("publication_date_raw") or "").strip(),
                str(row.get("trade_name") or "").strip(),
                now,
            ),
        )
        conn.commit()


def replace_rts_ezt_lot_items(
    path: Path,
    notification_number: str,
    items: list[dict[str, Any]],
) -> None:
    number = str(notification_number or "").strip()
    if not number:
        return
    with connect(path) as conn:
        conn.execute("DELETE FROM rts_ezt_lot_items WHERE notification_number = ?", (number,))
        for item in items:
            try:
                line_index = int(item.get("line_index") or 0)
            except (TypeError, ValueError):
                continue
            chars = item.get("characteristics")
            if not isinstance(chars, list):
                chars = _pp_core_parse_json(item.get("characteristics_json"), [])
            conn.execute(
                """
                INSERT INTO rts_ezt_lot_items(
                  notification_number, line_index, ktru_code, quantity, unit_price,
                  total, characteristics_json, ktru_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    number,
                    line_index,
                    str(item.get("ktru_code") or "").strip(),
                    item.get("quantity"),
                    item.get("unit_price"),
                    item.get("total"),
                    _json_dumps(chars)[:32000],
                    item.get("ktru_version"),
                ),
            )
        conn.commit()


def get_rts_ezt_lot_item(
    path: Path,
    notification_number: str,
    line_index: int,
) -> dict[str, Any] | None:
    number = str(notification_number or "").strip()
    if not number or not path.is_file():
        return None
    try:
        idx = int(line_index)
    except (TypeError, ValueError):
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT notification_number, line_index, ktru_code, quantity, unit_price,
                   total, characteristics_json, ktru_version
            FROM rts_ezt_lot_items
            WHERE notification_number = ? AND line_index = ?
            """,
            (number, idx),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["characteristics"] = _pp_core_parse_json(data.get("characteristics_json"), [])
    data.pop("characteristics_json", None)
    return data


def load_notice_characteristics(
    path: Path,
    *,
    notification_number: str,
    line_index: int,
    ktru_code: str = "",
) -> list[dict[str, Any]]:
    """Notice chars stored at poll time, else HTML-monitor line for the same извещение."""
    item = get_rts_ezt_lot_item(path, notification_number, line_index)
    chars = item.get("characteristics") if item else None
    if isinstance(chars, list) and chars:
        return [ch for ch in chars if isinstance(ch, dict)]
    number = str(notification_number or "").strip()
    code = str(ktru_code or "").strip()
    if not number or not path.is_file():
        return []
    try:
        idx = int(line_index)
    except (TypeError, ValueError):
        idx = 0
    with connect(path) as conn:
        if code:
            row = conn.execute(
                """
                SELECT characteristics_json FROM notice_html_ktru_lines
                WHERE purchase_number = ? AND ktru_code = ? AND position_index = ?
                LIMIT 1
                """,
                (number, code, idx),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT characteristics_json FROM notice_html_ktru_lines
                    WHERE purchase_number = ? AND ktru_code = ?
                    ORDER BY position_index ASC LIMIT 1
                    """,
                    (number, code),
                ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT characteristics_json FROM notice_html_ktru_lines
                WHERE purchase_number = ? AND position_index = ?
                LIMIT 1
                """,
                (number, idx),
            ).fetchone()
    if not row:
        return []
    parsed = _pp_core_parse_json(row["characteristics_json"], [])
    return [ch for ch in parsed if isinstance(ch, dict)]


def upsert_virtual_match_event(path: Path, event: dict[str, Any]) -> None:
    number = str(event.get("notification_number") or "").strip()
    try:
        line_index = int(event.get("line_index") or 0)
    except (TypeError, ValueError):
        return
    sid = str(event.get("set_id") or "").strip()
    if not number or not sid:
        return
    now = str(event.get("matched_at_utc") or "").strip() or _now()
    variant_no = event.get("variant_no")
    in_core = event.get("in_core")
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO virtual_match_events(
              notification_number, line_index, ktru_code, variant_no, in_core,
              char_status, subject_oktmo, region_raw, unit_price, quantity, nmck,
              own_application, qty_risk, offer_unit_price, set_id, matched_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notification_number, line_index, set_id) DO UPDATE SET
              ktru_code=excluded.ktru_code,
              variant_no=excluded.variant_no,
              in_core=excluded.in_core,
              char_status=excluded.char_status,
              subject_oktmo=excluded.subject_oktmo,
              region_raw=excluded.region_raw,
              unit_price=excluded.unit_price,
              quantity=excluded.quantity,
              nmck=excluded.nmck,
              own_application=excluded.own_application,
              qty_risk=excluded.qty_risk,
              offer_unit_price=excluded.offer_unit_price,
              matched_at_utc=excluded.matched_at_utc
            """,
            (
                number,
                line_index,
                str(event.get("ktru_code") or "").strip(),
                int(variant_no) if variant_no is not None else None,
                int(in_core) if in_core is not None else None,
                str(event.get("char_status") or CHAR_MISS),
                str(event.get("subject_oktmo") or "").strip(),
                str(event.get("region_raw") or "").strip(),
                event.get("unit_price"),
                event.get("quantity"),
                event.get("nmck"),
                1 if event.get("own_application") else 0,
                1 if event.get("qty_risk") else 0,
                event.get("offer_unit_price"),
                sid,
                now,
            ),
        )
        conn.commit()


def parse_winner_unit_price(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    if not text:
        return None
    value = float(text)
    if value < 0:
        raise ValueError("Цена победителя не может быть отрицательной.")
    return value


def save_virtual_match_winner_unit_price(
    path: Path,
    *,
    notification_number: str,
    line_index: int,
    ktru_code: str = "",
    winner_unit_price: float | None,
) -> int:
    """Store operator-entered winner unit price; rematch does not overwrite it."""
    number = str(notification_number or "").strip()
    code = str(ktru_code or "").strip()
    if not number or not path.is_file():
        return 0
    try:
        idx = int(line_index)
    except (TypeError, ValueError):
        return 0
    where = ["notification_number = ?", "line_index = ?"]
    args: list[Any] = [winner_unit_price, number, idx]
    if code:
        where.append("ktru_code = ?")
        args.append(code)
    with connect(path) as conn:
        cur = conn.execute(
            f"""
            UPDATE virtual_match_events
            SET winner_unit_price = ?
            WHERE {" AND ".join(where)}
            """,
            tuple(args),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def parse_char_status_override(raw: Any) -> str | None:
    """Empty/auto clears the override; hit/miss (or да/нет) stores a manual status."""
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("ё", "е")
    if text in ("", "auto", "авто", "none", "null"):
        return None
    if text in (CHAR_HIT, "да", "yes", "true", "1"):
        return CHAR_HIT
    if text in (CHAR_MISS, "нет", "no", "false", "0"):
        return CHAR_MISS
    raise ValueError("Статус должен быть да, нет или авто.")


def save_virtual_match_char_status_override(
    path: Path,
    *,
    notification_number: str,
    line_index: int,
    ktru_code: str = "",
    char_status_override: str | None,
) -> int:
    """Store operator match status; rematch does not overwrite it."""
    number = str(notification_number or "").strip()
    code = str(ktru_code or "").strip()
    if not number or not path.is_file():
        return 0
    try:
        idx = int(line_index)
    except (TypeError, ValueError):
        return 0
    where = ["notification_number = ?", "line_index = ?"]
    args: list[Any] = [char_status_override, number, idx]
    if code:
        where.append("ktru_code = ?")
        args.append(code)
    with connect(path) as conn:
        cur = conn.execute(
            f"""
            UPDATE virtual_match_events
            SET char_status_override = ?
            WHERE {" AND ".join(where)}
            """,
            tuple(args),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def get_virtual_match_event(
    path: Path,
    *,
    notification_number: str,
    line_index: int,
    ktru_code: str = "",
) -> dict[str, Any] | None:
    number = str(notification_number or "").strip()
    if not number or not path.is_file():
        return None
    try:
        idx = int(line_index)
    except (TypeError, ValueError):
        return None
    code = str(ktru_code or "").strip()
    with connect(path) as conn:
        if code:
            row = conn.execute(
                """
                SELECT
                  notification_number, line_index, ktru_code, variant_no, in_core,
                  char_status, char_status_override, subject_oktmo, region_raw, unit_price, quantity,
                  nmck, own_application, qty_risk, offer_unit_price, winner_unit_price,
                  set_id, matched_at_utc
                FROM virtual_match_events
                WHERE notification_number = ? AND line_index = ? AND ktru_code = ?
                ORDER BY matched_at_utc DESC
                LIMIT 1
                """,
                (number, idx, code),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT
                  notification_number, line_index, ktru_code, variant_no, in_core,
                  char_status, char_status_override, subject_oktmo, region_raw, unit_price, quantity,
                  nmck, own_application, qty_risk, offer_unit_price, winner_unit_price,
                  set_id, matched_at_utc
                FROM virtual_match_events
                WHERE notification_number = ? AND line_index = ?
                ORDER BY matched_at_utc DESC
                LIMIT 1
                """,
                (number, idx),
            ).fetchone()
    if not row:
        return None
    return _decorate_match_event(dict(row))


def virtual_match_stats(
    path: Path,
    ktru_code: str,
    *,
    days: int = 7,
    set_id: str = "",
) -> dict[str, Any]:
    code = str(ktru_code or "").strip()
    window = max(1, min(int(days or 7), 90))
    if not code or not path.is_file():
        return _empty_stats(code, window, set_id)
    sid = str(set_id or "").strip()
    with connect(path) as conn:
        if not sid:
            row = conn.execute(
                """
                SELECT set_id FROM virtual_pp_sets
                WHERE ktru_code = ?
                ORDER BY created_at_utc DESC LIMIT 1
                """,
                (code,),
            ).fetchone()
            sid = str(row["set_id"] or "") if row else ""
        params: list[Any] = [code, window]
        set_sql = ""
        if sid:
            set_sql = " AND e.set_id = ?"
            params.append(sid)
        summary = conn.execute(
            f"""
            SELECT
              COUNT(*) AS n,
              SUM(CASE WHEN {_EFFECTIVE_CHAR_STATUS} = 'hit' AND e.in_core = 1 THEN 1 ELSE 0 END) AS hit_core,
              SUM(CASE WHEN {_EFFECTIVE_CHAR_STATUS} = 'hit' AND IFNULL(e.in_core, 0) = 0 THEN 1 ELSE 0 END) AS hit_belt,
              SUM(CASE WHEN {_EFFECTIVE_CHAR_STATUS} = 'miss' THEN 1 ELSE 0 END) AS miss_chars,
              SUM(CASE WHEN {_EFFECTIVE_CHAR_STATUS} = 'unmapped_chars' THEN 1 ELSE 0 END) AS unmapped_chars,
              SUM(CASE WHEN e.subject_oktmo = 'unmapped' OR e.subject_oktmo = '' THEN 1 ELSE 0 END) AS unmapped_region,
              SUM(CASE WHEN e.own_application = 1 THEN 1 ELSE 0 END) AS own_application,
              SUM(CASE WHEN e.qty_risk = 1 THEN 1 ELSE 0 END) AS qty_risk
            FROM virtual_match_events e
            JOIN rts_ezt_notices n ON n.notification_number = e.notification_number
            WHERE e.ktru_code = ?
              AND date(n.publication_date) >= date('now', '-' || ? || ' days')
              {set_sql}
            """,
            params,
        ).fetchone()
        regions = conn.execute(
            f"""
            SELECT
              e.subject_oktmo AS subject_oktmo,
              e.region_raw AS region_raw,
              COUNT(*) AS n
            FROM virtual_match_events e
            JOIN rts_ezt_notices n ON n.notification_number = e.notification_number
            WHERE e.ktru_code = ?
              AND date(n.publication_date) >= date('now', '-' || ? || ' days')
              {set_sql}
            GROUP BY e.subject_oktmo, e.region_raw
            ORDER BY n DESC, e.subject_oktmo, e.region_raw
            LIMIT 80
            """,
            params,
        ).fetchall()
        prices = conn.execute(
            f"""
            SELECT e.variant_no, e.unit_price, e.quantity, e.in_core
            FROM virtual_match_events e
            JOIN rts_ezt_notices n ON n.notification_number = e.notification_number
            WHERE e.ktru_code = ?
              AND {_EFFECTIVE_CHAR_STATUS} = 'hit'
              AND e.unit_price IS NOT NULL
              AND date(n.publication_date) >= date('now', '-' || ? || ' days')
              {set_sql}
            ORDER BY e.variant_no, e.unit_price
            """,
            params,
        ).fetchall()
        notices_n = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM rts_ezt_notices
            WHERE ktru_code = ?
              AND date(publication_date) >= date('now', '-' || ? || ' days')
            """,
            (code, window),
        ).fetchone()
    total = int(summary["n"] or 0) if summary else 0
    region_rows = []
    for row in regions:
        n = int(row["n"] or 0)
        region_rows.append(
            {
                "subject_oktmo": str(row["subject_oktmo"] or ""),
                "region_raw": str(row["region_raw"] or ""),
                "n": n,
                "share": round(n / total, 4) if total else 0.0,
            }
        )
    by_variant: dict[int, dict[str, Any]] = {}
    for row in prices:
        try:
            no = int(row["variant_no"] or 0)
        except (TypeError, ValueError):
            continue
        bucket = by_variant.setdefault(
            no,
            {"variant_no": no, "in_core": int(row["in_core"] or 0), "prices": [], "qty": []},
        )
        if row["unit_price"] is not None:
            bucket["prices"].append(float(row["unit_price"]))
        if row["quantity"] is not None:
            bucket["qty"].append(float(row["quantity"]))
    return {
        "ktru_code": code,
        "days": window,
        "set_id": sid,
        "notices": int(notices_n["c"] or 0) if notices_n else 0,
        "events": total,
        "hit_core": int(summary["hit_core"] or 0) if summary else 0,
        "hit_belt": int(summary["hit_belt"] or 0) if summary else 0,
        "miss_chars": int(summary["miss_chars"] or 0) if summary else 0,
        "unmapped_chars": int(summary["unmapped_chars"] or 0) if summary else 0,
        "unmapped_region": int(summary["unmapped_region"] or 0) if summary else 0,
        "own_application": int(summary["own_application"] or 0) if summary else 0,
        "qty_risk": int(summary["qty_risk"] or 0) if summary else 0,
        "regions": region_rows,
        "variant_stats": [_summarize_variant(v) for v in by_variant.values()],
    }


def _event_line_sum(quantity: Any, unit_price: Any, nmck: Any) -> float | None:
    try:
        if quantity is not None and unit_price is not None:
            return round(float(quantity) * float(unit_price), 2)
    except (TypeError, ValueError):
        pass
    try:
        if nmck is not None:
            return round(float(nmck), 2)
    except (TypeError, ValueError):
        return None
    return None


def _decorate_match_event(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["variant_no"] = int(data["variant_no"]) if data.get("variant_no") is not None else None
    data["in_core"] = int(data["in_core"]) if data.get("in_core") is not None else None
    data["own_application"] = bool(data.get("own_application"))
    data["qty_risk"] = bool(data.get("qty_risk"))
    override = str(data.get("char_status_override") or "").strip()
    if override not in (CHAR_HIT, CHAR_MISS):
        override = ""
        data["char_status_override"] = None
    else:
        data["char_status_override"] = override
    effective = override or str(data.get("char_status") or "")
    data["char_status_effective"] = effective
    data["matched"] = effective == CHAR_HIT
    pn = str(data.get("notification_number") or "").strip()
    data["eis_url"] = normalize_eis_notice_link("", reg_number=pn) if pn else ""
    data["line_sum"] = _event_line_sum(data.get("quantity"), data.get("unit_price"), data.get("nmck"))
    return data


def _collapse_match_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("notification_number") or ""),
            str(row.get("ktru_code") or ""),
            row.get("unit_price"),
            row.get("quantity"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def list_virtual_match_events(
    path: Path,
    ktru_code: str = "",
    *,
    days: int = 7,
    set_id: str = "",
    limit: int = 500,
    matched: str = "all",
    collapse: bool = False,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    code = str(ktru_code or "").strip()
    window = max(1, min(int(days or 7), 90))
    lim = max(1, min(int(limit), 1000))
    sid = str(set_id or "").strip()
    want = str(matched or "all").strip().lower()
    where = ["date(IFNULL(n.publication_date, e.matched_at_utc)) >= date('now', '-' || ? || ' days')"]
    params: list[Any] = [window]
    if code:
        where.append("e.ktru_code = ?")
        params.append(code)
        if not sid:
            latest = latest_virtual_set(path, code)
            sid = str((latest or {}).get("set_id") or "")
    if sid:
        where.append("e.set_id = ?")
        params.append(sid)
    elif not code:
        where.append(
            """e.id IN (
              SELECT MAX(id) FROM virtual_match_events
              GROUP BY notification_number, ktru_code, line_index
            )"""
        )
    if want == "yes":
        where.append(f"{_EFFECTIVE_CHAR_STATUS} = 'hit'")
    elif want == "no":
        where.append(f"{_EFFECTIVE_CHAR_STATUS} <> 'hit'")
    fetch_lim = lim * 3 if collapse else lim
    params.append(fetch_lim)
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              e.notification_number, e.line_index, e.ktru_code, e.variant_no, e.in_core,
              e.char_status, e.char_status_override, e.subject_oktmo, e.region_raw, e.unit_price, e.quantity,
              e.nmck, e.own_application, e.qty_risk, e.offer_unit_price, e.winner_unit_price, e.set_id, e.matched_at_utc,
              n.trade_name, n.publication_date, n.lot_state_label
            FROM virtual_match_events e
            LEFT JOIN rts_ezt_notices n ON n.notification_number = e.notification_number
            WHERE {" AND ".join(where)}
            ORDER BY IFNULL(n.publication_date, e.matched_at_utc) DESC, e.notification_number DESC, e.line_index ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    out = [_decorate_match_event(dict(row)) for row in rows]
    if collapse:
        out = _collapse_match_events(out)
    return out[:lim]


def _summarize_variant(bucket: dict[str, Any]) -> dict[str, Any]:
    prices = sorted(float(x) for x in bucket.get("prices") or [])
    qty = sorted(float(x) for x in bucket.get("qty") or [])
    return {
        "variant_no": int(bucket.get("variant_no") or 0),
        "in_core": int(bucket.get("in_core") or 0),
        "n": len(prices),
        "price_min": prices[0] if prices else None,
        "price_max": prices[-1] if prices else None,
        "price_median": prices[len(prices) // 2] if prices else None,
        "prices": prices[:40],
        "qty_min": qty[0] if qty else None,
        "qty_max": qty[-1] if qty else None,
        "qty_median": qty[len(qty) // 2] if qty else None,
        "qty": qty[:40],
    }


def _empty_stats(code: str, days: int, set_id: str) -> dict[str, Any]:
    return {
        "ktru_code": code,
        "days": days,
        "set_id": set_id,
        "notices": 0,
        "events": 0,
        "hit_core": 0,
        "hit_belt": 0,
        "miss_chars": 0,
        "unmapped_chars": 0,
        "unmapped_region": 0,
        "own_application": 0,
        "qty_risk": 0,
        "regions": [],
        "variant_stats": [],
    }

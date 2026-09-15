"""SQLite analytics for EIS KTRU observations.

The module stores a durable stream of purchase/KTRU observations beside the
existing short-lived JSON state files. It is intentionally independent from
FastAPI so both CLI polls and the web scheduler can write the same database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from copy import deepcopy
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from eis_changelog import MSK, dt_to_msk_clean, parse_publish_datetime_utc
from eis_ezt_archive import NoticeMatch
from eis_history_config import HISTORY_DOCUMENT_TYPE_44
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine
from eis_notice_html_parse import parse_notice_common_info_context, parse_notice_common_info_html

ANALYTICS_SCHEMA = 20
DEFAULT_DB_NAME = "eis_analytics.sqlite3"
DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 30_000
RSS_TELEGRAM_LINE_KEY = "__rss__"
RSS_TELEGRAM_CHANNEL = "telegram_rss_cron"

_WS_RE = re.compile(r"\s+")


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm_text(value: object) -> str:
    return _WS_RE.sub(" ", str(value or "").strip()).lower()


def _norm_ktru_query(value: object) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-я]", "", str(value or "")).lower()


def _ktru_search_sql(column_sql: str = "l.ktru_code") -> str:
    normalized = (
        f"LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE({column_sql}, ''), '.', ''), '-', ''), "
        "'–', ''), '—', ''), ' ', ''))"
    )
    return f"(LOWER(COALESCE({column_sql}, '')) LIKE ? OR {normalized} LIKE ?)"


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def analytics_db_path(meta_root: Path, raw_name: str | None = None) -> Path:
    name = (raw_name or DEFAULT_DB_NAME).strip() or DEFAULT_DB_NAME
    p = Path(name)
    if p.is_absolute():
        return p
    return meta_root / p.name


def sqlite_busy_timeout_ms() -> int:
    raw = os.environ.get("EIS_SQLITE_BUSY_TIMEOUT_MS", "").strip()
    if not raw:
        return DEFAULT_SQLITE_BUSY_TIMEOUT_MS
    try:
        return max(0, min(int(raw), 300_000))
    except ValueError:
        return DEFAULT_SQLITE_BUSY_TIMEOUT_MS


def connect(path: Path, *, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    ms = sqlite_busy_timeout_ms() if busy_timeout_ms is None else max(0, int(busy_timeout_ms))
    timeout_sec = ms / 1000.0
    conn = sqlite3.connect(str(path), timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={ms}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS poll_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at_utc TEXT,
          finished_at_utc TEXT NOT NULL,
          org_region TEXT NOT NULL,
          document_type_44 TEXT NOT NULL,
          ktru_filter TEXT,
          exact_dates_json TEXT NOT NULL,
          zips_downloaded INTEGER NOT NULL DEFAULT 0,
          ok INTEGER NOT NULL DEFAULT 1,
          errors_json TEXT NOT NULL DEFAULT '[]',
          created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notice_events (
          purchase_key TEXT PRIMARY KEY,
          purchase_number TEXT,
          source_file TEXT,
          publish_dt_utc TEXT,
          publish_source TEXT,
          detected_at_utc TEXT NOT NULL,
          org_region TEXT NOT NULL,
          document_type_44 TEXT NOT NULL,
          purchase_method_codes_json TEXT NOT NULL DEFAULT '[]',
          shelf_confidence TEXT NOT NULL DEFAULT 'unknown',
          first_seen_at_utc TEXT NOT NULL,
          last_seen_at_utc TEXT NOT NULL,
          poll_run_id INTEGER,
          FOREIGN KEY(poll_run_id) REFERENCES poll_runs(id)
        );

        CREATE TABLE IF NOT EXISTS ktru_lines (
          line_key TEXT PRIMARY KEY,
          purchase_key TEXT NOT NULL,
          purchase_number TEXT,
          ktru_code TEXT NOT NULL,
          position_display_name TEXT,
          object_sid TEXT,
          object_external_sid TEXT,
          price_per_unit_text TEXT,
          line_sum_text TEXT,
          notice_total_text TEXT,
          customer_text TEXT,
          characteristics_count INTEGER NOT NULL DEFAULT 0,
          characteristics_hash TEXT NOT NULL,
          characteristics_json TEXT NOT NULL,
          first_seen_at_utc TEXT NOT NULL,
          last_seen_at_utc TEXT NOT NULL,
          poll_run_id INTEGER,
          FOREIGN KEY(purchase_key) REFERENCES notice_events(purchase_key),
          FOREIGN KEY(poll_run_id) REFERENCES poll_runs(id)
        );

        CREATE TABLE IF NOT EXISTS watchlist_ktru (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ktru_code_or_prefix TEXT NOT NULL UNIQUE,
          enabled INTEGER NOT NULL DEFAULT 1,
          reason TEXT,
          source TEXT,
          created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rss_poll_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at_utc TEXT NOT NULL,
          finished_at_utc TEXT NOT NULL,
          previous_success_finished_at_utc TEXT,
          query_label TEXT NOT NULL,
          rss_url TEXT NOT NULL,
          ok INTEGER NOT NULL DEFAULT 1,
          items_total INTEGER NOT NULL DEFAULT 0,
          items_new INTEGER NOT NULL DEFAULT 0,
          errors_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS eis_rss_observations (
          reg_number TEXT PRIMARY KEY,
          query_label TEXT NOT NULL,
          title TEXT,
          link TEXT,
          author TEXT,
          pub_date_utc TEXT,
          first_seen_at_utc TEXT NOT NULL,
          last_seen_at_utc TEXT NOT NULL,
          stage_text TEXT,
          customer_name TEXT,
          price_text TEXT,
          ikz TEXT,
          procedure_name TEXT,
          description_html TEXT,
          first_seen_poll_run_id INTEGER,
          first_seen_poll_finished_at_utc TEXT,
          detection_window_start_utc TEXT,
          detection_window_end_utc TEXT,
          FOREIGN KEY(first_seen_poll_run_id) REFERENCES rss_poll_runs(id)
        );

        CREATE TABLE IF NOT EXISTS notice_html_pages (
          purchase_number TEXT PRIMARY KEY,
          source_url TEXT,
          fetched_at_utc TEXT NOT NULL,
          ok INTEGER NOT NULL DEFAULT 0,
          http_status INTEGER,
          error_text TEXT,
          html_sha256 TEXT,
          html_text TEXT,
          contract_start_date TEXT,
          contract_execution_term TEXT,
          region TEXT,
          delivery_place TEXT,
          delivery_days TEXT
        );

        CREATE TABLE IF NOT EXISTS notice_html_ktru_lines (
          line_key TEXT PRIMARY KEY,
          purchase_number TEXT NOT NULL,
          ktru_code TEXT,
          position_display_name TEXT,
          unit_text TEXT,
          quantity_text TEXT,
          price_per_unit_text TEXT,
          line_sum_text TEXT,
          characteristics_count INTEGER NOT NULL DEFAULT 0,
          characteristics_json TEXT NOT NULL DEFAULT '[]',
          parsed_at_utc TEXT NOT NULL,
          FOREIGN KEY(purchase_number) REFERENCES notice_html_pages(purchase_number)
        );

        CREATE TABLE IF NOT EXISTS offer_watch_notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          purchase_number TEXT NOT NULL,
          line_key TEXT NOT NULL,
          ktru_code TEXT NOT NULL,
          offer_id TEXT,
          match_ratio REAL NOT NULL DEFAULT 0,
          matched INTEGER NOT NULL DEFAULT 0,
          required INTEGER NOT NULL DEFAULT 0,
          channel TEXT NOT NULL,
          sent_at_utc TEXT NOT NULL,
          status TEXT NOT NULL,
          error_text TEXT NOT NULL DEFAULT '',
          UNIQUE(purchase_number, line_key, channel)
        );

        CREATE TABLE IF NOT EXISTS rts_pp_drafts (
          purchase_number TEXT NOT NULL,
          line_key TEXT NOT NULL,
          rts_profile TEXT NOT NULL DEFAULT '',
          ktru_code TEXT NOT NULL,
          rts_draft_id INTEGER,
          protected_id TEXT NOT NULL DEFAULT '',
          edit_url TEXT NOT NULL DEFAULT '',
          offer_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'created',
          error_text TEXT NOT NULL DEFAULT '',
          created_at_utc TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT '',
          fill_report_json TEXT NOT NULL DEFAULT '',
          verify_json TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (purchase_number, line_key, rts_profile)
        );

        CREATE INDEX IF NOT EXISTS idx_notice_publish ON notice_events(publish_dt_utc);
        CREATE INDEX IF NOT EXISTS idx_notice_doc_publish ON notice_events(document_type_44, publish_dt_utc);
        CREATE INDEX IF NOT EXISTS idx_notice_region_publish ON notice_events(org_region, publish_dt_utc);
        CREATE INDEX IF NOT EXISTS idx_notice_confidence ON notice_events(shelf_confidence);
        CREATE INDEX IF NOT EXISTS idx_ktru_code ON ktru_lines(ktru_code);
        CREATE INDEX IF NOT EXISTS idx_ktru_hash ON ktru_lines(characteristics_hash);
        CREATE INDEX IF NOT EXISTS idx_ktru_code_hash ON ktru_lines(ktru_code, characteristics_hash);
        CREATE INDEX IF NOT EXISTS idx_ktru_purchase_key ON ktru_lines(purchase_key);
        CREATE INDEX IF NOT EXISTS idx_rss_pub_date ON eis_rss_observations(pub_date_utc);
        CREATE INDEX IF NOT EXISTS idx_rss_first_seen ON eis_rss_observations(first_seen_at_utc);
        CREATE INDEX IF NOT EXISTS idx_rss_query_label ON eis_rss_observations(query_label);
        CREATE INDEX IF NOT EXISTS idx_notice_html_status ON notice_html_pages(ok, fetched_at_utc);
        CREATE INDEX IF NOT EXISTS idx_notice_html_ktru_purchase ON notice_html_ktru_lines(purchase_number);
        CREATE INDEX IF NOT EXISTS idx_offer_watch_status ON offer_watch_notifications(status, sent_at_utc);
        CREATE INDEX IF NOT EXISTS idx_offer_watch_ktru ON offer_watch_notifications(ktru_code);
        CREATE INDEX IF NOT EXISTS idx_rts_pp_drafts_status ON rts_pp_drafts(status, created_at_utc);

        CREATE TABLE IF NOT EXISTS auto_participate_attempts (
          purchase_number TEXT NOT NULL,
          line_key TEXT NOT NULL,
          rts_profile TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL,
          ktru_code TEXT NOT NULL DEFAULT '',
          rts_draft_id INTEGER,
          edit_url TEXT NOT NULL DEFAULT '',
          error_text TEXT NOT NULL DEFAULT '',
          created_at_utc TEXT NOT NULL,
          PRIMARY KEY (purchase_number, line_key, rts_profile)
        );
        CREATE INDEX IF NOT EXISTS idx_auto_pp_attempt_profile ON auto_participate_attempts(rts_profile, created_at_utc);
        CREATE INDEX IF NOT EXISTS idx_auto_pp_attempt_status ON auto_participate_attempts(status, created_at_utc);

        CREATE TABLE IF NOT EXISTS rts_pp_monitor_notifications (
          offer_price_list_item_id TEXT NOT NULL,
          ktru_code TEXT NOT NULL,
          channel TEXT NOT NULL,
          link_date TEXT NOT NULL DEFAULT '',
          unit_price REAL,
          trade_mark TEXT NOT NULL DEFAULT '',
          card_url TEXT NOT NULL DEFAULT '',
          sent_at_utc TEXT NOT NULL,
          status TEXT NOT NULL,
          error_text TEXT NOT NULL DEFAULT '',
          UNIQUE(offer_price_list_item_id, channel)
        );
        CREATE INDEX IF NOT EXISTS idx_rts_pp_monitor_status ON rts_pp_monitor_notifications(status, sent_at_utc);
        CREATE INDEX IF NOT EXISTS idx_rts_pp_monitor_ktru ON rts_pp_monitor_notifications(ktru_code);

        CREATE TABLE IF NOT EXISTS rts_pp_monitor_findings (
          offer_price_list_item_id TEXT PRIMARY KEY,
          ktru_code TEXT NOT NULL DEFAULT '',
          trade_mark TEXT NOT NULL DEFAULT '',
          link_date TEXT NOT NULL DEFAULT '',
          unit_price REAL,
          card_url TEXT NOT NULL DEFAULT '',
          find_json TEXT NOT NULL DEFAULT '',
          fetched_at_utc TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'notify'
        );
        CREATE INDEX IF NOT EXISTS idx_rts_pp_monitor_findings_ktru ON rts_pp_monitor_findings(ktru_code, fetched_at_utc);

        CREATE TABLE IF NOT EXISTS rts_pp_monitor_drafts (
          offer_price_list_item_id TEXT NOT NULL,
          rts_profile TEXT NOT NULL DEFAULT '',
          ktru_code TEXT NOT NULL DEFAULT '',
          rts_draft_id INTEGER,
          protected_id TEXT NOT NULL DEFAULT '',
          edit_url TEXT NOT NULL DEFAULT '',
          offer_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'created',
          error_text TEXT NOT NULL DEFAULT '',
          created_at_utc TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT '',
          fill_report_json TEXT NOT NULL DEFAULT '',
          verify_json TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (offer_price_list_item_id, rts_profile)
        );
        CREATE INDEX IF NOT EXISTS idx_rts_pp_monitor_drafts_profile ON rts_pp_monitor_drafts(rts_profile, created_at_utc);

        CREATE TABLE IF NOT EXISTS rts_pp_monitor_registry (
          offer_price_list_item_id TEXT PRIMARY KEY,
          ktru_code TEXT NOT NULL,
          price_list_item_id INTEGER,
          name TEXT NOT NULL DEFAULT '',
          trade_mark TEXT NOT NULL DEFAULT '',
          link_date TEXT NOT NULL DEFAULT '',
          validity_date TEXT NOT NULL DEFAULT '',
          unit_price REAL,
          delivery_regions_count INTEGER,
          card_url TEXT NOT NULL DEFAULT '',
          first_seen_at_utc TEXT NOT NULL,
          last_seen_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pp_registry_ktru_link ON rts_pp_monitor_registry(ktru_code, link_date);

        CREATE TABLE IF NOT EXISTS pp_eis_pp_match (
          purchase_number TEXT NOT NULL,
          line_key TEXT NOT NULL,
          offer_price_list_item_id TEXT NOT NULL,
          ktru_code TEXT NOT NULL,
          stack_key TEXT NOT NULL DEFAULT '',
          matched_count INTEGER NOT NULL,
          total_compared INTEGER NOT NULL,
          notice_only_count INTEGER NOT NULL DEFAULT 0,
          pp_only_count INTEGER NOT NULL DEFAULT 0,
          diff_json TEXT NOT NULL DEFAULT '[]',
          notice_pub_date_utc TEXT NOT NULL DEFAULT '',
          pp_link_date TEXT NOT NULL DEFAULT '',
          computed_at_utc TEXT NOT NULL,
          analyst_status TEXT NOT NULL DEFAULT 'pending',
          analyst_note TEXT NOT NULL DEFAULT '',
          analyst_user TEXT NOT NULL DEFAULT '',
          analyst_at_utc TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (purchase_number, line_key, offer_price_list_item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_pp_eis_match_ktru_score
          ON pp_eis_pp_match(ktru_code, matched_count, total_compared);
        CREATE INDEX IF NOT EXISTS idx_pp_eis_match_status ON pp_eis_pp_match(analyst_status);
        CREATE INDEX IF NOT EXISTS idx_pp_eis_match_offer ON pp_eis_pp_match(offer_price_list_item_id);

        CREATE TABLE IF NOT EXISTS pp_eis_pp_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_type TEXT NOT NULL,
          occurred_at_utc TEXT NOT NULL,
          ktru_code TEXT NOT NULL DEFAULT '',
          payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_pp_eis_events_type ON pp_eis_pp_events(event_type, occurred_at_utc);

        CREATE TABLE IF NOT EXISTS notice_event_journal_pages (
          purchase_number TEXT PRIMARY KEY,
          source_url TEXT,
          fetched_at_utc TEXT NOT NULL,
          ok INTEGER NOT NULL DEFAULT 0,
          error_text TEXT,
          html_text TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_notice_event_journal_ok ON notice_event_journal_pages(ok, fetched_at_utc);

        CREATE TABLE IF NOT EXISTS notice_supplier_results_pages (
          purchase_number TEXT PRIMARY KEY,
          source_url TEXT,
          fetched_at_utc TEXT NOT NULL,
          ok INTEGER NOT NULL DEFAULT 0,
          error_text TEXT,
          html_text TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_notice_supplier_results_ok ON notice_supplier_results_pages(ok, fetched_at_utc);

        CREATE TABLE IF NOT EXISTS notice_eis_outcome (
          purchase_number TEXT NOT NULL,
          line_key TEXT NOT NULL,
          eis_outcome TEXT NOT NULL DEFAULT 'unknown',
          winner_participant_id TEXT NOT NULL DEFAULT '',
          winner_offer_total REAL,
          notice_quantity REAL,
          expected_pp_unit_price REAL,
          expected_pp_unit_price_text TEXT NOT NULL DEFAULT '',
          protocol_at_utc TEXT NOT NULL DEFAULT '',
          computed_at_utc TEXT NOT NULL,
          PRIMARY KEY (purchase_number, line_key)
        );
        CREATE INDEX IF NOT EXISTS idx_notice_eis_outcome_outcome ON notice_eis_outcome(eis_outcome, computed_at_utc);

        CREATE TABLE IF NOT EXISTS rts_pp_publish_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          rts_profile TEXT NOT NULL DEFAULT '',
          rts_draft_id INTEGER NOT NULL,
          source TEXT NOT NULL DEFAULT '',
          source_key TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'preparing',
          prepare_json TEXT NOT NULL DEFAULT '',
          sign_requests_json TEXT NOT NULL DEFAULT '',
          signatures_json TEXT NOT NULL DEFAULT '',
          error_text TEXT NOT NULL DEFAULT '',
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          published_at_utc TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_rts_pp_publish_jobs_draft
          ON rts_pp_publish_jobs(rts_draft_id, rts_profile, updated_at_utc);
        CREATE INDEX IF NOT EXISTS idx_rts_pp_publish_jobs_status
          ON rts_pp_publish_jobs(status, updated_at_utc);
        """
    )
    _ensure_column(conn, "rss_poll_runs", "previous_success_finished_at_utc", "TEXT")
    _ensure_column(conn, "eis_rss_observations", "first_seen_poll_run_id", "INTEGER")
    _ensure_column(conn, "eis_rss_observations", "first_seen_poll_finished_at_utc", "TEXT")
    _ensure_column(conn, "eis_rss_observations", "detection_window_start_utc", "TEXT")
    _ensure_column(conn, "eis_rss_observations", "detection_window_end_utc", "TEXT")
    _ensure_column(conn, "notice_html_pages", "contract_start_date", "TEXT")
    _ensure_column(conn, "notice_html_pages", "contract_execution_term", "TEXT")
    _ensure_column(conn, "notice_html_pages", "region", "TEXT")
    _ensure_column(conn, "notice_html_pages", "delivery_place", "TEXT")
    _ensure_column(conn, "notice_html_pages", "delivery_days", "TEXT")
    _ensure_column(conn, "rts_pp_drafts", "fill_report_json", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "rts_pp_drafts", "verify_json", "TEXT NOT NULL DEFAULT ''")
    _migrate_rts_pp_drafts_profile(conn)
    _ensure_column(conn, "rts_pp_monitor_findings", "characteristics_hash", "TEXT")
    _ensure_column(conn, "rts_pp_monitor_registry", "characteristics_hash", "TEXT")
    _ensure_column(conn, "pp_eis_pp_match", "is_exact_match", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "pp_eis_pp_match", "lag_minutes", "REAL")
    _ensure_column(conn, "pp_eis_pp_match", "match_origin", "TEXT NOT NULL DEFAULT 'live'")
    _ensure_column(conn, "pp_eis_pp_match", "scenario_tag", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "pp_eis_pp_match", "participation_intent", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "rts_pp_monitor_drafts", "draft_unit_price", "REAL")
    _ensure_column(conn, "rts_pp_monitor_drafts", "source_unit_price", "REAL")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pp_registry_stack
        ON rts_pp_monitor_registry(ktru_code, characteristics_hash)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pp_eis_match_exact_lag
        ON pp_eis_pp_match(is_exact_match, lag_minutes)
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)",
        (str(ANALYTICS_SCHEMA),),
    )
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    cols = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def _rts_pp_drafts_pk_includes_profile(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(rts_pp_drafts)").fetchall()
    if not rows:
        return True
    pk_cols = [str(row[1]) for row in rows if int(row[5] or 0) > 0]
    return "rts_profile" in pk_cols


def _migrate_rts_pp_drafts_profile(conn: sqlite3.Connection) -> None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rts_pp_drafts'"
    ).fetchone():
        return
    if _rts_pp_drafts_pk_includes_profile(conn):
        return
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(rts_pp_drafts)").fetchall()}
    if "rts_profile" not in cols:
        conn.execute("ALTER TABLE rts_pp_drafts ADD COLUMN rts_profile TEXT NOT NULL DEFAULT ''")
    conn.executescript(
        """
        CREATE TABLE rts_pp_drafts__profile_mig (
          purchase_number TEXT NOT NULL,
          line_key TEXT NOT NULL,
          rts_profile TEXT NOT NULL DEFAULT '',
          ktru_code TEXT NOT NULL,
          rts_draft_id INTEGER,
          protected_id TEXT NOT NULL DEFAULT '',
          edit_url TEXT NOT NULL DEFAULT '',
          offer_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'created',
          error_text TEXT NOT NULL DEFAULT '',
          created_at_utc TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT '',
          fill_report_json TEXT NOT NULL DEFAULT '',
          verify_json TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (purchase_number, line_key, rts_profile)
        );
        INSERT INTO rts_pp_drafts__profile_mig (
          purchase_number, line_key, rts_profile, ktru_code, rts_draft_id, protected_id,
          edit_url, offer_id, status, error_text, created_at_utc, created_by,
          fill_report_json, verify_json
        )
        SELECT purchase_number, line_key, COALESCE(rts_profile, ''), ktru_code, rts_draft_id,
               protected_id, edit_url, offer_id, status, error_text, created_at_utc, created_by,
               fill_report_json, COALESCE(verify_json, '')
        FROM rts_pp_drafts;
        DROP TABLE rts_pp_drafts;
        ALTER TABLE rts_pp_drafts__profile_mig RENAME TO rts_pp_drafts;
        CREATE INDEX IF NOT EXISTS idx_rts_pp_drafts_status ON rts_pp_drafts(status, created_at_utc);
        CREATE INDEX IF NOT EXISTS idx_rts_pp_drafts_profile ON rts_pp_drafts(rts_profile, created_at_utc);
        """
    )


def _characteristics_full_json(
    characteristics: Iterable[CustomerCharacteristic | dict[str, Any]],
) -> str:
    out: list[dict[str, Any]] = []
    for raw in characteristics:
        if isinstance(raw, CustomerCharacteristic):
            out.append(raw.as_dict())
        elif isinstance(raw, dict):
            out.append(dict(raw))
    return _json_dumps(out)


def characteristics_fingerprint(characteristics: Iterable[CustomerCharacteristic | dict[str, Any]]) -> tuple[str, str]:
    pairs: list[dict[str, str]] = []
    for raw in characteristics:
        if isinstance(raw, CustomerCharacteristic):
            characteristic_code = raw.characteristic_code
            display_label = raw.display_label
            match_key = raw.match_key
            value_display = raw.value_display
        elif isinstance(raw, dict):
            characteristic_code = raw.get("characteristic_code")
            display_label = str(raw.get("display_label") or "")
            match_key = str(raw.get("match_key") or "")
            value_display = str(raw.get("value_display") or "")
        else:
            continue
        key_raw = str(characteristic_code or "").strip() or display_label or match_key
        key = _norm_text(key_raw)
        value = _norm_text(value_display)
        if key or value:
            pairs.append({"key": key, "value": value})
    pairs.sort(key=lambda x: (x["key"], x["value"]))
    canonical = _json_dumps(pairs)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return digest, canonical


def classify_shelf_confidence(document_type_44: str, purchase_method_codes: Iterable[str]) -> str:
    doc = (document_type_44 or "").strip().lower()
    methods = {_norm_text(x) for x in purchase_method_codes if str(x or "").strip()}
    joined = " ".join(sorted(methods))
    if doc == "epnotificationezt2020":
        return "strong"
    if "ezt" in doc or "эзт" in joined or "ezt" in joined:
        return "medium"
    return "unknown"


def _purchase_key(m: NoticeMatch) -> str:
    pn = (m.purchase_number or "").strip()
    if pn:
        return pn
    return f"FILE:{m.source_file}:{','.join(m.ktru_codes)}"


def _publish_dt_and_source(m: NoticeMatch) -> tuple[str, str]:
    raw = (m.publish_dt_in_eis or "").strip()
    src = "ЕИС" if raw else ""
    if not raw:
        raw = (m.publish_dt_in_etp or "").strip()
        src = "ЭТП" if raw else ""
    dt = parse_publish_datetime_utc(raw)
    return (_utc_iso(dt) if dt else "", src or "—")


def _line_from_match(m: NoticeMatch) -> Iterable[KtruPurchaseLine]:
    if m.customer_ktru_lines:
        yield from m.customer_ktru_lines
        return
    pk = _purchase_key(m)
    for idx, code in enumerate(m.ktru_codes):
        code_s = str(code or "").strip()
        if not code_s:
            continue
        yield KtruPurchaseLine(
            line_key=f"{pk}|{code_s}|fallback:{idx}",
            purchase_number=(m.purchase_number or "").strip(),
            object_sid="",
            object_external_sid="",
            position_index=idx,
            ktru_code=code_s,
            characteristics=[],
        )


def record_eis_poll_run(
    path: Path,
    matches: list[NoticeMatch],
    *,
    finished_at_utc: datetime,
    org_region: str,
    document_type_44: str,
    ktru_filter: str | None,
    exact_dates: list[str],
    zips_downloaded: int,
    ok: bool,
    errors: list[str],
) -> int:
    finished_iso = _utc_iso(finished_at_utc)
    with connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO poll_runs(
              finished_at_utc, org_region, document_type_44, ktru_filter,
              exact_dates_json, zips_downloaded, ok, errors_json, created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                finished_iso,
                org_region,
                document_type_44,
                ktru_filter or "",
                _json_dumps(exact_dates),
                int(zips_downloaded),
                1 if ok else 0,
                _json_dumps(errors),
                finished_iso,
            ),
        )
        run_id = int(cur.lastrowid or 0)
        for m in matches:
            pk = _purchase_key(m)
            publish_iso, publish_src = _publish_dt_and_source(m)
            confidence = classify_shelf_confidence(document_type_44, m.purchase_method_codes)
            conn.execute(
                """
                INSERT INTO notice_events(
                  purchase_key, purchase_number, source_file, publish_dt_utc, publish_source,
                  detected_at_utc, org_region, document_type_44, purchase_method_codes_json,
                  shelf_confidence, first_seen_at_utc, last_seen_at_utc, poll_run_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(purchase_key) DO UPDATE SET
                  source_file=excluded.source_file,
                  publish_dt_utc=COALESCE(NULLIF(excluded.publish_dt_utc, ''), notice_events.publish_dt_utc),
                  publish_source=excluded.publish_source,
                  detected_at_utc=excluded.detected_at_utc,
                  org_region=excluded.org_region,
                  document_type_44=excluded.document_type_44,
                  purchase_method_codes_json=excluded.purchase_method_codes_json,
                  shelf_confidence=excluded.shelf_confidence,
                  last_seen_at_utc=excluded.last_seen_at_utc,
                  poll_run_id=excluded.poll_run_id
                """,
                (
                    pk,
                    (m.purchase_number or "").strip(),
                    m.source_file,
                    publish_iso,
                    publish_src,
                    finished_iso,
                    org_region,
                    document_type_44,
                    _json_dumps(sorted(set(m.purchase_method_codes))),
                    confidence,
                    finished_iso,
                    finished_iso,
                    run_id,
                ),
            )
            for line in _line_from_match(m):
                line_dict = line.as_dict()
                chars = line_dict.get("characteristics")
                chars_list = chars if isinstance(chars, list) else []
                ch_hash, _ch_canon = characteristics_fingerprint(chars_list)
                ch_json = _characteristics_full_json(chars_list)
                conn.execute(
                    """
                    INSERT INTO ktru_lines(
                      line_key, purchase_key, purchase_number, ktru_code, position_display_name,
                      object_sid, object_external_sid, price_per_unit_text, line_sum_text,
                      notice_total_text, customer_text, characteristics_count, characteristics_hash,
                      characteristics_json, first_seen_at_utc, last_seen_at_utc, poll_run_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(line_key) DO UPDATE SET
                      purchase_key=excluded.purchase_key,
                      purchase_number=excluded.purchase_number,
                      ktru_code=excluded.ktru_code,
                      position_display_name=excluded.position_display_name,
                      object_sid=excluded.object_sid,
                      object_external_sid=excluded.object_external_sid,
                      price_per_unit_text=excluded.price_per_unit_text,
                      line_sum_text=excluded.line_sum_text,
                      notice_total_text=excluded.notice_total_text,
                      customer_text=excluded.customer_text,
                      characteristics_count=excluded.characteristics_count,
                      characteristics_hash=excluded.characteristics_hash,
                      characteristics_json=excluded.characteristics_json,
                      last_seen_at_utc=excluded.last_seen_at_utc,
                      poll_run_id=excluded.poll_run_id
                    """,
                    (
                        line.line_key,
                        pk,
                        (line.purchase_number or m.purchase_number or "").strip(),
                        line.ktru_code,
                        line.position_display_name,
                        line.object_sid,
                        line.object_external_sid,
                        line.price_per_unit_display,
                        line.line_sum_display,
                        line.notice_total_display,
                        line.customer_organization_display,
                        len(chars_list),
                        ch_hash,
                        ch_json,
                        finished_iso,
                        finished_iso,
                        run_id,
                    ),
                )
        conn.commit()
        return run_id


def record_rss_poll_run(
    path: Path,
    items: list[dict[str, Any]],
    *,
    query_label: str,
    rss_url: str,
    started_at_utc: datetime,
    finished_at_utc: datetime,
    ok: bool,
    errors: list[str],
) -> tuple[int, int]:
    started_iso = _utc_iso(started_at_utc)
    finished_iso = _utc_iso(finished_at_utc)
    normalized: list[dict[str, str]] = []
    for raw in items:
        reg = str(raw.get("reg_number") or "").strip()
        if not reg:
            continue
        normalized.append(
            {
                "reg_number": reg,
                "title": str(raw.get("title") or "").strip(),
                "link": str(raw.get("link") or "").strip(),
                "author": str(raw.get("author") or "").strip(),
                "pub_date_utc": str(raw.get("pub_date_utc") or "").strip(),
                "stage_text": str(raw.get("stage_text") or "").strip(),
                "customer_name": str(raw.get("customer_name") or "").strip(),
                "price_text": str(raw.get("price_text") or "").strip(),
                "ikz": str(raw.get("ikz") or "").strip(),
                "procedure_name": str(raw.get("procedure_name") or "").strip(),
                "description_html": str(raw.get("description_html") or ""),
            }
        )

    with connect(path) as conn:
        prev_success_row = conn.execute(
            """
            SELECT finished_at_utc
            FROM rss_poll_runs
            WHERE query_label = ? AND ok = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (query_label,),
        ).fetchone()
        previous_success_finished = (
            str(prev_success_row["finished_at_utc"] or "") if prev_success_row is not None else ""
        )
        existing = {
            str(r["reg_number"])
            for r in conn.execute(
                "SELECT reg_number FROM eis_rss_observations WHERE reg_number IN (%s)"
                % ",".join("?" for _ in normalized),
                [x["reg_number"] for x in normalized],
            ).fetchall()
        } if normalized else set()
        new_count = sum(1 for x in normalized if x["reg_number"] not in existing)
        cur = conn.execute(
            """
            INSERT INTO rss_poll_runs(
              started_at_utc, finished_at_utc, previous_success_finished_at_utc, query_label, rss_url, ok,
              items_total, items_new, errors_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_iso,
                finished_iso,
                previous_success_finished,
                query_label,
                rss_url,
                1 if ok else 0,
                len(normalized),
                new_count,
                _json_dumps(errors),
            ),
        )
        run_id = int(cur.lastrowid or 0)
        for item in normalized:
            conn.execute(
                """
                INSERT INTO eis_rss_observations(
                  reg_number, query_label, title, link, author, pub_date_utc,
                  first_seen_at_utc, last_seen_at_utc, stage_text, customer_name,
                  price_text, ikz, procedure_name, description_html, first_seen_poll_run_id,
                  first_seen_poll_finished_at_utc, detection_window_start_utc, detection_window_end_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(reg_number) DO UPDATE SET
                  query_label=excluded.query_label,
                  title=excluded.title,
                  link=excluded.link,
                  author=excluded.author,
                  pub_date_utc=COALESCE(NULLIF(excluded.pub_date_utc, ''), eis_rss_observations.pub_date_utc),
                  last_seen_at_utc=excluded.last_seen_at_utc,
                  stage_text=excluded.stage_text,
                  customer_name=excluded.customer_name,
                  price_text=excluded.price_text,
                  ikz=excluded.ikz,
                  procedure_name=excluded.procedure_name,
                  description_html=excluded.description_html
                """,
                (
                    item["reg_number"],
                    query_label,
                    item["title"],
                    item["link"],
                    item["author"],
                    item["pub_date_utc"],
                    started_iso,
                    finished_iso,
                    item["stage_text"],
                    item["customer_name"],
                    item["price_text"],
                    item["ikz"],
                    item["procedure_name"],
                    item["description_html"],
                    run_id,
                    finished_iso,
                    previous_success_finished,
                    started_iso,
                ),
            )
        conn.commit()
        return run_id, new_count


def _notice_html_lines_from_text(
    html_text: str,
    *,
    purchase_number: str,
    source_url: str = "",
) -> list[KtruPurchaseLine]:
    if not html_text:
        return []
    return parse_notice_common_info_html(
        html_text,
        purchase_number=purchase_number,
        source_url=source_url,
    )


def _match_parsed_html_line(
    parsed: list[KtruPurchaseLine],
    *,
    line_key: str,
    ktru_code: str,
) -> KtruPurchaseLine | None:
    lk = str(line_key or "").strip()
    kc = str(ktru_code or "").strip()
    by_key = {p.line_key: p for p in parsed}
    if lk and lk in by_key:
        return by_key[lk]
    if kc:
        for line in parsed:
            if line.ktru_code == kc:
                return line
    return parsed[0] if len(parsed) == 1 else None


def _apply_fresh_html_characteristics(
    row: dict[str, Any],
    *,
    parse_cache: dict[str, list[KtruPurchaseLine]],
) -> None:
    if not int(row.get("html_ok") or 0):
        return
    html_text = str(row.get("html_text") or "")
    if not html_text:
        return
    pn = str(row.get("purchase_number") or "").strip()
    if not pn:
        return
    if pn not in parse_cache:
        parse_cache[pn] = _notice_html_lines_from_text(
            html_text,
            purchase_number=pn,
            source_url=str(row.get("source_url") or ""),
        )
    parsed = parse_cache[pn]
    pline = _match_parsed_html_line(
        parsed,
        line_key=str(row.get("line_key") or ""),
        ktru_code=str(row.get("ktru_code") or ""),
    )
    if pline is None:
        return
    row["characteristics"] = [c.as_dict() for c in pline.characteristics]
    row["characteristics_count"] = len(pline.characteristics)


def reparse_notice_html_ktru_lines(path: Path, purchase_number: str) -> int:
    """Re-parse stored notice HTML and refresh notice_html_ktru_lines rows."""
    pn = str(purchase_number or "").strip()
    if not pn or not path.is_file():
        return 0
    with connect(path) as conn:
        page = conn.execute(
            """
            SELECT source_url, fetched_at_utc, ok, html_text
            FROM notice_html_pages
            WHERE purchase_number = ?
            """,
            (pn,),
        ).fetchone()
        if page is None or not int(page["ok"] or 0):
            return 0
        html_text = str(page["html_text"] or "")
        if not html_text:
            return 0
        lines = _notice_html_lines_from_text(
            html_text,
            purchase_number=pn,
            source_url=str(page["source_url"] or ""),
        )
        parsed_at = str(page["fetched_at_utc"] or _utc_iso(datetime.now(timezone.utc)))
        conn.execute("DELETE FROM notice_html_ktru_lines WHERE purchase_number = ?", (pn,))
        for line in lines:
            chars_json = _json_dumps([c.as_dict() for c in line.characteristics])
            conn.execute(
                """
                INSERT INTO notice_html_ktru_lines(
                  line_key, purchase_number, ktru_code, position_display_name, unit_text,
                  quantity_text, price_per_unit_text, line_sum_text, characteristics_count,
                  characteristics_json, parsed_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    line.line_key,
                    pn,
                    line.ktru_code,
                    line.position_display_name,
                    line.unit_display,
                    line.quantity_display,
                    line.price_per_unit_display,
                    line.line_sum_display,
                    len(line.characteristics),
                    chars_json,
                    parsed_at,
                ),
            )
        conn.commit()
        return len(lines)


def record_notice_html_page(
    path: Path,
    *,
    purchase_number: str,
    source_url: str,
    html_text: str,
    lines: list[KtruPurchaseLine],
    fetched_at_utc: datetime,
    ok: bool,
    http_status: int | None = None,
    error_text: str = "",
    page_context: dict[str, str] | None = None,
) -> None:
    fetched_iso = _utc_iso(fetched_at_utc)
    digest = hashlib.sha256((html_text or "").encode("utf-8")).hexdigest() if html_text else ""
    context = page_context if page_context is not None else (
        parse_notice_common_info_context(html_text) if ok and html_text else {}
    )
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO notice_html_pages(
              purchase_number, source_url, fetched_at_utc, ok, http_status,
              error_text, html_sha256, html_text, contract_start_date,
              contract_execution_term, region, delivery_place, delivery_days
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(purchase_number) DO UPDATE SET
              source_url=excluded.source_url,
              fetched_at_utc=excluded.fetched_at_utc,
              ok=excluded.ok,
              http_status=excluded.http_status,
              error_text=excluded.error_text,
              html_sha256=excluded.html_sha256,
              html_text=excluded.html_text,
              contract_start_date=excluded.contract_start_date,
              contract_execution_term=excluded.contract_execution_term,
              region=excluded.region,
              delivery_place=excluded.delivery_place,
              delivery_days=excluded.delivery_days
            """,
            (
                purchase_number,
                source_url,
                fetched_iso,
                1 if ok else 0,
                http_status,
                error_text,
                digest,
                html_text,
                str(context.get("contract_start_date") or ""),
                str(context.get("contract_execution_term") or ""),
                str(context.get("region") or ""),
                str(context.get("delivery_place") or ""),
                str(context.get("delivery_days") or ""),
            ),
        )
        conn.execute("DELETE FROM notice_html_ktru_lines WHERE purchase_number = ?", (purchase_number,))
        if ok:
            for line in lines:
                chars_json = _json_dumps([c.as_dict() for c in line.characteristics])
                conn.execute(
                    """
                    INSERT INTO notice_html_ktru_lines(
                      line_key, purchase_number, ktru_code, position_display_name, unit_text,
                      quantity_text, price_per_unit_text, line_sum_text, characteristics_count,
                      characteristics_json, parsed_at_utc
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        line.line_key,
                        purchase_number,
                        line.ktru_code,
                        line.position_display_name,
                        line.unit_display,
                        line.quantity_display,
                        line.price_per_unit_display,
                        line.line_sum_display,
                        len(line.characteristics),
                        chars_json,
                        fetched_iso,
                    ),
                )
        conn.commit()
    if ok and lines:
        try:
            from eis_pp_link_service import maybe_recompute_after_notice_html

            meta_dir = Path(os.environ.get("RTS_META_DIR", "/var/lib/rts_tender"))
            hist_raw = os.environ.get("EIS_HISTORY_DB", "").strip()
            history_path = Path(hist_raw) if hist_raw else None
            pub_date_utc = ""
            with connect(path) as conn:
                row = conn.execute(
                    "SELECT pub_date_utc FROM eis_rss_observations WHERE reg_number = ?",
                    (purchase_number,),
                ).fetchone()
                if row:
                    pub_date_utc = str(row[0] or "")
            maybe_recompute_after_notice_html(
                path,
                meta_dir=meta_dir,
                purchase_number=purchase_number,
                lines=lines,
                history_path=history_path if history_path and history_path.is_file() else None,
                pub_date_utc=pub_date_utc,
            )
        except Exception:
            pass


def record_notice_event_journal_page(
    path: Path,
    *,
    purchase_number: str,
    source_url: str,
    html_text: str,
    ok: bool = True,
    error_text: str = "",
) -> None:
    pn = str(purchase_number or "").strip()
    if not pn:
        raise ValueError("purchase_number required")
    fetched_iso = _utc_iso(datetime.now(timezone.utc))
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO notice_event_journal_pages(
              purchase_number, source_url, fetched_at_utc, ok, error_text, html_text
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(purchase_number) DO UPDATE SET
              source_url=excluded.source_url,
              fetched_at_utc=excluded.fetched_at_utc,
              ok=excluded.ok,
              error_text=excluded.error_text,
              html_text=excluded.html_text
            """,
            (
                pn,
                str(source_url or "")[:500],
                fetched_iso,
                1 if ok else 0,
                str(error_text or "")[:2000],
                html_text if ok else "",
            ),
        )
        conn.commit()


def get_notice_event_journal_fetched_at(path: Path, *, purchase_number: str) -> str | None:
    pn = str(purchase_number or "").strip()
    if not pn or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT fetched_at_utc
            FROM notice_event_journal_pages
            WHERE purchase_number = ? AND ok = 1
            """,
            (pn,),
        ).fetchone()
    if not row or not row[0]:
        return None
    return str(row[0])


def get_or_fetch_notice_event_journal(
    path: Path,
    *,
    purchase_number: str,
    source_url: str,
    timeout: float = 30.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    pn = str(purchase_number or "").strip()
    if not pn:
        return {"ok": False, "error_text": "purchase_number required", "html_text": ""}
    if path.is_file() and not force_refresh:
        with connect(path) as conn:
            row = conn.execute(
                """
                SELECT purchase_number, source_url, fetched_at_utc, ok, error_text, html_text
                FROM notice_event_journal_pages
                WHERE purchase_number = ? AND ok = 1 AND TRIM(COALESCE(html_text, '')) != ''
                """,
                (pn,),
            ).fetchone()
        if row:
            return dict(row)
    from eis_notice_event_journal_fetch import fetch_notice_event_journal_html

    url = str(source_url or "").strip()
    try:
        html, status = fetch_notice_event_journal_html(url, timeout=timeout)
        record_notice_event_journal_page(
            path,
            purchase_number=pn,
            source_url=url,
            html_text=html,
            ok=True,
        )
        return {
            "purchase_number": pn,
            "source_url": url,
            "ok": True,
            "http_status": status,
            "html_text": html,
            "error_text": "",
        }
    except Exception as exc:
        record_notice_event_journal_page(
            path,
            purchase_number=pn,
            source_url=url,
            html_text="",
            ok=False,
            error_text=str(exc),
        )
        return {
            "purchase_number": pn,
            "source_url": url,
            "ok": False,
            "html_text": "",
            "error_text": str(exc),
        }


def record_notice_supplier_results_page(
    path: Path,
    *,
    purchase_number: str,
    source_url: str,
    html_text: str,
    ok: bool = True,
    error_text: str = "",
) -> None:
    pn = str(purchase_number or "").strip()
    if not pn:
        raise ValueError("purchase_number required")
    fetched_iso = _utc_iso(datetime.now(timezone.utc))
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO notice_supplier_results_pages(
              purchase_number, source_url, fetched_at_utc, ok, error_text, html_text
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(purchase_number) DO UPDATE SET
              source_url=excluded.source_url,
              fetched_at_utc=excluded.fetched_at_utc,
              ok=excluded.ok,
              error_text=excluded.error_text,
              html_text=excluded.html_text
            """,
            (
                pn,
                str(source_url or "")[:500],
                fetched_iso,
                1 if ok else 0,
                str(error_text or "")[:2000],
                html_text if ok else "",
            ),
        )
        conn.commit()


def get_or_fetch_notice_supplier_results(
    path: Path,
    *,
    purchase_number: str,
    source_url: str,
    timeout: float = 30.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    pn = str(purchase_number or "").strip()
    if not pn:
        return {"ok": False, "error_text": "purchase_number required", "html_text": ""}
    if path.is_file() and not force_refresh:
        with connect(path) as conn:
            row = conn.execute(
                """
                SELECT purchase_number, source_url, fetched_at_utc, ok, error_text, html_text
                FROM notice_supplier_results_pages
                WHERE purchase_number = ? AND ok = 1 AND TRIM(COALESCE(html_text, '')) != ''
                """,
                (pn,),
            ).fetchone()
        if row:
            return dict(row)
    from eis_supplier_results_fetch import fetch_notice_supplier_results_html

    url = str(source_url or "").strip()
    try:
        html, status = fetch_notice_supplier_results_html(url, timeout=timeout)
        record_notice_supplier_results_page(
            path,
            purchase_number=pn,
            source_url=url,
            html_text=html,
            ok=True,
        )
        return {
            "purchase_number": pn,
            "source_url": url,
            "ok": True,
            "http_status": status,
            "html_text": html,
            "error_text": "",
        }
    except Exception as exc:
        record_notice_supplier_results_page(
            path,
            purchase_number=pn,
            source_url=url,
            html_text="",
            ok=False,
            error_text=str(exc),
        )
        return {
            "purchase_number": pn,
            "source_url": url,
            "ok": False,
            "html_text": "",
            "error_text": str(exc),
        }


def upsert_notice_eis_outcome(
    path: Path,
    *,
    purchase_number: str,
    line_key: str,
    eis_outcome: str,
    winner_participant_id: str = "",
    winner_offer_total: float | None = None,
    notice_quantity: float | None = None,
    expected_pp_unit_price: float | None = None,
    expected_pp_unit_price_text: str = "",
    protocol_at_utc: str = "",
) -> None:
    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    if not pn or not lk:
        raise ValueError("purchase_number and line_key required")
    computed_iso = _utc_iso(datetime.now(timezone.utc))
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO notice_eis_outcome(
              purchase_number, line_key, eis_outcome, winner_participant_id,
              winner_offer_total, notice_quantity, expected_pp_unit_price,
              expected_pp_unit_price_text, protocol_at_utc, computed_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(purchase_number, line_key) DO UPDATE SET
              eis_outcome=excluded.eis_outcome,
              winner_participant_id=excluded.winner_participant_id,
              winner_offer_total=excluded.winner_offer_total,
              notice_quantity=excluded.notice_quantity,
              expected_pp_unit_price=excluded.expected_pp_unit_price,
              expected_pp_unit_price_text=excluded.expected_pp_unit_price_text,
              protocol_at_utc=excluded.protocol_at_utc,
              computed_at_utc=excluded.computed_at_utc
            """,
            (
                pn,
                lk,
                str(eis_outcome or "unknown")[:50],
                str(winner_participant_id or "")[:50],
                winner_offer_total,
                notice_quantity,
                expected_pp_unit_price,
                str(expected_pp_unit_price_text or "")[:50],
                str(protocol_at_utc or "")[:50],
                computed_iso,
            ),
        )
        conn.commit()


def get_notice_eis_outcome(
    path: Path,
    *,
    purchase_number: str,
    line_key: str,
) -> dict[str, Any] | None:
    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    if not pn or not lk or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT purchase_number, line_key, eis_outcome, winner_participant_id,
                   winner_offer_total, notice_quantity, expected_pp_unit_price,
                   expected_pp_unit_price_text, protocol_at_utc, computed_at_utc
            FROM notice_eis_outcome
            WHERE purchase_number = ? AND line_key = ?
            """,
            (pn, lk),
        ).fetchone()
    return dict(row) if row else None


def count_pilot_lines_for_notice(
    path: Path,
    *,
    purchase_number: str,
    pilot_ktru_codes: frozenset[str],
) -> int:
    pn = str(purchase_number or "").strip()
    if not pn or not pilot_ktru_codes or not path.is_file():
        return 0
    placeholders = ",".join("?" * len(pilot_ktru_codes))
    params: list[Any] = [pn, *sorted(pilot_ktru_codes)]
    with connect(path) as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt
            FROM notice_html_ktru_lines
            WHERE purchase_number = ? AND ktru_code IN ({placeholders})
            """,
            params,
        ).fetchone()
    return int(row["cnt"]) if row else 0


def notice_html_payload(path: Path, purchase_number: str) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "not_loaded", "purchase_number": purchase_number, "lines": []}
    with connect(path) as conn:
        page = conn.execute(
            """
            SELECT purchase_number, source_url, fetched_at_utc, ok, http_status,
                   error_text, html_sha256, contract_start_date, contract_execution_term,
                   region, delivery_place, delivery_days, html_text
            FROM notice_html_pages
            WHERE purchase_number = ?
            """,
            (purchase_number,),
        ).fetchone()
        if page is None:
            return {"status": "not_loaded", "purchase_number": purchase_number, "lines": []}
        rows = conn.execute(
            """
            SELECT line_key, purchase_number, ktru_code, position_display_name, unit_text,
                   quantity_text, price_per_unit_text, line_sum_text, characteristics_count,
                   characteristics_json, parsed_at_utc
            FROM notice_html_ktru_lines
            WHERE purchase_number = ?
            ORDER BY line_key ASC
            """,
            (purchase_number,),
        ).fetchall()
    page_d = dict(page)
    context_keys = ("contract_start_date", "contract_execution_term", "region", "delivery_place", "delivery_days")
    if any(not page_d.get(k) for k in context_keys):
        context = parse_notice_common_info_context(str(page_d.get("html_text") or ""))
        for key in context_keys:
            if not page_d.get(key):
                page_d[key] = context.get(key, "")
    page_d.pop("html_text", None)
    lines_out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        try:
            d["characteristics"] = json.loads(str(d.get("characteristics_json") or "[]"))
        except json.JSONDecodeError:
            d["characteristics"] = []
        d.pop("characteristics_json", None)
        lines_out.append(d)
    page_d["status"] = "loaded" if int(page_d.get("ok") or 0) else "error"
    page_d["fetched_display_msk"] = _display_msk(page_d.get("fetched_at_utc"))
    page_d["lines"] = lines_out
    return page_d


def offer_watch_candidates(
    path: Path,
    *,
    limit: int = 100,
    channel: str = "telegram_preoffer_watch",
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
              l.line_key,
              l.purchase_number,
              l.ktru_code,
              l.position_display_name,
              l.price_per_unit_text,
              l.quantity_text,
              l.line_sum_text,
              l.characteristics_count,
              l.characteristics_json,
              l.parsed_at_utc,
              p.source_url,
              p.fetched_at_utc,
              p.region,
              p.delivery_place,
              p.delivery_days,
              p.contract_execution_term,
              r.pub_date_utc,
              r.first_seen_at_utc,
              r.customer_name,
              r.price_text,
              r.stage_text
            FROM notice_html_ktru_lines l
            JOIN notice_html_pages p ON p.purchase_number = l.purchase_number AND p.ok = 1
            LEFT JOIN eis_rss_observations r ON r.reg_number = l.purchase_number
            WHERE COALESCE(l.ktru_code, '') <> ''
              AND NOT EXISTS (
                SELECT 1
                FROM offer_watch_notifications n
                WHERE n.purchase_number = l.purchase_number
                  AND n.line_key = l.line_key
                  AND n.channel = ?
                  AND n.status = 'sent'
              )
            ORDER BY COALESCE(r.pub_date_utc, p.fetched_at_utc) DESC, l.purchase_number DESC, l.line_key ASC
            LIMIT ?
            """,
            (channel, max(1, int(limit))),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        try:
            d["characteristics"] = json.loads(str(d.get("characteristics_json") or "[]"))
        except json.JSONDecodeError:
            d["characteristics"] = []
        d.pop("characteristics_json", None)
        d["pub_display_msk"] = _display_msk(d.get("pub_date_utc"))
        d["first_seen_display_msk"] = _display_msk(d.get("first_seen_at_utc"))
        d["fetched_display_msk"] = _display_msk(d.get("fetched_at_utc"))
        out.append(d)
    return out


def rss_poll_telegram_candidates(
    path: Path,
    run_id: int,
    *,
    limit: int = 100,
    channel: str = RSS_TELEGRAM_CHANNEL,
    line_key: str = RSS_TELEGRAM_LINE_KEY,
) -> list[dict[str, Any]]:
    """New RSS observations from a poll run that have not yet been notified."""
    if not path.is_file() or run_id <= 0:
        return []
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
              o.reg_number,
              o.title,
              o.link,
              o.customer_name,
              o.price_text,
              o.stage_text,
              o.procedure_name,
              o.pub_date_utc,
              o.first_seen_at_utc,
              o.ikz
            FROM eis_rss_observations o
            WHERE o.first_seen_poll_run_id = ?
              AND NOT EXISTS (
                SELECT 1
                FROM offer_watch_notifications n
                WHERE n.purchase_number = o.reg_number
                  AND n.line_key = ?
                  AND n.channel = ?
                  AND n.status = 'sent'
              )
            ORDER BY COALESCE(o.pub_date_utc, o.first_seen_at_utc) DESC, o.reg_number DESC
            LIMIT ?
            """,
            (run_id, line_key, channel, max(1, int(limit))),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["purchase_number"] = d.pop("reg_number")
        d["pub_display_msk"] = _display_msk(d.get("pub_date_utc"))
        d["first_seen_display_msk"] = _display_msk(d.get("first_seen_at_utc"))
        out.append(d)
    return out


def record_offer_watch_notification(
    path: Path,
    *,
    purchase_number: str,
    line_key: str,
    ktru_code: str,
    offer_id: str,
    match_ratio: float,
    matched: int,
    required: int,
    channel: str,
    status: str,
    error_text: str = "",
    sent_at_utc: datetime | None = None,
) -> None:
    sent_iso = _utc_iso(sent_at_utc or datetime.now(timezone.utc))
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO offer_watch_notifications(
              purchase_number, line_key, ktru_code, offer_id, match_ratio,
              matched, required, channel, sent_at_utc, status, error_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(purchase_number, line_key, channel) DO UPDATE SET
              ktru_code=excluded.ktru_code,
              offer_id=excluded.offer_id,
              match_ratio=excluded.match_ratio,
              matched=excluded.matched,
              required=excluded.required,
              sent_at_utc=excluded.sent_at_utc,
              status=excluded.status,
              error_text=excluded.error_text
            """,
            (
                purchase_number,
                line_key,
                ktru_code,
                offer_id,
                float(match_ratio),
                int(matched),
                int(required),
                channel,
                sent_iso,
                status,
                error_text[:2000],
            ),
        )
        conn.commit()


def list_known_pp_monitor_ids(path: Path, *, ktru_code: str | None = None) -> set[str]:
    if not path.is_file():
        return set()
    ktru = str(ktru_code or "").strip()
    with connect(path) as conn:
        if ktru:
            rows = conn.execute(
                """
                SELECT DISTINCT offer_price_list_item_id
                FROM rts_pp_monitor_notifications
                WHERE ktru_code = ?
                """,
                (ktru,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT offer_price_list_item_id FROM rts_pp_monitor_notifications"
            ).fetchall()
    return {str(row[0]).strip() for row in rows if str(row[0] or "").strip()}


def record_pp_monitor_notification(
    path: Path,
    *,
    offer_price_list_item_id: str,
    ktru_code: str,
    channel: str,
    link_date: str = "",
    unit_price: float | None = None,
    trade_mark: str = "",
    card_url: str = "",
    status: str,
    error_text: str = "",
    sent_at_utc: datetime | None = None,
) -> None:
    sent_iso = _utc_iso(sent_at_utc or datetime.now(timezone.utc))
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO rts_pp_monitor_notifications(
              offer_price_list_item_id, ktru_code, channel, link_date, unit_price,
              trade_mark, card_url, sent_at_utc, status, error_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(offer_price_list_item_id, channel) DO UPDATE SET
              ktru_code=excluded.ktru_code,
              link_date=excluded.link_date,
              unit_price=excluded.unit_price,
              trade_mark=excluded.trade_mark,
              card_url=excluded.card_url,
              sent_at_utc=excluded.sent_at_utc,
              status=excluded.status,
              error_text=excluded.error_text
            """,
            (
                offer_price_list_item_id,
                ktru_code,
                channel,
                link_date[:200],
                unit_price,
                trade_mark[:500],
                card_url[:500],
                sent_iso,
                status,
                error_text[:2000],
            ),
        )
        conn.commit()


def list_pp_monitor_recent(path: Path, *, limit: int = 30) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 200))
    if not path.is_file():
        return []
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT n.offer_price_list_item_id, n.ktru_code, n.link_date, n.unit_price,
                   n.trade_mark, n.card_url, n.sent_at_utc, n.status, n.error_text
            FROM rts_pp_monitor_notifications n
            INNER JOIN (
                SELECT offer_price_list_item_id, MAX(rowid) AS max_rowid
                FROM rts_pp_monitor_notifications
                GROUP BY offer_price_list_item_id
            ) u ON n.rowid = u.max_rowid
            ORDER BY n.sent_at_utc DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["sent_display_msk"] = _display_msk(d.get("sent_at_utc"))
        out.append(d)
    return out


def clear_pp_monitor_notifications(path: Path) -> None:
    if not path.is_file():
        return
    with connect(path) as conn:
        conn.execute("DELETE FROM rts_pp_monitor_notifications")
        conn.commit()


def clear_pp_monitor_registry(path: Path) -> None:
    if not path.is_file():
        return
    with connect(path) as conn:
        conn.execute("DELETE FROM rts_pp_monitor_registry")
        conn.commit()


def upsert_pp_monitor_registry_row(
    path: Path,
    *,
    offer_price_list_item_id: str,
    ktru_code: str,
    name: str = "",
    trade_mark: str = "",
    link_date: str = "",
    validity_date: str = "",
    unit_price: float | None = None,
    delivery_regions_count: int | None = None,
    card_url: str = "",
    price_list_item_id: int | None = None,
    seen_at_utc: datetime | None = None,
) -> None:
    oid = str(offer_price_list_item_id or "").strip()
    if not oid:
        return
    seen_iso = _utc_iso(seen_at_utc or datetime.now(timezone.utc))
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO rts_pp_monitor_registry(
              offer_price_list_item_id, ktru_code, price_list_item_id, name, trade_mark,
              link_date, validity_date, unit_price, delivery_regions_count, card_url,
              first_seen_at_utc, last_seen_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(offer_price_list_item_id) DO UPDATE SET
              ktru_code=excluded.ktru_code,
              price_list_item_id=excluded.price_list_item_id,
              name=excluded.name,
              trade_mark=excluded.trade_mark,
              link_date=excluded.link_date,
              validity_date=excluded.validity_date,
              unit_price=excluded.unit_price,
              delivery_regions_count=excluded.delivery_regions_count,
              card_url=excluded.card_url,
              last_seen_at_utc=excluded.last_seen_at_utc
            """,
            (
                oid,
                str(ktru_code or "").strip(),
                price_list_item_id,
                name[:500],
                trade_mark[:500],
                link_date[:200],
                validity_date[:200],
                unit_price,
                delivery_regions_count,
                card_url[:500],
                seen_iso,
                seen_iso,
            ),
        )
        conn.commit()


def get_pp_monitor_registry_row(path: Path, offer_price_list_item_id: str) -> dict[str, Any] | None:
    oid = str(offer_price_list_item_id or "").strip()
    if not oid or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT offer_price_list_item_id, ktru_code, price_list_item_id, name, trade_mark,
                   link_date, validity_date, unit_price, delivery_regions_count, card_url,
                   first_seen_at_utc, last_seen_at_utc, characteristics_hash
            FROM rts_pp_monitor_registry
            WHERE offer_price_list_item_id = ?
            """,
            (oid,),
        ).fetchone()
    return dict(row) if row else None


def list_registry_ids_for_ktru(path: Path, ktru_code: str) -> set[str]:
    code = str(ktru_code or "").strip()
    if not code or not path.is_file():
        return set()
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT offer_price_list_item_id FROM rts_pp_monitor_registry WHERE ktru_code = ?",
            (code,),
        ).fetchall()
    return {str(row[0]).strip() for row in rows if str(row[0] or "").strip()}


def _pp_monitor_registry_row(d: dict[str, Any]) -> dict[str, Any]:
    link_raw = str(d.get("link_date") or "").strip()
    validity_raw = str(d.get("validity_date") or "").strip()
    first_seen_raw = str(d.get("first_seen_at_utc") or "").strip()
    last_seen_raw = str(d.get("last_seen_at_utc") or "").strip()
    d["link_date_display_msk"] = display_compare_datetime_msk(link_raw)
    d["link_date_sort_key"] = compare_datetime_sort_key(link_raw)
    d["validity_display_msk"] = display_compare_datetime_msk(validity_raw)
    d["validity_sort_key"] = compare_datetime_sort_key(validity_raw)
    d["first_seen_display_msk"] = display_compare_datetime_msk(first_seen_raw)
    d["first_seen_sort_key"] = compare_datetime_sort_key(first_seen_raw)
    d["last_seen_display_msk"] = display_compare_datetime_msk(last_seen_raw)
    d["last_seen_sort_key"] = compare_datetime_sort_key(last_seen_raw)
    price = d.get("unit_price")
    if price is None:
        d["unit_price_sort_key"] = ""
    else:
        try:
            d["unit_price_sort_key"] = f"{float(price):015.4f}"
        except (TypeError, ValueError):
            d["unit_price_sort_key"] = ""
    regions = d.get("delivery_regions_count")
    if regions is None:
        d["delivery_regions_sort_key"] = ""
    else:
        try:
            d["delivery_regions_sort_key"] = f"{int(regions):010d}"
        except (TypeError, ValueError):
            d["delivery_regions_sort_key"] = ""
    plid = d.get("price_list_item_id")
    if plid is None:
        d["price_list_item_sort_key"] = ""
    else:
        try:
            d["price_list_item_sort_key"] = f"{int(plid):015d}"
        except (TypeError, ValueError):
            d["price_list_item_sort_key"] = ""
    return d


def list_pp_monitor_registry_ktru_codes(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ktru_code
            FROM rts_pp_monitor_registry
            WHERE trim(ktru_code) != ''
            ORDER BY ktru_code
            """
        ).fetchall()
    return [str(row[0]).strip() for row in rows if str(row[0] or "").strip()]


def list_pp_monitor_registry_names(
    path: Path,
    *,
    ktru_code: str | None = None,
) -> list[str]:
    if not path.is_file():
        return []
    code = str(ktru_code or "").strip()
    params: list[Any] = []
    ktru_sql = ""
    if code:
        ktru_sql = " AND r.ktru_code = ?"
        params.append(code)
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT TRIM(r.name) AS name
            FROM rts_pp_monitor_registry r
            WHERE TRIM(r.name) != ''{ktru_sql}
            ORDER BY name COLLATE NOCASE
            """,
            params,
        ).fetchall()
    return [str(row[0]).strip() for row in rows if str(row[0] or "").strip()]


def pp_winner_offer_ids(path: Path, offer_ids: list[str]) -> set[str]:
    """Offer ids treated as procedure winner PP for registry highlighting."""
    ids = [str(oid or "").strip() for oid in offer_ids if str(oid or "").strip()]
    if not ids or not path.is_file():
        return set()
    placeholders = ",".join("?" * len(ids))
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT m.offer_price_list_item_id
            FROM pp_eis_pp_match m
            LEFT JOIN notice_eis_outcome o
              ON o.purchase_number = m.purchase_number AND o.line_key = m.line_key
            WHERE m.offer_price_list_item_id IN ({placeholders})
              AND (
                m.match_origin = 'eis_winner_price'
                OR LOWER(TRIM(COALESCE(m.analyst_status, ''))) = 'confirmed'
                OR LOWER(TRIM(COALESCE(m.match_origin, ''))) = 'manual'
                OR (
                  COALESCE(m.is_exact_match, 0) != 0
                  AND LOWER(TRIM(COALESCE(o.eis_outcome, ''))) = 'completed_winner'
                )
              )
            """,
            ids,
        ).fetchall()
    return {str(row[0]).strip() for row in rows if str(row[0] or "").strip()}


def _registry_list_where(
    ktru_code: str | None,
    name_query: str | None = None,
    name_values: list[str] | None = None,
    unit_price_min: float | None = None,
    unit_price_max: float | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    code = str(ktru_code or "").strip()
    if code:
        clauses.append("r.ktru_code = ?")
        params.append(code)
    names = [str(n or "").strip() for n in (name_values or []) if str(n or "").strip()]
    if names:
        placeholders = ",".join("?" * len(names))
        clauses.append(f"TRIM(r.name) IN ({placeholders})")
        params.extend(names)
    else:
        q = str(name_query or "").strip()
        if q:
            clauses.append("LOWER(r.name) LIKE ?")
            params.append(f"%{q.lower()}%")
    price_min = unit_price_min
    price_max = unit_price_max
    if price_min is not None and price_max is not None and price_min > price_max:
        price_min, price_max = price_max, price_min
    if price_min is not None:
        clauses.append("r.unit_price IS NOT NULL AND r.unit_price >= ?")
        params.append(price_min)
    if price_max is not None:
        clauses.append("r.unit_price IS NOT NULL AND r.unit_price <= ?")
        params.append(price_max)
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


_REGISTRY_SORT_COLUMNS: dict[str, str] = {
    "last_seen_at_utc": "r.last_seen_at_utc",
    "first_seen_at_utc": "r.first_seen_at_utc",
    "link_date": "r.link_date",
    "offer_id": "r.offer_price_list_item_id",
    "ktru_code": "r.ktru_code",
    "name": "r.name",
    "trade_mark": "r.trade_mark",
    "validity_date": "r.validity_date",
    "unit_price": "r.unit_price",
    "delivery_regions_count": "r.delivery_regions_count",
    "has_find": "has_find",
}


def count_pp_monitor_registry(
    path: Path,
    *,
    ktru_code: str | None = None,
    name_query: str | None = None,
    name_values: list[str] | None = None,
    unit_price_min: float | None = None,
    unit_price_max: float | None = None,
) -> int:
    if not path.is_file():
        return 0
    where_sql, params = _registry_list_where(
        ktru_code,
        name_query,
        name_values,
        unit_price_min,
        unit_price_max,
    )
    with connect(path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM rts_pp_monitor_registry r {where_sql}",
            params,
        ).fetchone()
    return int(row[0]) if row else 0


def _utc_ge(left: object, right: str) -> bool:
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return False
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00"))
        db = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return da >= db
    except ValueError:
        return False


def list_pp_monitor_registry(
    path: Path,
    *,
    limit: int = 50,
    offset: int = 0,
    ktru_code: str | None = None,
    name_query: str | None = None,
    name_values: list[str] | None = None,
    unit_price_min: float | None = None,
    unit_price_max: float | None = None,
    sort: str = "last_seen_at_utc",
    sort_dir: str = "desc",
    highlight_since_utc: str | None = None,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    if not path.is_file():
        return []
    where_sql, params = _registry_list_where(
        ktru_code,
        name_query,
        name_values,
        unit_price_min,
        unit_price_max,
    )
    sort_col = _REGISTRY_SORT_COLUMNS.get(str(sort or "").strip(), "r.last_seen_at_utc")
    direction = "ASC" if str(sort_dir or "").strip().lower() == "asc" else "DESC"
    query_params = list(params) + [lim, off]
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT r.offer_price_list_item_id, r.ktru_code, r.price_list_item_id, r.name, r.trade_mark,
                   r.link_date, r.validity_date, r.unit_price, r.delivery_regions_count, r.card_url,
                   r.first_seen_at_utc, r.last_seen_at_utc, r.characteristics_hash,
                   CASE WHEN f.offer_price_list_item_id IS NOT NULL THEN 1 ELSE 0 END AS has_find
            FROM rts_pp_monitor_registry r
            LEFT JOIN rts_pp_monitor_findings f
              ON f.offer_price_list_item_id = r.offer_price_list_item_id
            {where_sql}
            ORDER BY {sort_col} {direction}, r.offer_price_list_item_id
            LIMIT ? OFFSET ?
            """,
            query_params,
        ).fetchall()
    out: list[dict[str, Any]] = []
    highlight_since = str(highlight_since_utc or "").strip()
    for row in rows:
        d = _pp_monitor_registry_row(dict(row))
        d["has_find"] = bool(d.get("has_find"))
        d["is_latest_pp_new"] = bool(
            highlight_since
            and (_utc_ge(d.get("last_seen_at_utc"), highlight_since) or _utc_ge(d.get("first_seen_at_utc"), highlight_since))
        )
        out.append(d)
    return out


def upsert_pp_monitor_finding(
    path: Path,
    *,
    offer_price_list_item_id: str,
    find_json: str,
    ktru_code: str = "",
    trade_mark: str = "",
    link_date: str = "",
    unit_price: float | None = None,
    card_url: str = "",
    source: str = "notify",
    trigger_recompute: bool = True,
) -> None:
    oid = str(offer_price_list_item_id or "").strip()
    if not oid:
        raise ValueError("offer_price_list_item_id required")
    fetched_iso = _utc_iso(datetime.now(timezone.utc))
    chash = _characteristics_hash_from_find_json(find_json)
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO rts_pp_monitor_findings(
              offer_price_list_item_id, ktru_code, trade_mark, link_date, unit_price,
              card_url, find_json, fetched_at_utc, source, characteristics_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(offer_price_list_item_id) DO UPDATE SET
              ktru_code=excluded.ktru_code,
              trade_mark=excluded.trade_mark,
              link_date=excluded.link_date,
              unit_price=excluded.unit_price,
              card_url=excluded.card_url,
              find_json=excluded.find_json,
              fetched_at_utc=excluded.fetched_at_utc,
              source=excluded.source,
              characteristics_hash=excluded.characteristics_hash
            """,
            (
                oid,
                str(ktru_code or "")[:200],
                str(trade_mark or "")[:500],
                str(link_date or "")[:200],
                unit_price,
                str(card_url or "")[:500],
                find_json,
                fetched_iso,
                str(source or "notify")[:50],
                chash or None,
            ),
        )
        if chash:
            conn.execute(
                """
                UPDATE rts_pp_monitor_registry
                SET characteristics_hash = ?
                WHERE offer_price_list_item_id = ?
                """,
                (chash, oid),
            )
        conn.commit()
    if not trigger_recompute:
        return
    try:
        from eis_pp_link_service import maybe_recompute_after_pp_finding

        meta_dir = Path(os.environ.get("RTS_META_DIR", "/var/lib/rts_tender"))
        hist_raw = os.environ.get("EIS_HISTORY_DB", "").strip()
        history_path = Path(hist_raw) if hist_raw else None
        maybe_recompute_after_pp_finding(
            path,
            meta_dir=meta_dir,
            offer_price_list_item_id=oid,
            ktru_code=str(ktru_code or ""),
            find_json=find_json,
            link_date=str(link_date or ""),
            characteristics_hash=chash or "",
            history_path=history_path if history_path and history_path.is_file() else None,
        )
    except Exception:
        pass


def list_pp_monitor_registry_link_window(
    path: Path,
    *,
    ktru_codes: list[str] | None = None,
    link_from_utc: str,
    link_to_utc: str | None = None,
    missing_find_only: bool = False,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    codes = [str(c or "").strip() for c in (ktru_codes or []) if str(c or "").strip()]
    where = ["r.link_date >= ?"]
    params: list[Any] = [str(link_from_utc or "").strip()]
    if link_to_utc:
        where.append("r.link_date <= ?")
        params.append(str(link_to_utc or "").strip())
    if codes:
        placeholders = ",".join("?" for _ in codes)
        where.append(f"r.ktru_code IN ({placeholders})")
        params.extend(codes)
    join_find = ""
    if missing_find_only:
        join_find = (
            "LEFT JOIN rts_pp_monitor_findings f "
            "ON f.offer_price_list_item_id = r.offer_price_list_item_id "
        )
        where.append("f.offer_price_list_item_id IS NULL")
    lim = max(1, min(int(limit), 20000))
    params.append(lim)
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT r.offer_price_list_item_id, r.ktru_code, r.trade_mark, r.link_date,
                   r.unit_price, r.card_url, r.characteristics_hash
            FROM rts_pp_monitor_registry r
            {join_find}
            WHERE {' AND '.join(where)}
            ORDER BY r.link_date DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _characteristics_hash_from_find_json(find_json: str) -> str:
    from pp_stack import EMPTY_HASH, find_characteristics_fingerprint

    raw = str(find_json or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    digest, _ = find_characteristics_fingerprint(parsed)
    return digest if digest else ""


def backfill_pp_characteristics_hashes(path: Path) -> int:
    """Recompute characteristics_hash from stored find_json. Returns rows updated."""
    if not path.is_file():
        return 0
    updated = 0
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT offer_price_list_item_id, find_json
            FROM rts_pp_monitor_findings
            WHERE TRIM(COALESCE(find_json, '')) != ''
            """
        ).fetchall()
        for row in rows:
            oid = str(row[0] or "").strip()
            find_json = str(row[1] or "")
            chash = _characteristics_hash_from_find_json(find_json)
            if not chash:
                continue
            conn.execute(
                """
                UPDATE rts_pp_monitor_findings
                SET characteristics_hash = ?
                WHERE offer_price_list_item_id = ?
                """,
                (chash, oid),
            )
            conn.execute(
                """
                UPDATE rts_pp_monitor_registry
                SET characteristics_hash = ?
                WHERE offer_price_list_item_id = ?
                """,
                (chash, oid),
            )
            updated += 1
        conn.commit()
    return updated


def _fetch_pp_monitor_registry_for_grouping(
    path: Path,
    *,
    ktru_code: str | None = None,
    name_query: str | None = None,
    name_values: list[str] | None = None,
    unit_price_min: float | None = None,
    unit_price_max: float | None = None,
    highlight_since_utc: str | None = None,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    where_sql, params = _registry_list_where(
        ktru_code,
        name_query,
        name_values,
        unit_price_min,
        unit_price_max,
    )
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT r.offer_price_list_item_id, r.ktru_code, r.price_list_item_id, r.name, r.trade_mark,
                   r.link_date, r.validity_date, r.unit_price, r.delivery_regions_count, r.card_url,
                   r.first_seen_at_utc, r.last_seen_at_utc, r.characteristics_hash,
                   CASE WHEN f.offer_price_list_item_id IS NOT NULL THEN 1 ELSE 0 END AS has_find
            FROM rts_pp_monitor_registry r
            LEFT JOIN rts_pp_monitor_findings f
              ON f.offer_price_list_item_id = r.offer_price_list_item_id
            {where_sql}
            ORDER BY r.link_date ASC, r.offer_price_list_item_id ASC
            """,
            params,
        ).fetchall()
    highlight_since = str(highlight_since_utc or "").strip()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = _pp_monitor_registry_row(dict(row))
        d["has_find"] = bool(d.get("has_find"))
        d["is_latest_pp_new"] = bool(
            highlight_since
            and (_utc_ge(d.get("last_seen_at_utc"), highlight_since) or _utc_ge(d.get("first_seen_at_utc"), highlight_since))
        )
        out.append(d)
    return out


def _registry_group_sort_key(primary: dict[str, Any], sort: str) -> str:
    key = str(sort or "last_seen_at_utc").strip()
    mapping = {
        "last_seen_at_utc": "last_seen_sort_key",
        "first_seen_at_utc": "first_seen_sort_key",
        "link_date": "link_date_sort_key",
        "offer_id": "offer_price_list_item_id",
        "ktru_code": "ktru_code",
        "name": "name",
        "trade_mark": "trade_mark",
        "validity_date": "validity_sort_key",
        "unit_price": "unit_price_sort_key",
        "delivery_regions_count": "delivery_regions_sort_key",
        "has_find": "has_find",
    }
    field = mapping.get(key, "last_seen_sort_key")
    if field == "has_find":
        return "1" if primary.get("has_find") else "0"
    return str(primary.get(field) or "")


def _build_pp_monitor_registry_groups(
    path: Path,
    *,
    ktru_code: str | None = None,
    name_query: str | None = None,
    name_values: list[str] | None = None,
    unit_price_min: float | None = None,
    unit_price_max: float | None = None,
    sort: str = "last_seen_at_utc",
    sort_dir: str = "desc",
    highlight_since_utc: str | None = None,
) -> list[dict[str, Any]]:
    from pp_stack import stack_key

    rows = _fetch_pp_monitor_registry_for_grouping(
        path,
        ktru_code=ktru_code,
        name_query=name_query,
        name_values=name_values,
        unit_price_min=unit_price_min,
        unit_price_max=unit_price_max,
        highlight_since_utc=highlight_since_utc,
    )
    by_stack: dict[str, list[dict[str, Any]]] = {}
    singles_no_hash: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("ktru_code") or "").strip()
        chash = str(row.get("characteristics_hash") or "").strip()
        if code and chash and chash != "empty":
            sk = stack_key(code, chash)
            by_stack.setdefault(sk, []).append(row)
        else:
            singles_no_hash.append(row)

    groups: list[dict[str, Any]] = []
    all_offer_ids = [
        str(r.get("offer_price_list_item_id") or "")
        for r in rows
        if str(r.get("offer_price_list_item_id") or "")
    ]
    draft_prices = load_pp_monitor_draft_prices(path, all_offer_ids)

    for sk, members in by_stack.items():
        members.sort(
            key=lambda m: (
                str(m.get("link_date_sort_key") or ""),
                str(m.get("offer_price_list_item_id") or ""),
            )
        )
        marked = mark_our_pp_members(members, draft_prices=draft_prices)
        primary = marked[0]
        if len(marked) >= 2:
            groups.append(
                {
                    "is_stack": True,
                    "stack_key": sk,
                    "member_count": len(marked),
                    "primary": primary,
                    "children": marked[1:],
                    "has_our_pp": any(bool(m.get("is_our_pp")) for m in marked),
                }
            )
        else:
            groups.append(
                {
                    "is_stack": False,
                    "stack_key": sk,
                    "member_count": 1,
                    "primary": primary,
                    "children": [],
                    "has_our_pp": bool(primary.get("is_our_pp")),
                }
            )

    for row in singles_no_hash:
        groups.append(
            {
                "is_stack": False,
                "stack_key": None,
                "member_count": 1,
                "primary": row,
                "children": [],
                "has_our_pp": False,
            }
        )

    reverse = str(sort_dir or "desc").strip().lower() != "asc"
    groups.sort(
        key=lambda g: _registry_group_sort_key(g["primary"], sort),
        reverse=reverse,
    )
    return groups


def count_pp_monitor_registry_groups(
    path: Path,
    *,
    ktru_code: str | None = None,
    name_query: str | None = None,
    name_values: list[str] | None = None,
    unit_price_min: float | None = None,
    unit_price_max: float | None = None,
) -> int:
    return len(
        _build_pp_monitor_registry_groups(
            path,
            ktru_code=ktru_code,
            name_query=name_query,
            name_values=name_values,
            unit_price_min=unit_price_min,
            unit_price_max=unit_price_max,
        )
    )


def list_pp_monitor_registry_groups(
    path: Path,
    *,
    limit: int = 50,
    offset: int = 0,
    ktru_code: str | None = None,
    name_query: str | None = None,
    name_values: list[str] | None = None,
    unit_price_min: float | None = None,
    unit_price_max: float | None = None,
    sort: str = "last_seen_at_utc",
    sort_dir: str = "desc",
    highlight_since_utc: str | None = None,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    groups = _build_pp_monitor_registry_groups(
        path,
        ktru_code=ktru_code,
        name_query=name_query,
        name_values=name_values,
        unit_price_min=unit_price_min,
        unit_price_max=unit_price_max,
        sort=sort,
        sort_dir=sort_dir,
        highlight_since_utc=highlight_since_utc,
    )
    return groups[off : off + lim]


def _pp_stack_where(
    *,
    ktru_code: str | None = None,
    only_with_our: bool = False,
) -> tuple[str, list[Any]]:
    del only_with_our  # applied in Python after member prices loaded
    clauses = [
        "r.characteristics_hash IS NOT NULL",
        "TRIM(r.characteristics_hash) != ''",
        "r.characteristics_hash != 'empty'",
    ]
    params: list[Any] = []
    code = str(ktru_code or "").strip()
    if code:
        clauses.append("r.ktru_code = ?")
        params.append(code)
    return "WHERE " + " AND ".join(clauses), params


def count_pp_monitor_stacks(
    path: Path,
    *,
    ktru_code: str | None = None,
    min_members: int = 2,
    only_with_our: bool = False,
) -> int:
    stacks = _build_pp_monitor_stacks(
        path,
        ktru_code=ktru_code,
        min_members=min_members,
        only_with_our=only_with_our,
    )
    return len(stacks)


def _build_pp_monitor_stacks(
    path: Path,
    *,
    ktru_code: str | None = None,
    min_members: int = 2,
    only_with_our: bool = False,
    sort: str = "last_link_date",
    sort_dir: str = "desc",
) -> list[dict[str, Any]]:
    from pp_stack import mark_our_pp, stack_has_our_pp, stack_key

    min_mem = max(1, int(min_members))
    if not path.is_file():
        return []
    where_sql, params = _pp_stack_where(ktru_code=ktru_code)
    sort_col = {
        "last_link_date": "last_link_date",
        "member_count": "member_count",
        "ktru_code": "ktru_code",
        "min_price": "min_price",
        "max_price": "max_price",
    }.get(str(sort or "").strip(), "last_link_date")
    direction = "ASC" if str(sort_dir or "").strip().lower() == "asc" else "DESC"
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT r.ktru_code,
                   r.characteristics_hash,
                   COUNT(*) AS member_count,
                   MIN(r.link_date) AS first_link_date,
                   MAX(r.link_date) AS last_link_date,
                   MIN(r.unit_price) AS min_price,
                   MAX(r.unit_price) AS max_price
            FROM rts_pp_monitor_registry r
            {where_sql}
            GROUP BY r.ktru_code, r.characteristics_hash
            HAVING COUNT(*) >= ?
            ORDER BY {sort_col} {direction}, r.ktru_code, r.characteristics_hash
            """,
            [*params, min_mem],
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        code = str(d.get("ktru_code") or "").strip()
        chash = str(d.get("characteristics_hash") or "").strip()
        sk = stack_key(code, chash)
        d["stack_key"] = sk
        members = list_pp_monitor_stack_members(path, sk, annotate_our=False)
        draft_prices = load_pp_monitor_draft_prices(
            path,
            [str(m.get("offer_price_list_item_id") or "") for m in members],
        )
        marked = mark_our_pp(members, draft_prices=draft_prices)
        d["has_our_pp"] = stack_has_our_pp(members, draft_prices=draft_prices)
        d["our_count"] = sum(1 for m in marked if m.get("is_our_pp"))
        if only_with_our and not d["has_our_pp"]:
            continue
        out.append(d)
    return out


def list_pp_monitor_stacks(
    path: Path,
    *,
    ktru_code: str | None = None,
    min_members: int = 2,
    only_with_our: bool = False,
    limit: int = 50,
    offset: int = 0,
    sort: str = "last_link_date",
    sort_dir: str = "desc",
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    stacks = _build_pp_monitor_stacks(
        path,
        ktru_code=ktru_code,
        min_members=min_members,
        only_with_our=only_with_our,
        sort=sort,
        sort_dir=sort_dir,
    )
    return stacks[off : off + lim]


def mark_our_pp_members(
    members: list[dict[str, Any]],
    draft_prices: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    from pp_stack import mark_our_pp

    return mark_our_pp(members, draft_prices=draft_prices)


def load_pp_monitor_draft_prices(path: Path, offer_ids: Iterable[str]) -> dict[str, float]:
    ids = [str(x or "").strip() for x in offer_ids if str(x or "").strip()]
    if not ids or not path.is_file():
        return {}
    placeholders = ",".join("?" * len(ids))
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT offer_price_list_item_id, draft_unit_price
            FROM rts_pp_monitor_drafts
            WHERE offer_price_list_item_id IN ({placeholders})
              AND draft_unit_price IS NOT NULL
            """,
            ids,
        ).fetchall()
    out: dict[str, float] = {}
    for row in rows:
        oid = str(row[0] or "").strip()
        try:
            price = float(row[1])
        except (TypeError, ValueError):
            continue
        if oid:
            out[oid] = price
    return out


def load_pp_monitor_draft_created_at(
    path: Path,
    offer_ids: Iterable[str],
    *,
    rts_profiles: Iterable[str] | None = None,
) -> dict[str, str]:
    """Latest created_at_utc per offer among monitor drafts visible to the caller."""
    ids = [str(x or "").strip() for x in offer_ids if str(x or "").strip()]
    profiles = [str(p or "").strip() for p in (rts_profiles or []) if str(p or "").strip()]
    if not ids or not path.is_file():
        return {}
    id_ph = ",".join("?" * len(ids))
    params: list[Any] = list(ids)
    profile_sql = ""
    if profiles:
        profile_sql = f" AND rts_profile IN ({','.join('?' * len(profiles))})"
        params.extend(profiles)
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT offer_price_list_item_id, MAX(created_at_utc) AS created_at_utc
            FROM rts_pp_monitor_drafts
            WHERE offer_price_list_item_id IN ({id_ph}){profile_sql}
            GROUP BY offer_price_list_item_id
            """,
            params,
        ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        oid = str(row[0] or "").strip()
        created = str(row[1] or "").strip()
        if oid and created:
            out[oid] = created
    return out


def load_pp_monitor_draft_offer_ids(
    path: Path,
    offer_ids: Iterable[str],
    *,
    rts_profiles: Iterable[str] | None = None,
) -> set[str]:
    """Offer IDs that still have a row in rts_pp_monitor_drafts (Черновики ПП, источник monitor)."""
    return set(load_pp_monitor_draft_created_at(path, offer_ids, rts_profiles=rts_profiles).keys())


def list_pp_monitor_stack_members(
    path: Path,
    stack_key_raw: str,
    *,
    annotate_our: bool = True,
) -> list[dict[str, Any]]:
    from pp_stack import parse_stack_key

    parsed = parse_stack_key(stack_key_raw)
    if parsed is None or not path.is_file():
        return []
    ktru_code, chash = parsed
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT r.offer_price_list_item_id, r.ktru_code, r.price_list_item_id, r.name, r.trade_mark,
                   r.link_date, r.validity_date, r.unit_price, r.delivery_regions_count, r.card_url,
                   r.first_seen_at_utc, r.last_seen_at_utc, r.characteristics_hash,
                   CASE WHEN f.offer_price_list_item_id IS NOT NULL THEN 1 ELSE 0 END AS has_find
            FROM rts_pp_monitor_registry r
            LEFT JOIN rts_pp_monitor_findings f
              ON f.offer_price_list_item_id = r.offer_price_list_item_id
            WHERE r.ktru_code = ? AND r.characteristics_hash = ?
            ORDER BY r.link_date ASC, r.offer_price_list_item_id ASC
            """,
            (ktru_code, chash),
        ).fetchall()
    members = [_pp_monitor_registry_row(dict(row)) for row in rows]
    if not annotate_our:
        return members
    draft_prices = load_pp_monitor_draft_prices(
        path,
        [str(m.get("offer_price_list_item_id") or "") for m in members],
    )
    return mark_our_pp_members(members, draft_prices=draft_prices)


def _pp_monitor_finding_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    raw_json = str(d.get("find_json") or "").strip()
    find_item: dict[str, Any] = {}
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                find_item = parsed
        except json.JSONDecodeError:
            pass
    d["find"] = find_item
    d["fetched_display_msk"] = _display_msk(d.get("fetched_at_utc"))
    added_raw = str(find_item.get("offerPriceListItemLinkDate") or d.get("link_date") or "").strip()
    validity_raw = _compare_validity_raw(find_item)
    d["added_display_msk"] = display_compare_datetime_msk(added_raw)
    d["added_sort_key"] = compare_datetime_sort_key(added_raw)
    d["validity_display_msk"] = display_compare_datetime_msk(validity_raw)
    d["link_date_display_msk"] = d["added_display_msk"]
    d["link_date_sort_key"] = d["added_sort_key"]
    ktru_obj = find_item.get("ktru") if isinstance(find_item.get("ktru"), dict) else {}
    d["ktru_name"] = str(ktru_obj.get("name") or "").strip()
    places = find_item.get("deliveryPlaces")
    if isinstance(places, list):
        codes: set[str] = set()
        for place in places:
            if not isinstance(place, dict):
                continue
            oktmo = place.get("oktmo")
            if isinstance(oktmo, dict):
                code = str(oktmo.get("code") or "").strip()
                if code:
                    codes.add(code)
        d["delivery_regions_count"] = len(codes) if codes else len(places)
    else:
        d["delivery_regions_count"] = None
    return d


def get_pp_monitor_finding(path: Path, offer_price_list_item_id: str) -> dict[str, Any] | None:
    oid = str(offer_price_list_item_id or "").strip()
    if not oid or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT offer_price_list_item_id, ktru_code, trade_mark, link_date, unit_price,
                   card_url, find_json, fetched_at_utc, source, characteristics_hash
            FROM rts_pp_monitor_findings
            WHERE offer_price_list_item_id = ?
            """,
            (oid,),
        ).fetchone()
    if row is None:
        return None
    return _pp_monitor_finding_row(row)


def list_pp_monitor_findings(
    path: Path,
    *,
    limit: int = 100,
    ktru_code: str | None = None,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 500))
    if not path.is_file():
        return []
    code = str(ktru_code or "").strip()
    with connect(path) as conn:
        if code:
            rows = conn.execute(
                """
                SELECT offer_price_list_item_id, ktru_code, trade_mark, link_date, unit_price,
                       card_url, find_json, fetched_at_utc, source
                FROM rts_pp_monitor_findings
                WHERE ktru_code = ?
                ORDER BY fetched_at_utc DESC
                LIMIT ?
                """,
                (code, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT offer_price_list_item_id, ktru_code, trade_mark, link_date, unit_price,
                       card_url, find_json, fetched_at_utc, source
                FROM rts_pp_monitor_findings
                ORDER BY fetched_at_utc DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
    return [_pp_monitor_finding_row(row) for row in rows]


def get_pp_monitor_notification_latest(path: Path, offer_price_list_item_id: str) -> dict[str, Any] | None:
    oid = str(offer_price_list_item_id or "").strip()
    if not oid or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT offer_price_list_item_id, ktru_code, link_date, unit_price,
                   trade_mark, card_url, sent_at_utc, status, error_text
            FROM rts_pp_monitor_notifications
            WHERE offer_price_list_item_id = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (oid,),
        ).fetchone()
    return dict(row) if row is not None else None


def _parse_rts_local_datetime(raw: object) -> datetime | None:
    """Parse RTS API datetime (naive local MSK or ISO with offset)."""
    s = str(raw or "").strip()
    if not s or s.startswith("0001-01-01"):
        return None
    if "T" in s and ("+" in s[10:] or s.endswith("Z")):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(MSK)
        except ValueError:
            pass
    for fmt, length in (
        ("%Y-%m-%dT%H:%M:%S.%f", 26),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    ):
        try:
            dt = datetime.strptime(s[:length], fmt)
            return dt.replace(tzinfo=MSK)
        except ValueError:
            continue
    return None


def _parse_compare_datetime_utc(raw: object) -> datetime | None:
    """Parse compare/find naive datetime as UTC; display in MSK (+3)."""
    s = str(raw or "").strip()
    if not s or s.startswith("0001-01-01"):
        return None
    if "T" in s and ("+" in s[10:] or s.endswith("Z")):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(MSK)
        except ValueError:
            pass
    for fmt, length in (
        ("%Y-%m-%dT%H:%M:%S.%f", 26),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    ):
        try:
            dt = datetime.strptime(s[:length], fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(MSK)
        except ValueError:
            continue
    return None


def display_compare_datetime_msk(raw: object) -> str:
    dt = _parse_compare_datetime_utc(raw)
    if dt is None:
        return "—"
    return dt_to_msk_clean(dt) + " (МСК)"


def compare_datetime_sort_key(raw: object) -> str:
    dt = _parse_compare_datetime_utc(raw)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _compare_validity_raw(find_item: dict[str, Any]) -> str:
    validity = str(find_item.get("validityDate") or "").strip()
    if validity and not validity.startswith("0001-01-01"):
        return validity
    offer = find_item.get("offer")
    if isinstance(offer, dict):
        validity = str(offer.get("validityDate") or "").strip()
        if validity and not validity.startswith("0001-01-01"):
            return validity
    return ""


def display_rts_datetime_msk(raw: object) -> str:
    dt = _parse_rts_local_datetime(raw)
    if dt is None:
        return "—"
    return dt_to_msk_clean(dt) + " (МСК)"


def rts_datetime_sort_key(raw: object) -> str:
    dt = _parse_rts_local_datetime(raw)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _pp_monitor_item_name(find_item: dict[str, Any], row: dict[str, Any]) -> str:
    name = str(find_item.get("name") or "").strip()
    if not name:
        name = str(row.get("trade_mark") or find_item.get("tradeMark") or "").strip()
    if not name:
        ktru = find_item.get("ktru")
        if isinstance(ktru, dict):
            name = str(ktru.get("name") or "").strip()
    return name


def _pp_monitor_journal_row(d: dict[str, Any]) -> dict[str, Any]:
    find_item = d.get("find") if isinstance(d.get("find"), dict) else {}
    create_raw = find_item.get("createDateTime") or ""
    validity_raw = find_item.get("validityDate") or ""
    link_raw = str(d.get("link_date") or find_item.get("offerPriceListItemLinkDate") or "").strip()
    d["create_display_msk"] = display_rts_datetime_msk(create_raw)
    d["validity_display_msk"] = display_rts_datetime_msk(validity_raw)
    d["link_date_display_msk"] = display_rts_datetime_msk(link_raw)
    d["create_sort_key"] = rts_datetime_sort_key(create_raw) or rts_datetime_sort_key(d.get("sent_at_utc"))
    d["item_name"] = _pp_monitor_item_name(find_item, d)
    places = find_item.get("deliveryPlaces")
    d["delivery_places_count"] = len(places) if isinstance(places, list) else None
    d.pop("find_json", None)
    d.pop("find", None)
    return d


def list_pp_monitor_journal_with_findings(
    path: Path,
    *,
    limit: int = 30,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 200))
    if not path.is_file():
        return []
    fetch_limit = max(lim * 10, 200) if settings else lim
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT n.offer_price_list_item_id, n.ktru_code, n.link_date, n.unit_price,
                   n.trade_mark, n.card_url, n.sent_at_utc, n.status, n.error_text,
                   CASE WHEN f.offer_price_list_item_id IS NOT NULL THEN 1 ELSE 0 END AS has_find,
                   f.find_json
            FROM rts_pp_monitor_notifications n
            INNER JOIN (
                SELECT offer_price_list_item_id, MAX(rowid) AS max_rowid
                FROM rts_pp_monitor_notifications
                GROUP BY offer_price_list_item_id
            ) u ON n.rowid = u.max_rowid
            LEFT JOIN rts_pp_monitor_findings f
              ON f.offer_price_list_item_id = n.offer_price_list_item_id
            ORDER BY n.sent_at_utc DESC
            LIMIT ?
            """,
            (fetch_limit,),
        ).fetchall()
    ktru_codes: set[str] = set()
    filter_fn = None
    if settings:
        from rts_pp_monitor_settings import effective_ktru_codes, passes_monitor_filters

        ktru_codes = set(effective_ktru_codes(settings))
        filter_fn = passes_monitor_filters
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["has_find"] = bool(d.get("has_find"))
        raw_json = str(d.get("find_json") or "").strip()
        find_item: dict[str, Any] = {}
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    find_item = parsed
            except json.JSONDecodeError:
                pass
        d["find"] = find_item
        if settings and filter_fn is not None:
            if ktru_codes and str(d.get("ktru_code") or "").strip() not in ktru_codes:
                continue
            summary = {
                "ktru_code": d.get("ktru_code"),
                "unit_price": d.get("unit_price"),
                "validity_date": str(find_item.get("validityDate") or "").strip(),
                "link_date": d.get("link_date"),
            }
            if not filter_fn(summary, settings):
                continue
        out.append(_pp_monitor_journal_row(d))
        if len(out) >= lim:
            break
    return out


def get_pp_monitor_draft(
    path: Path,
    offer_price_list_item_id: str,
    *,
    rts_profile: str = "",
) -> dict[str, Any] | None:
    oid = str(offer_price_list_item_id or "").strip()
    profile = str(rts_profile or "").strip()
    if not oid or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT offer_price_list_item_id, rts_profile, ktru_code, rts_draft_id, protected_id,
                   edit_url, offer_id, status, error_text, created_at_utc, created_by,
                   fill_report_json, verify_json, draft_unit_price, source_unit_price
            FROM rts_pp_monitor_drafts
            WHERE offer_price_list_item_id = ? AND rts_profile = ?
            """,
            (oid, profile),
        ).fetchone()
    return dict(row) if row is not None else None


def save_pp_monitor_draft(
    path: Path,
    *,
    offer_price_list_item_id: str,
    ktru_code: str,
    rts_draft_id: int | None,
    protected_id: str = "",
    edit_url: str = "",
    offer_id: str = "",
    status: str = "created",
    error_text: str = "",
    created_by: str = "",
    rts_profile: str = "",
    created_at_utc: datetime | None = None,
    fill_report_json: str = "",
    verify_json: str = "",
    draft_unit_price: float | None = None,
    source_unit_price: float | None = None,
) -> None:
    oid = str(offer_price_list_item_id or "").strip()
    profile = str(rts_profile or "").strip()
    created_iso = _utc_iso(created_at_utc or datetime.now(timezone.utc))
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO rts_pp_monitor_drafts(
              offer_price_list_item_id, rts_profile, ktru_code, rts_draft_id, protected_id, edit_url,
              offer_id, status, error_text, created_at_utc, created_by, fill_report_json, verify_json,
              draft_unit_price, source_unit_price
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(offer_price_list_item_id, rts_profile) DO UPDATE SET
              ktru_code=excluded.ktru_code,
              rts_draft_id=excluded.rts_draft_id,
              protected_id=excluded.protected_id,
              edit_url=excluded.edit_url,
              offer_id=excluded.offer_id,
              status=excluded.status,
              error_text=excluded.error_text,
              created_at_utc=excluded.created_at_utc,
              created_by=excluded.created_by,
              fill_report_json=excluded.fill_report_json,
              verify_json=excluded.verify_json,
              draft_unit_price=COALESCE(excluded.draft_unit_price, rts_pp_monitor_drafts.draft_unit_price),
              source_unit_price=COALESCE(excluded.source_unit_price, rts_pp_monitor_drafts.source_unit_price)
            """,
            (
                oid,
                profile,
                str(ktru_code or "")[:200],
                rts_draft_id,
                str(protected_id or "")[:200],
                str(edit_url or "")[:500],
                str(offer_id or "")[:200],
                str(status or "created")[:50],
                str(error_text or "")[:2000],
                created_iso,
                str(created_by or "")[:200],
                fill_report_json,
                verify_json,
                draft_unit_price,
                source_unit_price,
            ),
        )
        conn.commit()


def list_pp_monitor_drafts(
    path: Path,
    *,
    limit: int = 100,
    rts_profile: str | None = None,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 500))
    if not path.is_file():
        return []
    profile = str(rts_profile or "").strip()
    with connect(path) as conn:
        if profile:
            rows = conn.execute(
                """
                SELECT offer_price_list_item_id, rts_profile, ktru_code, rts_draft_id, protected_id,
                       edit_url, offer_id, status, error_text, created_at_utc, created_by,
                       fill_report_json, verify_json
                FROM rts_pp_monitor_drafts
                WHERE rts_profile = ?
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                (profile, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT offer_price_list_item_id, rts_profile, ktru_code, rts_draft_id, protected_id,
                       edit_url, offer_id, status, error_text, created_at_utc, created_by,
                       fill_report_json, verify_json
                FROM rts_pp_monitor_drafts
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
    return [dict(row) for row in rows]


MONITOR_DRAFT_LINE_PREFIX = "monitor:"


def monitor_draft_line_key(offer_price_list_item_id: str) -> str:
    oid = str(offer_price_list_item_id or "").strip()
    return f"{MONITOR_DRAFT_LINE_PREFIX}{oid}" if oid else ""


def is_monitor_draft_line_key(line_key: str) -> bool:
    return str(line_key or "").startswith(MONITOR_DRAFT_LINE_PREFIX)


def monitor_draft_offer_id_from_line_key(line_key: str) -> str:
    lk = str(line_key or "")
    if not lk.startswith(MONITOR_DRAFT_LINE_PREFIX):
        return ""
    return lk[len(MONITOR_DRAFT_LINE_PREFIX) :].strip()


def delete_pp_monitor_draft(
    path: Path,
    offer_price_list_item_id: str,
    *,
    rts_profile: str = "",
) -> bool:
    oid = str(offer_price_list_item_id or "").strip()
    profile = str(rts_profile or "").strip()
    if not oid or not path.is_file():
        return False
    with connect(path) as conn:
        cur = conn.execute(
            """
            DELETE FROM rts_pp_monitor_drafts
            WHERE offer_price_list_item_id = ? AND rts_profile = ?
            """,
            (oid, profile),
        )
        conn.commit()
        return int(cur.rowcount or 0) > 0


_PUBLISH_JOB_STATUSES = frozenset(
    {
        "preparing",
        "awaiting_signature",
        "timestamping",
        "publishing",
        "published",
        "failed",
        "cancelled",
    }
)


def _row_to_publish_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("prepare_json", "sign_requests_json", "signatures_json"):
        raw = str(d.get(key) or "").strip()
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
        d[key.replace("_json", "")] = parsed
    return d


def get_pp_publish_job(path: Path, job_id: int) -> dict[str, Any] | None:
    if not path.is_file() or int(job_id) <= 0:
        return None
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM rts_pp_publish_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
    return _row_to_publish_job(row)


def find_pp_publish_job_for_draft(
    path: Path,
    *,
    rts_draft_id: int,
    rts_profile: str = "",
    statuses: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    if not path.is_file() or int(rts_draft_id) <= 0:
        return None
    profile = str(rts_profile or "").strip()
    status_list = [str(s).strip() for s in (statuses or ()) if str(s).strip()]
    with connect(path) as conn:
        if status_list:
            placeholders = ",".join("?" for _ in status_list)
            row = conn.execute(
                f"""
                SELECT * FROM rts_pp_publish_jobs
                WHERE rts_draft_id = ? AND rts_profile = ?
                  AND status IN ({placeholders})
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(rts_draft_id), profile, *status_list),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM rts_pp_publish_jobs
                WHERE rts_draft_id = ? AND rts_profile = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(rts_draft_id), profile),
            ).fetchone()
    return _row_to_publish_job(row)


def create_pp_publish_job(
    path: Path,
    *,
    rts_draft_id: int,
    rts_profile: str = "",
    source: str = "",
    source_key: str = "",
    status: str = "preparing",
    prepare_json: str = "",
    sign_requests_json: str = "",
    signatures_json: str = "",
    error_text: str = "",
) -> int:
    now = _utc_iso(datetime.now(timezone.utc))
    st = str(status or "preparing").strip()
    if st not in _PUBLISH_JOB_STATUSES:
        st = "preparing"
    with connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO rts_pp_publish_jobs(
              rts_profile, rts_draft_id, source, source_key, status,
              prepare_json, sign_requests_json, signatures_json, error_text,
              created_at_utc, updated_at_utc, published_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (
                str(rts_profile or "").strip(),
                int(rts_draft_id),
                str(source or "")[:50],
                str(source_key or "")[:200],
                st,
                prepare_json or "",
                sign_requests_json or "",
                signatures_json or "",
                str(error_text or "")[:2000],
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_pp_publish_job(
    path: Path,
    job_id: int,
    *,
    status: str | None = None,
    prepare_json: str | None = None,
    sign_requests_json: str | None = None,
    signatures_json: str | None = None,
    error_text: str | None = None,
    published_at_utc: str | None = None,
) -> bool:
    if int(job_id) <= 0 or not path.is_file():
        return False
    fields: list[str] = ["updated_at_utc = ?"]
    values: list[Any] = [_utc_iso(datetime.now(timezone.utc))]
    if status is not None:
        st = str(status).strip()
        if st not in _PUBLISH_JOB_STATUSES:
            return False
        fields.append("status = ?")
        values.append(st)
    if prepare_json is not None:
        fields.append("prepare_json = ?")
        values.append(prepare_json)
    if sign_requests_json is not None:
        fields.append("sign_requests_json = ?")
        values.append(sign_requests_json)
    if signatures_json is not None:
        fields.append("signatures_json = ?")
        values.append(signatures_json)
    if error_text is not None:
        fields.append("error_text = ?")
        values.append(str(error_text)[:2000])
    if published_at_utc is not None:
        fields.append("published_at_utc = ?")
        values.append(str(published_at_utc))
    values.append(int(job_id))
    with connect(path) as conn:
        cur = conn.execute(
            f"UPDATE rts_pp_publish_jobs SET {', '.join(fields)} WHERE id = ?",
            tuple(values),
        )
        conn.commit()
        return int(cur.rowcount or 0) > 0


def list_rts_pp_drafts(
    path: Path,
    *,
    limit: int = 200,
    rts_profile: str | None = None,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 1000))
    if not path.is_file():
        return []
    profile = str(rts_profile or "").strip()
    with connect(path) as conn:
        if profile:
            rows = conn.execute(
                """
                SELECT purchase_number, line_key, rts_profile, ktru_code, rts_draft_id, protected_id,
                       edit_url, offer_id, status, error_text, created_at_utc, created_by,
                       fill_report_json, verify_json
                FROM rts_pp_drafts
                WHERE rts_profile = ?
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                (profile, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT purchase_number, line_key, rts_profile, ktru_code, rts_draft_id, protected_id,
                       edit_url, offer_id, status, error_text, created_at_utc, created_by,
                       fill_report_json, verify_json
                FROM rts_pp_drafts
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
    return [dict(row) for row in rows]


def delete_rts_pp_draft(
    path: Path,
    purchase_number: str,
    line_key: str,
    *,
    rts_profile: str = "",
) -> bool:
    """Remove local PP draft link (does not delete draft on RTS)."""
    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    profile = str(rts_profile or "").strip()
    if not pn or not lk or not path.is_file():
        return False
    with connect(path) as conn:
        cur = conn.execute(
            "DELETE FROM rts_pp_drafts WHERE purchase_number = ? AND line_key = ? AND rts_profile = ?",
            (pn, lk, profile),
        )
        conn.commit()
        return int(cur.rowcount or 0) > 0


def get_rts_pp_draft(
    path: Path,
    purchase_number: str,
    line_key: str,
    *,
    rts_profile: str = "",
) -> dict[str, Any] | None:
    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    profile = str(rts_profile or "").strip()
    if not pn or not lk or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT purchase_number, line_key, rts_profile, ktru_code, rts_draft_id, protected_id,
                   edit_url, offer_id, status, error_text, created_at_utc, created_by,
                   fill_report_json, verify_json
            FROM rts_pp_drafts
            WHERE purchase_number = ? AND line_key = ? AND rts_profile = ?
            """,
            (pn, lk, profile),
        ).fetchone()
    return dict(row) if row else None


def save_rts_pp_draft(
    path: Path,
    *,
    purchase_number: str,
    line_key: str,
    ktru_code: str,
    rts_draft_id: int | None,
    protected_id: str = "",
    edit_url: str = "",
    offer_id: str = "",
    status: str = "created",
    error_text: str = "",
    created_by: str = "",
    rts_profile: str = "",
    created_at_utc: datetime | None = None,
    fill_report_json: str = "",
    verify_json: str = "",
) -> None:
    created_iso = _utc_iso(created_at_utc or datetime.now(timezone.utc))
    profile = str(rts_profile or "").strip()
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO rts_pp_drafts(
              purchase_number, line_key, rts_profile, ktru_code, rts_draft_id, protected_id, edit_url,
              offer_id, status, error_text, created_at_utc, created_by, fill_report_json, verify_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(purchase_number, line_key, rts_profile) DO UPDATE SET
              ktru_code=excluded.ktru_code,
              rts_draft_id=excluded.rts_draft_id,
              protected_id=excluded.protected_id,
              edit_url=excluded.edit_url,
              offer_id=excluded.offer_id,
              status=excluded.status,
              error_text=excluded.error_text,
              created_at_utc=excluded.created_at_utc,
              created_by=excluded.created_by,
              fill_report_json=excluded.fill_report_json,
              verify_json=excluded.verify_json
            """,
            (
                purchase_number,
                line_key,
                profile,
                ktru_code,
                rts_draft_id,
                protected_id,
                edit_url,
                offer_id,
                status,
                error_text[:2000],
                created_iso,
                created_by,
                fill_report_json[:8000],
                verify_json[:32000],
            ),
        )
        conn.commit()


def record_auto_participate_attempt(
    path: Path,
    *,
    purchase_number: str,
    line_key: str,
    rts_profile: str,
    status: str,
    ktru_code: str = "",
    rts_draft_id: int | None = None,
    edit_url: str = "",
    error_text: str = "",
    created_at_utc: datetime | None = None,
) -> None:
    created_iso = _utc_iso(created_at_utc or datetime.now(timezone.utc))
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO auto_participate_attempts(
              purchase_number, line_key, rts_profile, status, ktru_code,
              rts_draft_id, edit_url, error_text, created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(purchase_number, line_key, rts_profile) DO UPDATE SET
              status=excluded.status,
              ktru_code=excluded.ktru_code,
              rts_draft_id=excluded.rts_draft_id,
              edit_url=excluded.edit_url,
              error_text=excluded.error_text,
              created_at_utc=excluded.created_at_utc
            """,
            (
                purchase_number.strip(),
                line_key.strip(),
                str(rts_profile or "").strip(),
                status.strip(),
                ktru_code.strip(),
                rts_draft_id,
                edit_url[:2000],
                error_text[:2000],
                created_iso,
            ),
        )
        conn.commit()


def get_auto_participate_attempt(
    path: Path,
    purchase_number: str,
    line_key: str,
    *,
    rts_profile: str,
) -> dict[str, Any] | None:
    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    profile = str(rts_profile or "").strip()
    if not pn or not lk or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT purchase_number, line_key, rts_profile, status, ktru_code,
                   rts_draft_id, edit_url, error_text, created_at_utc
            FROM auto_participate_attempts
            WHERE purchase_number = ? AND line_key = ? AND rts_profile = ?
            """,
            (pn, lk, profile),
        ).fetchone()
    return dict(row) if row else None


def list_auto_participate_attempts(
    path: Path,
    *,
    limit: int = 20,
    rts_profile: str | None = None,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 500))
    if not path.is_file():
        return []
    profile = str(rts_profile or "").strip()
    with connect(path) as conn:
        if profile:
            rows = conn.execute(
                """
                SELECT purchase_number, line_key, rts_profile, status, ktru_code,
                       rts_draft_id, edit_url, error_text, created_at_utc
                FROM auto_participate_attempts
                WHERE rts_profile = ?
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                (profile, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT purchase_number, line_key, rts_profile, status, ktru_code,
                       rts_draft_id, edit_url, error_text, created_at_utc
                FROM auto_participate_attempts
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
    return [dict(row) for row in rows]


def count_auto_participate_created_since(
    path: Path,
    *,
    rts_profile: str,
    since_utc: datetime,
) -> int:
    if not path.is_file():
        return 0
    since_iso = _utc_iso(since_utc)
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM auto_participate_attempts
            WHERE rts_profile = ?
              AND status = 'created'
              AND created_at_utc >= ?
            """,
            (str(rts_profile or "").strip(), since_iso),
        ).fetchone()
    return int(row["c"] if row else 0)


def auto_participate_candidates(
    path: Path,
    *,
    limit: int = 50,
    rts_profile: str,
) -> list[dict[str, Any]]:
    """HTML-loaded lines without prior auto-participate attempt for profile."""
    lim = max(1, min(int(limit), 200))
    profile = str(rts_profile or "").strip()
    if not path.is_file() or not profile:
        return []
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
              l.line_key,
              l.purchase_number,
              l.ktru_code,
              l.position_display_name,
              l.price_per_unit_text,
              l.quantity_text,
              l.line_sum_text,
              l.characteristics_count,
              l.characteristics_json,
              l.parsed_at_utc,
              p.source_url,
              p.fetched_at_utc,
              p.region,
              p.delivery_place,
              p.delivery_days,
              p.contract_execution_term,
              r.pub_date_utc,
              r.first_seen_at_utc,
              r.customer_name,
              r.price_text,
              r.stage_text
            FROM notice_html_ktru_lines l
            JOIN notice_html_pages p ON p.purchase_number = l.purchase_number AND p.ok = 1
            LEFT JOIN eis_rss_observations r ON r.reg_number = l.purchase_number
            WHERE COALESCE(l.ktru_code, '') <> ''
              AND NOT EXISTS (
                SELECT 1 FROM auto_participate_attempts a
                WHERE a.purchase_number = l.purchase_number
                  AND a.line_key = l.line_key
                  AND a.rts_profile = ?
              )
              AND NOT EXISTS (
                SELECT 1 FROM rts_pp_drafts d
                WHERE d.purchase_number = l.purchase_number
                  AND d.line_key = l.line_key
                  AND d.rts_profile = ?
                  AND d.status = 'created'
                  AND d.rts_draft_id IS NOT NULL
              )
            ORDER BY COALESCE(r.pub_date_utc, p.fetched_at_utc) DESC, l.purchase_number DESC
            LIMIT ?
            """,
            (profile, profile, lim),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        try:
            d["characteristics"] = json.loads(str(d.get("characteristics_json") or "[]"))
        except json.JSONDecodeError:
            d["characteristics"] = []
        d.pop("characteristics_json", None)
        d["html_status"] = "loaded"
        out.append(d)
    return out


def get_auto_participate_edit_url(
    path: Path,
    purchase_number: str,
    line_key: str,
    *,
    rts_profile: str,
) -> str | None:
    row = get_auto_participate_attempt(path, purchase_number, line_key, rts_profile=rts_profile)
    if not row or row.get("status") != "created":
        return None
    url = str(row.get("edit_url") or "").strip()
    return url or None


def notice_line_for_pp(path: Path, purchase_number: str, line_key: str) -> dict[str, Any] | None:
    rows = offer_matching_ktru_rows(
        path,
        limit=1,
        purchase_number=purchase_number.strip(),
        line_key=line_key.strip(),
    )
    return rows[0] if rows else None


_CODE_LIKE_RE = re.compile(r"^[0-9a-f-]{8,}$", re.IGNORECASE)


def _normalize_xml_char_row(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("display_label") or raw.get("characteristic_code"):
        return dict(raw)
    key = str(raw.get("key") or "").strip()
    val = str(raw.get("value") or "").strip()
    if not key and not val:
        return dict(raw)
    code = key if _CODE_LIKE_RE.match(key) else None
    return {
        "match_key": key,
        "characteristic_code": code,
        "display_label": key if not code else "",
        "value_display": val,
        "characteristic_kind": raw.get("characteristic_kind") or "reference",
    }


def _load_xml_chars_for_enrich(
    path: Path,
    *,
    purchase_number: str,
    ktru_code: str,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT characteristics_json, characteristics_hash
            FROM ktru_lines
            WHERE purchase_number = ? AND ktru_code = ?
            ORDER BY last_seen_at_utc DESC
            LIMIT 20
            """,
            (purchase_number, ktru_code),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            chars = json.loads(str(row["characteristics_json"] or "[]"))
        except json.JSONDecodeError:
            continue
        if not isinstance(chars, list):
            continue
        for raw in chars:
            if isinstance(raw, dict):
                out.append(_normalize_xml_char_row(raw))
    return out


def _enrich_label_keys(chars: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for raw in chars:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("characteristic_code") or "").strip()
        label = _norm_text(str(raw.get("display_label") or ""))
        if code:
            keys.add(f"code:{code}")
        if label:
            keys.add(f"name:{label}")
    return keys


def _enrich_label_overlap(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> float:
    a = _enrich_label_keys(left)
    b = _enrich_label_keys(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _enrich_group_key(raw: dict[str, Any]) -> str:
    code = str(raw.get("characteristic_code") or "").strip()
    if code:
        return f"code:{code}"
    label = _norm_text(str(raw.get("display_label") or ""))
    if label:
        return f"name:{label}"
    mk = str(raw.get("match_key") or "").strip()
    if mk and mk != "name:":
        return mk
    return ""


def _html_multi_value_group_keys(html_chars: list[dict[str, Any]]) -> set[str]:
    """Characteristics where HTML table has several rows (rowspan continuations)."""
    counts: dict[str, int] = {}
    for raw in html_chars:
        if not isinstance(raw, dict):
            continue
        key = _enrich_group_key(raw)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def enrich_notice_characteristics(
    path: Path,
    *,
    purchase_number: str,
    ktru_code: str,
    html_chars: list[dict[str, Any]],
    history_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Overlay XML metadata from ktru_lines onto HTML-parsed rows.

    XML wins for codes and labels; ``value_display`` is taken from XML only when
    HTML has a single row for that characteristic (multi-row HTML keeps each value).
    """
    if not html_chars:
        return []
    pn = str(purchase_number or "").strip()
    kc = str(ktru_code or "").strip()
    if not pn or not kc:
        return [dict(x) for x in html_chars if isinstance(x, dict)]

    xml_pool = _load_xml_chars_for_enrich(path, purchase_number=pn, ktru_code=kc)
    if history_path is not None:
        hist_rows = _load_xml_chars_for_enrich(history_path, purchase_number=pn, ktru_code=kc)
        if hist_rows:
            xml_pool = hist_rows + xml_pool

    if not xml_pool:
        return [dict(x) for x in html_chars if isinstance(x, dict)]

    xml_by_code: dict[str, dict[str, Any]] = {}
    xml_by_label: dict[str, dict[str, Any]] = {}
    xml_by_match_key: dict[str, dict[str, Any]] = {}
    for raw in xml_pool:
        code = str(raw.get("characteristic_code") or "").strip()
        label = _norm_text(str(raw.get("display_label") or ""))
        mk = str(raw.get("match_key") or "").strip()
        if code:
            xml_by_code[code] = raw
        if label:
            xml_by_label[label] = raw
        if mk:
            xml_by_match_key[mk] = raw

    multi_value_keys = _html_multi_value_group_keys(html_chars)
    overlay_keys = ("characteristic_code", "match_key", "characteristic_kind", "display_label")

    out: list[dict[str, Any]] = []
    for raw in html_chars:
        if not isinstance(raw, dict):
            continue
        merged = dict(raw)
        code = str(merged.get("characteristic_code") or "").strip()
        label = _norm_text(str(merged.get("display_label") or ""))
        mk = str(merged.get("match_key") or "").strip()
        xml_row = xml_by_code.get(code) if code else None
        if xml_row is None and mk:
            xml_row = xml_by_match_key.get(mk)
        if xml_row is None and label:
            xml_row = xml_by_label.get(label)
        if xml_row:
            group_key = _enrich_group_key(merged)
            keys = overlay_keys
            if group_key not in multi_value_keys:
                keys = overlay_keys + ("value_display",)
            for key in keys:
                val = xml_row.get(key)
                if val not in (None, ""):
                    merged[key] = val
        out.append(merged)
    return out


def shelf_purchase_rows(path: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
              r.reg_number,
              r.link,
              r.pub_date_utc,
              r.first_seen_at_utc,
              r.stage_text,
              r.customer_name,
              r.price_text,
              p.ok AS html_ok,
              p.fetched_at_utc AS html_fetched_at_utc,
              p.error_text AS html_error_text,
              COUNT(l.line_key) AS html_lines_count,
              COALESCE(SUM(l.characteristics_count), 0) AS html_characteristics_count
            FROM eis_rss_observations r
            LEFT JOIN notice_html_pages p ON p.purchase_number = r.reg_number
            LEFT JOIN notice_html_ktru_lines l ON l.purchase_number = r.reg_number
            GROUP BY r.reg_number
            ORDER BY r.pub_date_utc DESC, r.first_seen_at_utc DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        if d.get("html_fetched_at_utc"):
            d["html_status"] = "loaded" if int(d.get("html_ok") or 0) else "error"
        else:
            d["html_status"] = "not_loaded"
        d["pub_display_msk"] = _display_msk(d.get("pub_date_utc"))
        d["first_seen_display_msk"] = _display_msk(d.get("first_seen_at_utc"))
        d["html_fetched_display_msk"] = _display_msk(d.get("html_fetched_at_utc"))
        out.append(d)
    return out


def notice_html_fetch_candidates(
    path: Path,
    *,
    limit: int = 10,
    retry_errors: bool = False,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    error_sql = " OR p.ok = 0" if retry_errors else ""
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              r.reg_number AS purchase_number,
              r.link AS source_url,
              r.pub_date_utc,
              r.first_seen_at_utc,
              p.ok AS html_ok,
              p.error_text AS html_error_text,
              p.fetched_at_utc AS html_fetched_at_utc
            FROM eis_rss_observations r
            LEFT JOIN notice_html_pages p ON p.purchase_number = r.reg_number
            WHERE COALESCE(r.link, '') <> ''
              AND (p.purchase_number IS NULL{error_sql})
            ORDER BY r.pub_date_utc DESC, r.first_seen_at_utc DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(r) for r in rows]


def msk_day_bounds_utc(day: date | None = None) -> tuple[str, str]:
    """Границы календарного дня по MSK в ISO UTC (from включительно, to исключая)."""
    if day is None:
        day = datetime.now(MSK).date()
    start = datetime.combine(day, dt_time.min, tzinfo=MSK)
    end = start + timedelta(days=1)
    return _utc_iso(start), _utc_iso(end)


def _offer_matching_filter_sql(
    *,
    purchase_number: str | None = None,
    line_key: str | None = None,
    ktru_query: str | None = None,
    ktru_queries: list[str] | None = None,
    catalog_ktru_codes: list[str] | None = None,
    pub_from_utc: str | None = None,
    pub_to_utc: str | None = None,
) -> tuple[str, list[Any]]:
    where_sql = ""
    params: list[Any] = []
    if purchase_number:
        where_sql += " AND r.reg_number = ?"
        params.append(purchase_number)
    if line_key:
        where_sql += " AND l.line_key = ?"
        params.append(line_key)
    codes = [c.strip() for c in (catalog_ktru_codes or []) if c and str(c).strip()]
    if codes:
        placeholders = ",".join("?" * len(codes))
        where_sql += f" AND l.ktru_code IN ({placeholders})"
        params.extend(codes)
    queries = [q.strip() for q in (ktru_queries or []) if q.strip()]
    if ktru_query and not queries:
        queries = [ktru_query.strip()]
    if queries:
        parts: list[str] = []
        for query in queries:
            normalized_query = _norm_ktru_query(query)
            parts.append(_ktru_search_sql())
            params.extend([f"%{query.lower()}%", f"%{normalized_query}%"])
        where_sql += " AND (" + " OR ".join(parts) + ")"
    pub_col = "COALESCE(NULLIF(r.pub_date_utc, ''), r.first_seen_at_utc)"
    if pub_from_utc:
        where_sql += f" AND {pub_col} >= ?"
        params.append(pub_from_utc)
    if pub_to_utc:
        where_sql += f" AND {pub_col} < ?"
        params.append(pub_to_utc)
    return where_sql, params


def offer_matching_ktru_rows(
    path: Path,
    *,
    limit: int = 100,
    offset: int = 0,
    purchase_number: str | None = None,
    line_key: str | None = None,
    ktru_query: str | None = None,
    ktru_queries: list[str] | None = None,
    catalog_ktru_codes: list[str] | None = None,
    pub_from_utc: str | None = None,
    pub_to_utc: str | None = None,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    where_sql, params = _offer_matching_filter_sql(
        purchase_number=purchase_number,
        line_key=line_key,
        ktru_query=ktru_query,
        ktru_queries=ktru_queries,
        catalog_ktru_codes=catalog_ktru_codes,
        pub_from_utc=pub_from_utc,
        pub_to_utc=pub_to_utc,
    )
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              l.line_key,
              r.reg_number AS purchase_number,
              l.ktru_code,
              l.position_display_name,
              l.unit_text,
              l.quantity_text,
              l.price_per_unit_text,
              l.line_sum_text,
              l.characteristics_count,
              l.characteristics_json,
              l.parsed_at_utc,
              COALESCE(p.source_url, r.link) AS source_url,
              p.fetched_at_utc,
              p.ok AS html_ok,
              p.error_text AS html_error_text,
              p.contract_start_date,
              p.contract_execution_term,
              p.region,
              p.delivery_place,
              p.delivery_days,
              p.html_text,
              r.pub_date_utc,
              r.first_seen_at_utc,
              r.customer_name,
              r.price_text,
              r.stage_text
            FROM eis_rss_observations r
            LEFT JOIN notice_html_pages p ON p.purchase_number = r.reg_number
            LEFT JOIN notice_html_ktru_lines l ON l.purchase_number = r.reg_number AND p.ok = 1
            WHERE 1 = 1{where_sql}
            ORDER BY r.pub_date_utc DESC, r.first_seen_at_utc DESC, l.line_key ASC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    out: list[dict[str, Any]] = []
    html_parse_cache: dict[str, list[KtruPurchaseLine]] = {}
    for row in rows:
        d = dict(row)
        try:
            d["characteristics"] = json.loads(str(d.get("characteristics_json") or "[]"))
        except json.JSONDecodeError:
            d["characteristics"] = []
        d.pop("characteristics_json", None)
        if d.get("fetched_at_utc"):
            d["html_status"] = "loaded" if int(d.get("html_ok") or 0) else "error"
        else:
            d["html_status"] = "not_loaded"
        _apply_fresh_html_characteristics(d, parse_cache=html_parse_cache)
        context_keys = ("contract_start_date", "contract_execution_term", "region", "delivery_place", "delivery_days")
        if any(not d.get(k) for k in context_keys):
            context = parse_notice_common_info_context(str(d.get("html_text") or ""))
            for key in context_keys:
                if not d.get(key):
                    d[key] = context.get(key, "")
        d.pop("html_text", None)
        d["pub_display_msk"] = _display_msk(d.get("pub_date_utc"))
        d["first_seen_display_msk"] = _display_msk(d.get("first_seen_at_utc"))
        d["fetched_display_msk"] = _display_msk(d.get("fetched_at_utc"))
        out.append(d)
    return out


def offer_matching_ktru_count(
    path: Path,
    *,
    ktru_query: str | None = None,
    ktru_queries: list[str] | None = None,
    catalog_ktru_codes: list[str] | None = None,
    pub_from_utc: str | None = None,
    pub_to_utc: str | None = None,
) -> int:
    if not path.is_file():
        return 0
    where_sql, params = _offer_matching_filter_sql(
        ktru_query=ktru_query,
        ktru_queries=ktru_queries,
        catalog_ktru_codes=catalog_ktru_codes,
        pub_from_utc=pub_from_utc,
        pub_to_utc=pub_to_utc,
    )
    with connect(path) as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt
            FROM eis_rss_observations r
            LEFT JOIN notice_html_pages p ON p.purchase_number = r.reg_number
            LEFT JOIN notice_html_ktru_lines l ON l.purchase_number = r.reg_number AND p.ok = 1
            WHERE 1 = 1{where_sql}
            """,
            params,
        ).fetchone()
    return int(row["cnt"] if row is not None else 0)


def _period_clause(days: int, alias: str = "n") -> tuple[str, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return f"{alias}.detected_at_utc >= ?", _utc_iso(cutoff)


def top_ktru(path: Path, *, days: int = 30, limit: int = 20, region: str | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    where, cutoff = _period_clause(days)
    params: list[Any] = [cutoff]
    region_sql = ""
    if region:
        region_sql = " AND n.org_region = ?"
        params.append(region)
    params.append(max(1, int(limit)))
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              k.ktru_code,
              MAX(k.position_display_name) AS position_display_name,
              COUNT(*) AS line_count,
              COUNT(DISTINCT k.purchase_key) AS purchase_count,
              COUNT(DISTINCT k.characteristics_hash) AS characteristic_sets,
              MAX(n.detected_at_utc) AS last_seen_at_utc
            FROM ktru_lines k
            JOIN notice_events n ON n.purchase_key = k.purchase_key
            WHERE {where} AND n.shelf_confidence = 'strong'{region_sql}
            GROUP BY k.ktru_code
            ORDER BY purchase_count DESC, line_count DESC, k.ktru_code ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def top_ktru_characteristics(
    path: Path,
    *,
    days: int = 30,
    limit: int = 20,
    ktru_code: str | None = None,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    where, cutoff = _period_clause(days)
    params: list[Any] = [cutoff]
    ktru_sql = ""
    if ktru_code:
        ktru_sql = " AND k.ktru_code = ?"
        params.append(ktru_code)
    params.append(max(1, int(limit)))
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              k.ktru_code,
              MAX(k.position_display_name) AS position_display_name,
              k.characteristics_hash,
              MAX(k.characteristics_json) AS characteristics_json,
              MAX(k.characteristics_count) AS characteristics_count,
              COUNT(*) AS line_count,
              COUNT(DISTINCT k.purchase_key) AS purchase_count,
              MAX(n.detected_at_utc) AS last_seen_at_utc
            FROM ktru_lines k
            JOIN notice_events n ON n.purchase_key = k.purchase_key
            WHERE {where} AND n.shelf_confidence = 'strong'{ktru_sql}
            GROUP BY k.ktru_code, k.characteristics_hash
            ORDER BY purchase_count DESC, line_count DESC, k.ktru_code ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        try:
            d["characteristics_preview"] = json.loads(str(d.get("characteristics_json") or "[]"))[:4]
        except json.JSONDecodeError:
            d["characteristics_preview"] = []
        out.append(d)
    return out


def _html_observed_period_clause(days: int) -> tuple[str, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return "COALESCE(r.first_seen_at_utc, p.fetched_at_utc) >= ?", _utc_iso(cutoff)


def _trade_mark_from_characteristics_json(raw_json: str) -> str:
    try:
        raw = json.loads(raw_json or "[]")
    except json.JSONDecodeError:
        return ""
    if not isinstance(raw, list):
        return ""
    for ch in raw:
        if not isinstance(ch, dict):
            continue
        label = str(ch.get("display_label") or ch.get("characteristic_name") or "").strip().lower()
        if "товарный знак" in label or label == "торговая марка":
            value = str(ch.get("value_display") or ch.get("value") or "").strip()
            if value:
                return value
    return ""


def _html_characteristics_preview(raw_json: str, limit: int = 4) -> list[dict[str, str]]:
    try:
        raw = json.loads(raw_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for ch in raw:
        if not isinstance(ch, dict):
            continue
        key = str(ch.get("display_label") or ch.get("characteristic_name") or ch.get("match_key") or "").strip()
        value = str(ch.get("value_display") or ch.get("value") or "").strip()
        if key or value:
            out.append({"key": key, "value": value})
        if len(out) >= limit:
            break
    return out


def _html_ktru_rows_for_period(path: Path, *, days: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    where, cutoff = _html_observed_period_clause(days)
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              l.line_key,
              l.purchase_number,
              l.ktru_code,
              l.position_display_name,
              l.characteristics_count,
              l.characteristics_json,
              p.fetched_at_utc,
              r.first_seen_at_utc AS rss_first_seen_at_utc,
              COALESCE(r.first_seen_at_utc, p.fetched_at_utc) AS observed_at_utc
            FROM notice_html_ktru_lines l
            JOIN notice_html_pages p ON p.purchase_number = l.purchase_number
            LEFT JOIN eis_rss_observations r ON r.reg_number = l.purchase_number
            WHERE p.ok = 1
              AND COALESCE(l.ktru_code, '') <> ''
              AND {where}
            ORDER BY observed_at_utc DESC, l.purchase_number DESC
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def _history_period_bounds_utc(date_from: date, date_to: date) -> tuple[str, str]:
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    from_iso, _ = msk_day_bounds_utc(date_from)
    _, to_exclusive = msk_day_bounds_utc(date_to)
    return from_iso, to_exclusive


_HISTORY_LINES_FROM = """
FROM ktru_lines l
JOIN notice_events n ON n.purchase_key = l.purchase_key
"""

_HISTORY_OBSERVED_AT_EXPR = (
    "COALESCE(NULLIF(n.publish_dt_utc, ''), n.detected_at_utc)"
)

_MONTH_LABELS_RU = (
    "янв",
    "фев",
    "мар",
    "апр",
    "май",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)


def _parse_rub_amount(text: str) -> float | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    cleaned = raw.replace("\u00a0", "").replace(" ", "")
    for suffix in ("RUB", "руб.", "руб", "₽"):
        if cleaned.lower().endswith(suffix.lower()):
            cleaned = cleaned[: -len(suffix)].strip()
    cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value >= 0 else None


def _history_line_sum_sql_expr(column: str = "l.line_sum_text") -> str:
    stripped = (
        f"REPLACE(REPLACE(COALESCE({column}, ''), ' ', ''), char(160), '')"
    )
    normalized = f"REPLACE({stripped}, ',', '.')"
    return f"CAST(NULLIF({normalized}, '') AS REAL)"


def _format_decimal_ru(value: float, *, digits: int = 1) -> str:
    formatted = f"{float(value):.{digits}f}"
    return formatted.replace(".", ",")


def _format_rub_compact(value: float | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    if amount >= 1_000_000_000:
        return f"{_format_decimal_ru(amount / 1_000_000_000)} млрд ₽"
    if amount >= 1_000_000:
        return f"{_format_decimal_ru(amount / 1_000_000)} млн ₽"
    if amount >= 1_000:
        return f"{_format_decimal_ru(amount / 1_000)} тыс ₽"
    if abs(amount - round(amount)) < 0.005:
        return f"{int(round(amount)):,}".replace(",", " ") + " ₽"
    return f"{_format_decimal_ru(amount, digits=2)} ₽"


def _format_rub_table(value: float | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    if abs(amount - round(amount)) < 0.005:
        body = f"{int(round(amount)):,}".replace(",", " ")
    else:
        body = _format_decimal_ru(amount, digits=2)
    return f"{body} ₽"


def _month_label_ru(year_month: str) -> str:
    text = str(year_month or "").strip()
    if len(text) < 7 or text[4] != "-":
        return text or "—"
    try:
        month_idx = int(text[5:7])
        year = text[:4]
        if 1 <= month_idx <= 12:
            return f"{_MONTH_LABELS_RU[month_idx - 1]} {year}"
    except ValueError:
        pass
    return text


def _dashboard_avg_per_purchase(total_sum: float | None, purchase_count: int) -> float | None:
    if not purchase_count or total_sum is None:
        return None
    return float(total_sum) / float(purchase_count)


def _dashboard_money_fields(
    *,
    total_sum_rub: float | None,
    purchase_count: int,
) -> dict[str, Any]:
    avg = _dashboard_avg_per_purchase(total_sum_rub, purchase_count)
    return {
        "total_sum_rub": total_sum_rub,
        "total_sum_display": _format_rub_table(total_sum_rub),
        "avg_per_purchase_rub": avg,
        "avg_per_purchase_display": _format_rub_table(avg),
        "purchase_count": purchase_count,
    }


def _history_period_filters(
    *,
    document_type_44: str,
    from_iso: str,
    to_exclusive: str,
    ktru_code: str | None = None,
    ktru_query: str | None = None,
) -> tuple[str, list[Any]]:
    clauses = [
        "n.document_type_44 = ?",
        "COALESCE(l.ktru_code, '') <> ''",
        "COALESCE(NULLIF(n.publish_dt_utc, ''), n.detected_at_utc) >= ?",
        "COALESCE(NULLIF(n.publish_dt_utc, ''), n.detected_at_utc) < ?",
    ]
    params: list[Any] = [document_type_44, from_iso, to_exclusive]
    code = (ktru_code or "").strip()
    query = (ktru_query or "").strip()
    if code:
        clauses.append("l.ktru_code = ?")
        params.append(code)
    elif query:
        normalized = _norm_ktru_query(query)
        clauses.append(
            f"({_ktru_search_sql('l.ktru_code')} OR LOWER(COALESCE(l.position_display_name, '')) LIKE ?)"
        )
        params.extend([f"%{query.lower()}%", f"%{normalized}%", f"%{query.lower()}%"])
    return " AND ".join(clauses), params


def _history_period_purchase_count(
    path: Path,
    *,
    date_from: date,
    date_to: date,
    document_type_44: str = HISTORY_DOCUMENT_TYPE_44,
    ktru_code: str | None = None,
    ktru_query: str | None = None,
) -> int:
    if not path.is_file():
        return 0
    from eis_history_db import history_connect

    from_iso, to_exclusive = _history_period_bounds_utc(date_from, date_to)
    where_sql, params = _history_period_filters(
        document_type_44=document_type_44,
        from_iso=from_iso,
        to_exclusive=to_exclusive,
        ktru_code=ktru_code,
        ktru_query=ktru_query,
    )
    with history_connect(path) as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT l.purchase_number) AS cnt
            {_HISTORY_LINES_FROM}
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
    return int(row["cnt"]) if row else 0


def _history_top_ktru_row_dict(row: sqlite3.Row | dict[str, Any], *, total_purchases: int) -> dict[str, Any] | None:
    code = str(row["ktru_code"] or "").strip()
    if not code:
        return None
    purchase_count = int(row["purchase_count"] or 0)
    return {
        "ktru_code": code,
        "position_display_name": str(row["position_display_name"] or ""),
        "line_count": int(row["line_count"] or 0),
        "purchase_count": purchase_count,
        "characteristic_sets": int(row["characteristic_sets"] or 0),
        "purchase_share_pct": _html_pct(purchase_count, total_purchases),
        "last_seen_at_utc": str(row["last_seen_at_utc"] or ""),
        "last_seen_display_msk": _display_msk(row["last_seen_at_utc"]),
    }


def _history_period_overview(
    path: Path,
    *,
    date_from: date,
    date_to: date,
    document_type_44: str = HISTORY_DOCUMENT_TYPE_44,
    ktru_code: str | None = None,
    ktru_query: str | None = None,
    top_ktru_limit: int = 30,
) -> tuple[int, int, list[dict[str, Any]]]:
    """One DB round-trip: purchases in period, distinct KTRU count, top-KTRU rows."""
    if not path.is_file():
        return 0, 0, []
    from eis_history_db import history_connect

    from_iso, to_exclusive = _history_period_bounds_utc(date_from, date_to)
    where_sql, params = _history_period_filters(
        document_type_44=document_type_44,
        from_iso=from_iso,
        to_exclusive=to_exclusive,
        ktru_code=ktru_code,
        ktru_query=ktru_query,
    )
    top_params = list(params) + [max(1, int(top_ktru_limit))]
    with history_connect(path) as conn:
        summary = conn.execute(
            f"""
            SELECT
              COUNT(DISTINCT l.purchase_number) AS total_purchases,
              COUNT(DISTINCT l.ktru_code) AS total_ktru_codes
            {_HISTORY_LINES_FROM}
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        top_rows = conn.execute(
            f"""
            SELECT
              l.ktru_code,
              MAX(l.position_display_name) AS position_display_name,
              COUNT(*) AS line_count,
              COUNT(DISTINCT l.purchase_number) AS purchase_count,
              COUNT(DISTINCT l.characteristics_hash) AS characteristic_sets,
              MAX(COALESCE(NULLIF(n.publish_dt_utc, ''), n.detected_at_utc)) AS last_seen_at_utc
            {_HISTORY_LINES_FROM}
            WHERE {where_sql}
            GROUP BY l.ktru_code
            ORDER BY purchase_count DESC, line_count DESC, l.ktru_code ASC
            LIMIT ?
            """,
            top_params,
        ).fetchall()
    total_purchases = int(summary["total_purchases"] or 0) if summary else 0
    total_ktru_codes = int(summary["total_ktru_codes"] or 0) if summary else 0
    top_ktru: list[dict[str, Any]] = []
    for row in top_rows:
        item = _history_top_ktru_row_dict(row, total_purchases=total_purchases)
        if item:
            top_ktru.append(item)
    return total_purchases, total_ktru_codes, top_ktru


def _history_sql_top_ktru(
    path: Path,
    *,
    date_from: date,
    date_to: date,
    document_type_44: str = HISTORY_DOCUMENT_TYPE_44,
    ktru_code: str | None = None,
    ktru_query: str | None = None,
    limit: int = 30,
    total_purchases: int | None = None,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    from eis_history_db import history_connect

    from_iso, to_exclusive = _history_period_bounds_utc(date_from, date_to)
    where_sql, params = _history_period_filters(
        document_type_44=document_type_44,
        from_iso=from_iso,
        to_exclusive=to_exclusive,
        ktru_code=ktru_code,
        ktru_query=ktru_query,
    )
    total = (
        int(total_purchases)
        if total_purchases is not None
        else _history_period_purchase_count(
            path,
            date_from=date_from,
            date_to=date_to,
            document_type_44=document_type_44,
            ktru_query=ktru_query,
            ktru_code=ktru_code,
        )
    )
    params.append(max(1, int(limit)))
    with history_connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              l.ktru_code,
              MAX(l.position_display_name) AS position_display_name,
              COUNT(*) AS line_count,
              COUNT(DISTINCT l.purchase_number) AS purchase_count,
              COUNT(DISTINCT l.characteristics_hash) AS characteristic_sets,
              MAX(COALESCE(NULLIF(n.publish_dt_utc, ''), n.detected_at_utc)) AS last_seen_at_utc
            {_HISTORY_LINES_FROM}
            WHERE {where_sql}
            GROUP BY l.ktru_code
            ORDER BY purchase_count DESC, line_count DESC, l.ktru_code ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _history_top_ktru_row_dict(row, total_purchases=total)
        if item:
            out.append(item)
    return out


_HISTORY_ANALYTICS_CACHE_VERSION = 1
_HISTORY_ANALYTICS_MEMORY: dict[str, tuple[float, dict[str, Any]]] = {}
_HISTORY_ANALYTICS_MEMORY_TTL_SEC = 180.0
_HISTORY_ANALYTICS_MEMORY_MAX_ENTRIES = 64


def _history_db_cache_revision(path: Path) -> tuple[str, int, int]:
    """Stable cache token: changes only when the DB content changes."""
    if not path.is_file():
        return (str(path.resolve()), 0, 0)
    from eis_history_db import history_connect

    st = path.stat()
    with history_connect(path) as conn:
        row = conn.execute("PRAGMA data_version").fetchone()
    data_version = int(row[0]) if row else 0
    return (str(path.resolve()), int(st.st_size), data_version)


def _history_analytics_cache_dir(path: Path) -> Path:
    import os

    raw = os.environ.get("EIS_HISTORY_ANALYTICS_CACHE_DIR", "").strip()
    base = Path(raw) if raw else path.parent / ".history_analytics_cache"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _history_analytics_cache_digest(key: tuple[Any, ...]) -> str:
    blob = json.dumps(key, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _history_analytics_cache_path(path: Path, digest: str) -> Path:
    return _history_analytics_cache_dir(path) / f"{digest}.json"


def _history_analytics_cache_get(path: Path, key: tuple[Any, ...]) -> dict[str, Any] | None:
    digest = _history_analytics_cache_digest(key)
    entry = _HISTORY_ANALYTICS_MEMORY.get(digest)
    if entry:
        ts, payload = entry
        if time.monotonic() - ts <= _HISTORY_ANALYTICS_MEMORY_TTL_SEC:
            return deepcopy(payload)
        _HISTORY_ANALYTICS_MEMORY.pop(digest, None)
    cache_file = _history_analytics_cache_path(path, digest)
    if not cache_file.is_file():
        return None
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if int(raw.get("v") or 0) != _HISTORY_ANALYTICS_CACHE_VERSION:
        return None
    if str(raw.get("digest") or "") != digest:
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return None
    _HISTORY_ANALYTICS_MEMORY[digest] = (time.monotonic(), deepcopy(payload))
    return deepcopy(payload)


def _history_analytics_cache_set(
    path: Path, key: tuple[Any, ...], payload: dict[str, Any]
) -> None:
    digest = _history_analytics_cache_digest(key)
    if len(_HISTORY_ANALYTICS_MEMORY) >= _HISTORY_ANALYTICS_MEMORY_MAX_ENTRIES:
        oldest = min(_HISTORY_ANALYTICS_MEMORY, key=lambda k: _HISTORY_ANALYTICS_MEMORY[k][0])
        _HISTORY_ANALYTICS_MEMORY.pop(oldest, None)
    _HISTORY_ANALYTICS_MEMORY[digest] = (time.monotonic(), deepcopy(payload))
    cache_file = _history_analytics_cache_path(path, digest)
    try:
        cache_file.write_text(
            json.dumps(
                {
                    "v": _HISTORY_ANALYTICS_CACHE_VERSION,
                    "digest": digest,
                    "payload": payload,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _history_summary_cache_key(
    path: Path,
    *,
    date_from: date,
    date_to: date,
    ktru_code: str | None,
    ktru_query: str | None,
    top_ktru_limit: int,
    top_sets_limit: int,
    top_chars_limit: int,
    top_values_limit: int,
) -> tuple[Any, ...]:
    return (
        "analysis_v2",
        *_history_db_cache_revision(path),
        date_from.isoformat(),
        date_to.isoformat(),
        (ktru_code or "").strip(),
        (ktru_query or "").strip(),
        int(top_ktru_limit),
        int(top_sets_limit),
        int(top_chars_limit),
        int(top_values_limit),
    )


def _history_ktru_rows_for_period(
    path: Path,
    *,
    date_from: date,
    date_to: date,
    document_type_44: str = HISTORY_DOCUMENT_TYPE_44,
    ktru_code: str | None = None,
    ktru_query: str | None = None,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    from eis_history_db import history_connect

    from_iso, to_exclusive = _history_period_bounds_utc(date_from, date_to)
    where_sql, params = _history_period_filters(
        document_type_44=document_type_44,
        from_iso=from_iso,
        to_exclusive=to_exclusive,
        ktru_code=ktru_code,
        ktru_query=ktru_query,
    )
    with history_connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              l.line_key,
              l.purchase_number,
              l.ktru_code,
              l.position_display_name,
              l.characteristics_count,
              l.characteristics_json,
              COALESCE(NULLIF(n.publish_dt_utc, ''), n.detected_at_utc) AS observed_at_utc
            {_HISTORY_LINES_FROM}
            WHERE {where_sql}
            ORDER BY observed_at_utc DESC, l.purchase_number DESC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def html_top_ktru(path: Path, *, days: int = 30, limit: int = 15) -> list[dict[str, Any]]:
    rows = _html_ktru_rows_for_period(path, days=days)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("ktru_code") or "").strip()
        if not code:
            continue
        chars = row.get("characteristics_json") or "[]"
        try:
            chars_list = json.loads(str(chars or "[]"))
        except json.JSONDecodeError:
            chars_list = []
        digest, _canonical = characteristics_fingerprint(chars_list if isinstance(chars_list, list) else [])
        item = grouped.setdefault(
            code,
            {
                "ktru_code": code,
                "position_display_name": row.get("position_display_name") or "",
                "line_count": 0,
                "purchase_numbers": set(),
                "characteristics_hashes": set(),
                "last_seen_at_utc": "",
            },
        )
        item["line_count"] += 1
        item["purchase_numbers"].add(str(row.get("purchase_number") or ""))
        item["characteristics_hashes"].add(digest)
        if not item["position_display_name"] and row.get("position_display_name"):
            item["position_display_name"] = row.get("position_display_name")
        observed = str(row.get("observed_at_utc") or "")
        if observed > str(item.get("last_seen_at_utc") or ""):
            item["last_seen_at_utc"] = observed

    out: list[dict[str, Any]] = []
    for item in grouped.values():
        purchase_numbers = {x for x in item.pop("purchase_numbers") if x}
        characteristic_hashes = item.pop("characteristics_hashes")
        item["purchase_count"] = len(purchase_numbers)
        item["characteristic_sets"] = len(characteristic_hashes)
        out.append(item)
    out.sort(key=lambda x: (-int(x["purchase_count"]), -int(x["line_count"]), str(x["ktru_code"])))
    return out[: max(1, int(limit))]


def html_top_ktru_characteristics(path: Path, *, days: int = 30, limit: int = 10) -> list[dict[str, Any]]:
    rows = _html_ktru_rows_for_period(path, days=days)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("ktru_code") or "").strip()
        if not code:
            continue
        raw_json = str(row.get("characteristics_json") or "[]")
        try:
            chars = json.loads(raw_json)
        except json.JSONDecodeError:
            chars = []
        digest, canonical = characteristics_fingerprint(chars if isinstance(chars, list) else [])
        key = (code, digest)
        item = grouped.setdefault(
            key,
            {
                "ktru_code": code,
                "position_display_name": row.get("position_display_name") or "",
                "characteristics_hash": digest,
                "characteristics_json": raw_json,
                "characteristics_count": int(row.get("characteristics_count") or 0),
                "characteristics_canonical_json": canonical,
                "line_count": 0,
                "purchase_numbers": set(),
                "last_seen_at_utc": "",
            },
        )
        item["line_count"] += 1
        item["purchase_numbers"].add(str(row.get("purchase_number") or ""))
        if int(row.get("characteristics_count") or 0) > int(item.get("characteristics_count") or 0):
            item["characteristics_count"] = int(row.get("characteristics_count") or 0)
            item["characteristics_json"] = raw_json
        observed = str(row.get("observed_at_utc") or "")
        if observed > str(item.get("last_seen_at_utc") or ""):
            item["last_seen_at_utc"] = observed

    out: list[dict[str, Any]] = []
    for item in grouped.values():
        purchase_numbers = {x for x in item.pop("purchase_numbers") if x}
        item["purchase_count"] = len(purchase_numbers)
        item["characteristics_preview"] = _html_characteristics_preview(str(item.get("characteristics_json") or "[]"))
        out.append(item)
    out.sort(key=lambda x: (-int(x["purchase_count"]), -int(x["line_count"]), str(x["ktru_code"])))
    return out[: max(1, int(limit))]


def html_analytics_summary(path: Path, *, days: int = 30) -> dict[str, Any]:
    if not path.is_file():
        return {"db_exists": False, "days": days, "top_ktru": [], "top_characteristics": []}
    return {
        "db_exists": True,
        "days": days,
        "top_ktru": html_top_ktru(path, days=days, limit=15),
        "top_characteristics": html_top_ktru_characteristics(path, days=days, limit=10),
    }


def _html_pct(part: int, whole: int, *, digits: int = 1) -> float | None:
    if whole <= 0:
        return None
    return round(100.0 * float(part) / float(whole), digits)


def _html_characteristic_pairs(chars: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    pairs: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ch in chars:
        if not isinstance(ch, dict):
            continue
        key_display = str(ch.get("display_label") or ch.get("characteristic_name") or ch.get("match_key") or "").strip()
        value_display = str(ch.get("value_display") or ch.get("value") or "").strip()
        key_raw = str(ch.get("characteristic_code") or "").strip() or key_display or str(ch.get("match_key") or "")
        key_norm = _norm_text(key_raw)
        value_norm = _norm_text(value_display)
        if not key_norm and not value_norm:
            continue
        dedup = (key_norm, value_norm)
        if dedup in seen:
            continue
        seen.add(dedup)
        pairs.append((key_norm, value_norm, key_display or key_norm, value_display or value_norm))
    return pairs


def _html_build_period_aggregates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_purchases: set[str] = set()
    by_ktru: dict[str, dict[str, Any]] = {}
    by_ktru_set: dict[tuple[str, str], dict[str, Any]] = {}
    by_ktru_char: dict[tuple[str, str, str], dict[str, Any]] = {}
    char_purchases: dict[tuple[str, str], set[str]] = {}

    for row in rows:
        code = str(row.get("ktru_code") or "").strip()
        purchase = str(row.get("purchase_number") or "").strip()
        if not code or not purchase:
            continue
        total_purchases.add(purchase)
        try:
            chars_list = json.loads(str(row.get("characteristics_json") or "[]"))
        except json.JSONDecodeError:
            chars_list = []
        if not isinstance(chars_list, list):
            chars_list = []
        digest, _canonical = characteristics_fingerprint(chars_list if isinstance(chars_list, list) else [])
        observed = str(row.get("observed_at_utc") or "")

        ktru_item = by_ktru.setdefault(
            code,
            {
                "ktru_code": code,
                "position_display_name": "",
                "line_count": 0,
                "purchase_numbers": set(),
                "characteristics_hashes": set(),
                "last_seen_at_utc": "",
            },
        )
        ktru_item["line_count"] += 1
        ktru_item["purchase_numbers"].add(purchase)
        ktru_item["characteristics_hashes"].add(digest)
        if not ktru_item["position_display_name"] and row.get("position_display_name"):
            ktru_item["position_display_name"] = str(row.get("position_display_name") or "")
        if observed > str(ktru_item.get("last_seen_at_utc") or ""):
            ktru_item["last_seen_at_utc"] = observed

        set_item = by_ktru_set.setdefault(
            (code, digest),
            {
                "ktru_code": code,
                "characteristics_hash": digest,
                "characteristics_json": str(row.get("characteristics_json") or "[]"),
                "characteristics_count": int(row.get("characteristics_count") or 0),
                "line_count": 0,
                "purchase_numbers": set(),
                "last_seen_at_utc": "",
            },
        )
        set_item["line_count"] += 1
        set_item["purchase_numbers"].add(purchase)
        if int(row.get("characteristics_count") or 0) > int(set_item.get("characteristics_count") or 0):
            set_item["characteristics_count"] = int(row.get("characteristics_count") or 0)
            set_item["characteristics_json"] = str(row.get("characteristics_json") or "[]")
        if observed > str(set_item.get("last_seen_at_utc") or ""):
            set_item["last_seen_at_utc"] = observed

        for key_norm, value_norm, key_display, value_display in _html_characteristic_pairs(chars_list):
            char_key = (code, key_norm, value_norm)
            char_item = by_ktru_char.setdefault(
                char_key,
                {
                    "ktru_code": code,
                    "char_key": key_norm,
                    "char_key_display": key_display,
                    "value_norm": value_norm,
                    "value_display": value_display,
                    "line_count": 0,
                    "purchase_numbers": set(),
                },
            )
            char_item["line_count"] += 1
            char_item["purchase_numbers"].add(purchase)
            char_purchases.setdefault((code, key_norm), set()).add(purchase)

    ktru_purchase_totals = {
        code: len({x for x in item["purchase_numbers"] if x})
        for code, item in by_ktru.items()
    }
    return {
        "total_purchases": len(total_purchases),
        "by_ktru": by_ktru,
        "by_ktru_set": by_ktru_set,
        "by_ktru_char": by_ktru_char,
        "char_purchases": char_purchases,
        "ktru_purchase_totals": ktru_purchase_totals,
    }


def _html_rows_match_query(row: dict[str, Any], query: str) -> bool:
    q = _norm_text(query)
    if not q:
        return True
    code = _norm_text(row.get("ktru_code"))
    name = _norm_text(row.get("position_display_name"))
    return q in code or q in name or code.startswith(q) or name.startswith(q)


def _html_finalize_top_ktru(aggregates: dict[str, Any], *, total_purchases: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in aggregates["by_ktru"].values():
        purchase_numbers = {x for x in item.get("purchase_numbers") or set() if x}
        characteristic_hashes = set(item.get("characteristics_hashes") or set())
        purchase_count = len(purchase_numbers)
        out.append(
            {
                **item,
                "purchase_count": purchase_count,
                "characteristic_sets": len(characteristic_hashes),
                "purchase_share_pct": _html_pct(purchase_count, total_purchases),
                "last_seen_display_msk": _display_msk(item.get("last_seen_at_utc")),
            }
        )
    out.sort(key=lambda x: (-int(x["purchase_count"]), -int(x["line_count"]), str(x["ktru_code"])))
    return out


HTML_V2_CONSENSUS_MIN_PCT = 50.0


def _html_preview_short(preview: list[dict[str, Any]], *, limit: int = 3) -> str:
    parts: list[str] = []
    for item in preview or []:
        if len(parts) >= limit:
            break
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        val = str(item.get("value") or "").strip()
        if key and val:
            parts.append(f"{key}: {val}")
        elif val:
            parts.append(val)
    return "; ".join(parts)


def _html_enrich_characteristic_sets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
        row["preview_short"] = _html_preview_short(row.get("characteristics_preview") or [])
    return rows


def _html_build_typical_profile(
    char_values: list[dict[str, Any]],
    *,
    consensus_min_pct: float = HTML_V2_CONSENSUS_MIN_PCT,
) -> list[dict[str, Any]]:
    profile: list[dict[str, Any]] = []
    for ch in char_values:
        top_values = ch.get("top_values") or []
        if not top_values:
            continue
        lead = top_values[0]
        share = lead.get("value_share_within_char_pct")
        has_consensus = share is not None and float(share) >= float(consensus_min_pct)
        profile.append(
            {
                "char_key": ch.get("char_key"),
                "char_key_display": ch.get("char_key_display") or ch.get("char_key"),
                "value_display": lead.get("value_display"),
                "purchase_share_within_ktru_pct": ch.get("purchase_share_within_ktru_pct"),
                "value_share_within_char_pct": share,
                "has_consensus": has_consensus,
            }
        )
    profile.sort(
        key=lambda row: (
            not bool(row.get("has_consensus")),
            -(float(row["value_share_within_char_pct"]) if row.get("value_share_within_char_pct") is not None else -1.0),
            str(row.get("char_key_display") or ""),
        )
    )
    return profile


def _html_finalize_characteristic_sets(
    aggregates: dict[str, Any],
    *,
    ktru_code: str,
    limit: int,
) -> list[dict[str, Any]]:
    ktru_total = int(aggregates["ktru_purchase_totals"].get(ktru_code) or 0)
    rows: list[dict[str, Any]] = []
    for (code, digest), item in aggregates["by_ktru_set"].items():
        if code != ktru_code:
            continue
        purchase_numbers = {x for x in item.get("purchase_numbers") or set() if x}
        purchase_count = len(purchase_numbers)
        rows.append(
            {
                "ktru_code": code,
                "characteristics_hash": digest,
                "characteristics_count": int(item.get("characteristics_count") or 0),
                "characteristics_preview": _html_characteristics_preview(str(item.get("characteristics_json") or "[]")),
                "purchase_count": purchase_count,
                "line_count": int(item.get("line_count") or 0),
                "set_share_within_ktru_pct": _html_pct(purchase_count, ktru_total),
                "last_seen_display_msk": _display_msk(item.get("last_seen_at_utc")),
            }
        )
    rows.sort(key=lambda x: (-int(x["purchase_count"]), -int(x["line_count"]), str(x["characteristics_hash"])))
    return _html_enrich_characteristic_sets(rows[: max(1, int(limit))])


def _html_jaccard_similarity(
    left: frozenset[tuple[str, str]],
    right: frozenset[tuple[str, str]],
) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    inter = len(left & right)
    union = len(left | right)
    return float(inter) / float(union) if union else 0.0


def _html_order_purchases_by_similarity(purchases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(purchases) <= 1:
        return list(purchases)
    digest_counts: dict[str, int] = {}
    for item in purchases:
        digest = str(item.get("characteristics_hash") or "")
        digest_counts[digest] = digest_counts.get(digest, 0) + 1

    remaining = list(purchases)
    ordered: list[dict[str, Any]] = []

    def _seed_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        digest = str(item.get("characteristics_hash") or "")
        return (
            -digest_counts.get(digest, 0),
            -len(item.get("pairs") or frozenset()),
            str(item.get("purchase_number") or ""),
        )

    current = min(remaining, key=_seed_sort_key)
    remaining.remove(current)
    ordered.append(current)
    while remaining:
        current_pairs = current.get("pairs") or frozenset()
        nxt = min(
            remaining,
            key=lambda item: (
                -_html_jaccard_similarity(current_pairs, item.get("pairs") or frozenset()),
                -digest_counts.get(str(item.get("characteristics_hash") or ""), 0),
                -len(item.get("pairs") or frozenset()),
                str(item.get("purchase_number") or ""),
            ),
        )
        remaining.remove(nxt)
        ordered.append(nxt)
        current = nxt
    return ordered


def _html_collect_ktru_purchases(
    rows: list[dict[str, Any]],
    *,
    ktru_code: str,
    max_purchases: int | None = 120,
) -> list[dict[str, Any]]:
    by_purchase: dict[str, dict[str, Any]] = {}
    cap = max(1, int(max_purchases)) if max_purchases is not None else None
    for row in rows:
        code = str(row.get("ktru_code") or "").strip()
        purchase = str(row.get("purchase_number") or "").strip()
        if code != ktru_code or not purchase:
            continue
        if cap is not None and purchase not in by_purchase and len(by_purchase) >= cap:
            continue
        try:
            chars_list = json.loads(str(row.get("characteristics_json") or "[]"))
        except json.JSONDecodeError:
            chars_list = []
        if not isinstance(chars_list, list):
            chars_list = []
        entry = by_purchase.setdefault(
            purchase,
            {
                "purchase_number": purchase,
                "pairs": set(),
                "values": {},
                "key_displays": {},
                "chars_list": [],
            },
        )
        entry["chars_list"].extend(ch for ch in chars_list if isinstance(ch, dict))
        for key_norm, value_norm, key_display, value_display in _html_characteristic_pairs(chars_list):
            entry["pairs"].add((key_norm, value_norm))
            entry["values"][key_norm] = value_display
            entry["key_displays"][key_norm] = key_display

    out: list[dict[str, Any]] = []
    for purchase, entry in by_purchase.items():
        digest, _canonical = characteristics_fingerprint(entry["chars_list"])
        out.append(
            {
                "purchase_number": purchase,
                "characteristics_hash": digest,
                "pairs": frozenset(entry["pairs"]),
                "values": dict(entry["values"]),
                "key_displays": dict(entry["key_displays"]),
            }
        )
    return out


def _html_collapse_matrix_columns(ordered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for item in ordered:
        digest = str(item.get("characteristics_hash") or "")
        purchase = str(item.get("purchase_number") or "")
        if columns and columns[-1]["characteristics_hash"] == digest:
            prev = columns[-1]
            prev["collapsed_count"] = int(prev.get("collapsed_count") or 1) + 1
            prev["purchase_numbers"].append(purchase)
        else:
            columns.append(
                {
                    "characteristics_hash": digest,
                    "collapsed_count": 1,
                    "purchase_numbers": [purchase] if purchase else [],
                    "values": dict(item.get("values") or {}),
                    "key_displays": dict(item.get("key_displays") or {}),
                }
            )
    for idx, col in enumerate(columns, start=1):
        col["ordinal"] = idx
        col["purchase_number"] = (col.get("purchase_numbers") or [""])[0]
    return columns


def _html_build_ktru_purchase_matrix(
    rows: list[dict[str, Any]],
    *,
    ktru_code: str,
    max_columns: int = 40,
    max_purchases: int | None = 120,
) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "columns": [],
        "characteristics": [],
        "purchase_count": 0,
        "purchases_shown": 0,
        "truncated": False,
        "max_columns": max_columns,
    }
    purchases = _html_collect_ktru_purchases(rows, ktru_code=ktru_code, max_purchases=max_purchases)
    if not purchases:
        return empty

    ordered = _html_order_purchases_by_similarity(purchases)
    collapsed_all = _html_collapse_matrix_columns(ordered)
    truncated = len(collapsed_all) > max_columns
    collapsed = collapsed_all[: max(1, int(max_columns))] if truncated else collapsed_all
    purchases_shown = sum(int(col.get("collapsed_count") or 1) for col in collapsed)

    char_freq: dict[str, int] = {}
    key_displays: dict[str, str] = {}
    for col in collapsed:
        weight = int(col.get("collapsed_count") or 1)
        for key_norm in col.get("values") or {}:
            char_freq[key_norm] = char_freq.get(key_norm, 0) + weight
            key_displays.setdefault(key_norm, (col.get("key_displays") or {}).get(key_norm) or key_norm)

    char_keys = sorted(
        char_freq.keys(),
        key=lambda key_norm: (-char_freq[key_norm], key_displays.get(key_norm) or key_norm),
    )
    characteristics: list[dict[str, Any]] = []
    for key_norm in char_keys:
        values = [(col.get("values") or {}).get(key_norm) for col in collapsed]
        characteristics.append(
            {
                "char_key": key_norm,
                "char_key_display": key_displays.get(key_norm) or key_norm,
                "purchase_count": char_freq[key_norm],
                "cells": [v if v else "—" for v in values],
            }
        )

    return {
        "columns": collapsed,
        "characteristics": characteristics,
        "purchase_count": len(purchases),
        "purchases_shown": purchases_shown,
        "truncated": truncated or purchases_shown < len(purchases),
        "max_columns": max_columns,
    }


def _html_finalize_characteristic_values(
    aggregates: dict[str, Any],
    *,
    ktru_code: str,
    limit_characteristics: int,
    limit_values_per_characteristic: int,
) -> list[dict[str, Any]]:
    ktru_total = int(aggregates["ktru_purchase_totals"].get(ktru_code) or 0)
    grouped: dict[str, dict[str, Any]] = {}
    for (code, key_norm, value_norm), item in aggregates["by_ktru_char"].items():
        if code != ktru_code:
            continue
        char_bucket = grouped.setdefault(
            key_norm,
            {
                "char_key": key_norm,
                "char_key_display": item.get("char_key_display") or key_norm,
                "purchase_numbers": set(),
                "values": {},
            },
        )
        char_bucket["purchase_numbers"] |= set(item.get("purchase_numbers") or set())
        value_bucket = char_bucket["values"].setdefault(
            value_norm,
            {
                "value": value_norm,
                "value_display": item.get("value_display") or value_norm,
                "purchase_numbers": set(),
            },
        )
        value_bucket["purchase_numbers"] |= set(item.get("purchase_numbers") or set())

    out: list[dict[str, Any]] = []
    for char_bucket in grouped.values():
        char_purchases = {x for x in char_bucket.pop("purchase_numbers") if x}
        char_purchase_count = len(char_purchases)
        values_out: list[dict[str, Any]] = []
        for value_bucket in char_bucket["values"].values():
            value_purchases = {x for x in value_bucket.pop("purchase_numbers") if x}
            value_purchase_count = len(value_purchases)
            values_out.append(
                {
                    "value": value_bucket["value"],
                    "value_display": value_bucket["value_display"],
                    "purchase_count": value_purchase_count,
                    "value_share_within_char_pct": _html_pct(value_purchase_count, char_purchase_count),
                }
            )
        values_out.sort(key=lambda x: (-int(x["purchase_count"]), str(x["value"])))
        out.append(
            {
                "char_key": char_bucket["char_key"],
                "char_key_display": char_bucket["char_key_display"],
                "purchase_count": char_purchase_count,
                "purchase_share_within_ktru_pct": _html_pct(char_purchase_count, ktru_total),
                "top_values": values_out[: max(1, int(limit_values_per_characteristic))],
            }
        )
    out.sort(key=lambda x: (-int(x["purchase_count"]), str(x["char_key"])))
    return out[: max(1, int(limit_characteristics))]


def _analytics_v2_summary_from_rows(
    rows: list[dict[str, Any]],
    *,
    ktru_code: str | None = None,
    ktru_query: str | None = None,
    top_ktru_limit: int = 30,
    top_sets_limit: int = 10,
    top_chars_limit: int = 20,
    top_values_limit: int = 5,
) -> dict[str, Any]:
    query = (ktru_query or "").strip()
    filtered_rows = rows
    if query:
        filtered_rows = [r for r in rows if _html_rows_match_query(r, query)]
    aggregates = _html_build_period_aggregates(filtered_rows)
    total_purchases = int(aggregates["total_purchases"])
    top_ktru = _html_finalize_top_ktru(aggregates, total_purchases=total_purchases)
    if query:
        top_ktru = [
            row
            for row in top_ktru
            if _html_rows_match_query(
                {"ktru_code": row.get("ktru_code"), "position_display_name": row.get("position_display_name")},
                query,
            )
        ]

    selected = (ktru_code or "").strip()
    if selected and not any(row["ktru_code"] == selected for row in top_ktru):
        for row in _html_finalize_top_ktru(aggregates, total_purchases=total_purchases):
            if row["ktru_code"] == selected:
                top_ktru.append(row)
                break

    kpi: dict[str, Any] = {
        "total_purchases": total_purchases,
        "total_ktru_codes": len(top_ktru),
        "top_ktru_code": None,
        "top_ktru_name": None,
        "top_ktru_purchase_share_pct": None,
        "top_set_hash": None,
        "top_set_share_within_ktru_pct": None,
        "top_set_preview_short": None,
    }
    if top_ktru:
        lead = top_ktru[0]
        kpi["top_ktru_code"] = lead["ktru_code"]
        kpi["top_ktru_name"] = lead.get("position_display_name") or lead["ktru_code"]
        kpi["top_ktru_purchase_share_pct"] = lead.get("purchase_share_pct")
        lead_sets = _html_finalize_characteristic_sets(aggregates, ktru_code=str(lead["ktru_code"]), limit=1)
        if lead_sets:
            kpi["top_set_hash"] = lead_sets[0]["characteristics_hash"]
            kpi["top_set_share_within_ktru_pct"] = lead_sets[0].get("set_share_within_ktru_pct")
            kpi["top_set_preview_short"] = lead_sets[0].get("preview_short")

    selected_detail: dict[str, Any] | None = None
    if selected:
        selected_row = next((r for r in top_ktru if r["ktru_code"] == selected), None)
        sets = _html_finalize_characteristic_sets(aggregates, ktru_code=selected, limit=top_sets_limit)
        char_values = _html_finalize_characteristic_values(
            aggregates,
            ktru_code=selected,
            limit_characteristics=top_chars_limit,
            limit_values_per_characteristic=top_values_limit,
        )
        ktru_rows = [r for r in filtered_rows if str(r.get("ktru_code") or "").strip() == selected]
        selected_detail = {
            "ktru_code": selected,
            "position_display_name": (selected_row or {}).get("position_display_name") or selected,
            "purchase_count": int((selected_row or {}).get("purchase_count") or 0),
            "purchase_share_pct": (selected_row or {}).get("purchase_share_pct"),
            "characteristic_sets": (selected_row or {}).get("characteristic_sets") or 0,
            "top_sets": sets,
            "typical_profile": _html_build_typical_profile(char_values),
            "purchase_matrix": _html_build_ktru_purchase_matrix(ktru_rows, ktru_code=selected),
            "top_characteristic_values": char_values,
        }
        kpi["top_ktru_code"] = selected
        kpi["top_ktru_name"] = selected_detail["position_display_name"]
        kpi["top_ktru_purchase_share_pct"] = selected_detail.get("purchase_share_pct")
        if sets:
            kpi["top_set_hash"] = sets[0]["characteristics_hash"]
            kpi["top_set_share_within_ktru_pct"] = sets[0].get("set_share_within_ktru_pct")
            kpi["top_set_preview_short"] = sets[0].get("preview_short")

    return {
        "ktru_query": query,
        "selected_ktru": selected,
        "kpi": kpi,
        "top_ktru": top_ktru[: max(1, int(top_ktru_limit))],
        "selected_detail": selected_detail,
    }


def _analytics_v2_detail_from_aggregates(
    aggregates: dict[str, Any],
    *,
    selected: str,
    detail_rows: list[dict[str, Any]],
    selected_row: dict[str, Any] | None,
    top_sets_limit: int,
    top_chars_limit: int,
    top_values_limit: int,
    period_total_purchases: int | None = None,
    matrix_max_purchases: int | None = 120,
) -> dict[str, Any]:
    sets = _html_finalize_characteristic_sets(aggregates, ktru_code=selected, limit=top_sets_limit)
    char_values = _html_finalize_characteristic_values(
        aggregates,
        ktru_code=selected,
        limit_characteristics=top_chars_limit,
        limit_values_per_characteristic=top_values_limit,
    )
    ktru_purchase_count = int(aggregates["ktru_purchase_totals"].get(selected) or 0)
    total_for_share = (
        int(period_total_purchases)
        if period_total_purchases is not None
        else int(aggregates["total_purchases"])
    )
    purchase_share = _html_pct(ktru_purchase_count, total_for_share)
    set_count = len(
        {
            key[1]
            for key in aggregates["by_ktru_set"]
            if key[0] == selected
        }
    )
    return {
        "ktru_code": selected,
        "position_display_name": (selected_row or {}).get("position_display_name") or selected,
        "purchase_count": ktru_purchase_count or int((selected_row or {}).get("purchase_count") or 0),
        "purchase_share_pct": purchase_share if purchase_share is not None else (selected_row or {}).get("purchase_share_pct"),
        "characteristic_sets": set_count or int((selected_row or {}).get("characteristic_sets") or 0),
        "top_sets": sets,
        "typical_profile": _html_build_typical_profile(char_values),
        "purchase_matrix": _html_build_ktru_purchase_matrix(
            detail_rows,
            ktru_code=selected,
            max_purchases=matrix_max_purchases,
        ),
        "top_characteristic_values": char_values,
    }


def html_analytics_v2_summary(
    path: Path,
    *,
    days: int = 30,
    ktru_code: str | None = None,
    ktru_query: str | None = None,
    top_ktru_limit: int = 30,
    top_sets_limit: int = 10,
    top_chars_limit: int = 20,
    top_values_limit: int = 5,
) -> dict[str, Any]:
    days = max(1, min(int(days), 365))
    empty: dict[str, Any] = {
        "db_exists": False,
        "days": days,
        "ktru_query": (ktru_query or "").strip(),
        "selected_ktru": (ktru_code or "").strip(),
        "kpi": {},
        "top_ktru": [],
        "selected_detail": None,
    }
    if not path.is_file():
        return empty

    rows = _html_ktru_rows_for_period(path, days=days)
    body = _analytics_v2_summary_from_rows(
        rows,
        ktru_code=ktru_code,
        ktru_query=ktru_query,
        top_ktru_limit=top_ktru_limit,
        top_sets_limit=top_sets_limit,
        top_chars_limit=top_chars_limit,
        top_values_limit=top_values_limit,
    )
    return {"db_exists": True, "days": days, **body}


def _history_dashboard_cache_key(
    path: Path,
    *,
    date_from: date,
    date_to: date,
    ktru_code: str | None,
    ktru_query: str | None,
    table_limit: int,
) -> tuple[Any, ...]:
    return (
        "dashboard",
        *_history_db_cache_revision(path),
        date_from.isoformat(),
        date_to.isoformat(),
        (ktru_code or "").strip(),
        (ktru_query or "").strip(),
        int(table_limit),
    )


def _history_dashboard_kpi_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if not row:
        return {
            "region_count": 0,
            "total_purchases": 0,
            "total_sum_rub": None,
            "total_sum_display": _format_rub_compact(None),
            "customer_count": 0,
            "avg_per_purchase_rub": None,
            "avg_per_purchase_display": _format_rub_table(None),
            "unique_ktru_count": 0,
        }
    total_purchases = int(row["total_purchases"] or 0)
    total_sum = row["total_sum_rub"]
    total_sum_rub = float(total_sum) if total_sum is not None else None
    avg = _dashboard_avg_per_purchase(total_sum_rub, total_purchases)
    return {
        "region_count": int(row["region_count"] or 0),
        "total_purchases": total_purchases,
        "total_sum_rub": total_sum_rub,
        "total_sum_display": _format_rub_compact(total_sum_rub),
        "customer_count": int(row["customer_count"] or 0),
        "avg_per_purchase_rub": avg,
        "avg_per_purchase_display": _format_rub_table(avg),
        "unique_ktru_count": int(row["unique_ktru_count"] or 0),
    }


def _history_dashboard_ktru_table_row(row: sqlite3.Row) -> dict[str, Any]:
    purchase_count = int(row["purchase_count"] or 0)
    total_sum = row["total_sum_rub"]
    total_sum_rub = float(total_sum) if total_sum is not None else None
    avg = _dashboard_avg_per_purchase(total_sum_rub, purchase_count)
    return {
        "ktru_code": str(row["ktru_code"] or "").strip(),
        "position_display_name": str(row["position_display_name"] or ""),
        "purchase_count": purchase_count,
        "total_sum_rub": total_sum_rub,
        "total_sum_display": _format_rub_table(total_sum_rub),
        "avg_per_purchase_rub": avg,
        "avg_per_purchase_display": _format_rub_table(avg),
    }


def history_dashboard_summary(
    path: Path,
    *,
    date_from: date,
    date_to: date,
    ktru_code: str | None = None,
    ktru_query: str | None = None,
    table_limit: int = 100,
) -> dict[str, Any]:
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    period_label = f"{date_from.isoformat()} — {date_to.isoformat()}"
    query = (ktru_query or "").strip()
    selected = (ktru_code or "").strip()
    empty: dict[str, Any] = {
        "db_exists": False,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "period_label": period_label,
        "ktru_query": query,
        "selected_ktru": selected,
        "kpi": _history_dashboard_kpi_row(None),
        "purchases_by_month": [],
        "ktru_table": [],
        "ktru_table_totals": _dashboard_money_fields(total_sum_rub=None, purchase_count=0),
        "single_ktru": None,
    }
    if not path.is_file():
        return empty

    cache_key = _history_dashboard_cache_key(
        path,
        date_from=date_from,
        date_to=date_to,
        ktru_code=ktru_code,
        ktru_query=ktru_query,
        table_limit=table_limit,
    )
    cached = _history_analytics_cache_get(path, cache_key)
    if cached is not None:
        return cached

    from eis_history_db import history_connect

    from_iso, to_exclusive = _history_period_bounds_utc(date_from, date_to)
    where_sql, params = _history_period_filters(
        document_type_44=HISTORY_DOCUMENT_TYPE_44,
        from_iso=from_iso,
        to_exclusive=to_exclusive,
        ktru_code=ktru_code or None,
        ktru_query=ktru_query or None,
    )
    sum_expr = _history_line_sum_sql_expr()
    limit = max(1, int(table_limit))

    with history_connect(path) as conn:
        kpi_row = conn.execute(
            f"""
            SELECT
              COUNT(DISTINCT CASE WHEN COALESCE(n.org_region, '') <> '' THEN n.org_region END)
                AS region_count,
              COUNT(DISTINCT l.purchase_number) AS total_purchases,
              COUNT(DISTINCT CASE WHEN COALESCE(l.customer_text, '') <> '' THEN l.customer_text END)
                AS customer_count,
              COUNT(DISTINCT l.ktru_code) AS unique_ktru_count,
              SUM({sum_expr}) AS total_sum_rub
            {_HISTORY_LINES_FROM}
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        month_rows = conn.execute(
            f"""
            SELECT
              strftime('%Y-%m', {_HISTORY_OBSERVED_AT_EXPR}) AS month,
              COUNT(DISTINCT l.purchase_number) AS purchase_count
            {_HISTORY_LINES_FROM}
            WHERE {where_sql}
              AND {_HISTORY_OBSERVED_AT_EXPR} IS NOT NULL
              AND {_HISTORY_OBSERVED_AT_EXPR} <> ''
            GROUP BY month
            HAVING month IS NOT NULL AND month <> ''
            ORDER BY month ASC
            """,
            params,
        ).fetchall()

        ktru_table: list[dict[str, Any]] = []
        selected_name = ""
        if selected:
            name_row = conn.execute(
                f"""
                SELECT MAX(l.position_display_name) AS position_display_name
                {_HISTORY_LINES_FROM}
                WHERE {where_sql}
                """,
                params,
            ).fetchone()
            if name_row:
                selected_name = str(name_row["position_display_name"] or "")
        else:
            table_rows = conn.execute(
                f"""
                SELECT
                  l.ktru_code,
                  MAX(l.position_display_name) AS position_display_name,
                  COUNT(DISTINCT l.purchase_number) AS purchase_count,
                  SUM({sum_expr}) AS total_sum_rub
                {_HISTORY_LINES_FROM}
                WHERE {where_sql}
                GROUP BY l.ktru_code
                ORDER BY purchase_count DESC, total_sum_rub DESC, l.ktru_code ASC
                LIMIT ?
                """,
                list(params) + [limit],
            ).fetchall()
            for row in table_rows:
                item = _history_dashboard_ktru_table_row(row)
                if item["ktru_code"]:
                    ktru_table.append(item)

    purchases_by_month = [
        {
            "month": str(row["month"] or ""),
            "month_label": _month_label_ru(str(row["month"] or "")),
            "purchase_count": int(row["purchase_count"] or 0),
        }
        for row in month_rows
    ]

    kpi = _history_dashboard_kpi_row(kpi_row)
    ktru_table_totals = _dashboard_money_fields(
        total_sum_rub=kpi.get("total_sum_rub"),
        purchase_count=int(kpi.get("total_purchases") or 0),
    )
    single_ktru: dict[str, Any] | None = None
    if selected:
        single_ktru = {
            "ktru_code": selected,
            "position_display_name": selected_name,
            "purchase_count": kpi["total_purchases"],
            "total_sum_rub": kpi["total_sum_rub"],
            "total_sum_display": kpi["total_sum_display"],
            "avg_per_purchase_rub": kpi["avg_per_purchase_rub"],
            "avg_per_purchase_display": kpi["avg_per_purchase_display"],
        }

    result: dict[str, Any] = {
        "db_exists": True,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "period_label": period_label,
        "ktru_query": query,
        "selected_ktru": selected,
        "kpi": kpi,
        "purchases_by_month": purchases_by_month,
        "ktru_table": ktru_table,
        "ktru_table_totals": ktru_table_totals,
        "single_ktru": single_ktru,
    }
    _history_analytics_cache_set(path, cache_key, result)
    return result


def history_analytics_v2_summary(
    path: Path,
    *,
    date_from: date,
    date_to: date,
    ktru_code: str | None = None,
    ktru_query: str | None = None,
    top_ktru_limit: int = 30,
    top_sets_limit: int = 10,
    top_chars_limit: int = 20,
    top_values_limit: int = 5,
) -> dict[str, Any]:
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    period_label = f"{date_from.isoformat()} — {date_to.isoformat()}"
    empty: dict[str, Any] = {
        "db_exists": False,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "period_label": period_label,
        "ktru_query": (ktru_query or "").strip(),
        "selected_ktru": (ktru_code or "").strip(),
        "kpi": {},
        "top_ktru": [],
        "selected_detail": None,
    }
    if not path.is_file():
        return empty

    cache_key = _history_summary_cache_key(
        path,
        date_from=date_from,
        date_to=date_to,
        ktru_code=ktru_code,
        ktru_query=ktru_query,
        top_ktru_limit=top_ktru_limit,
        top_sets_limit=top_sets_limit,
        top_chars_limit=top_chars_limit,
        top_values_limit=top_values_limit,
    )
    cached = _history_analytics_cache_get(path, cache_key)
    if cached is not None:
        return cached

    query = (ktru_query or "").strip()
    selected = (ktru_code or "").strip()
    total_ktru_codes = 0
    if selected:
        total_purchases = _history_period_purchase_count(
            path,
            date_from=date_from,
            date_to=date_to,
            ktru_query=query if not selected else None,
        )
        top_ktru = _history_sql_top_ktru(
            path,
            date_from=date_from,
            date_to=date_to,
            ktru_query=query if not selected else None,
            limit=top_ktru_limit,
            total_purchases=total_purchases,
        )
    else:
        total_purchases, total_ktru_codes, top_ktru = _history_period_overview(
            path,
            date_from=date_from,
            date_to=date_to,
            ktru_query=query or None,
            top_ktru_limit=top_ktru_limit,
        )
    if query and not selected:
        top_ktru = [
            row
            for row in top_ktru
            if _html_rows_match_query(
                {"ktru_code": row.get("ktru_code"), "position_display_name": row.get("position_display_name")},
                query,
            )
        ]

    kpi: dict[str, Any] = {
        "total_purchases": total_purchases,
        "total_ktru_codes": total_ktru_codes if not selected else len(top_ktru),
        "top_ktru_code": None,
        "top_ktru_name": None,
        "top_ktru_purchase_share_pct": None,
        "top_set_hash": None,
        "top_set_share_within_ktru_pct": None,
        "top_set_preview_short": None,
    }
    selected_detail: dict[str, Any] | None = None

    if selected:
        if not any(row["ktru_code"] == selected for row in top_ktru):
            top_ktru.extend(
                _history_sql_top_ktru(
                    path,
                    date_from=date_from,
                    date_to=date_to,
                    ktru_code=selected,
                    limit=1,
                    total_purchases=total_purchases,
                )
            )
        detail_rows = _history_ktru_rows_for_period(
            path,
            date_from=date_from,
            date_to=date_to,
            ktru_code=selected,
        )
        aggregates = _html_build_period_aggregates(detail_rows)
        selected_row = next((r for r in top_ktru if r["ktru_code"] == selected), None)
        selected_detail = _analytics_v2_detail_from_aggregates(
            aggregates,
            selected=selected,
            detail_rows=detail_rows,
            selected_row=selected_row,
            top_sets_limit=top_sets_limit,
            top_chars_limit=top_chars_limit,
            top_values_limit=top_values_limit,
            period_total_purchases=total_purchases,
        )
        kpi["top_ktru_code"] = selected
        kpi["top_ktru_name"] = selected_detail["position_display_name"]
        kpi["top_ktru_purchase_share_pct"] = selected_detail.get("purchase_share_pct")
        if selected_detail["top_sets"]:
            lead_set = selected_detail["top_sets"][0]
            kpi["top_set_hash"] = lead_set["characteristics_hash"]
            kpi["top_set_share_within_ktru_pct"] = lead_set.get("set_share_within_ktru_pct")
            kpi["top_set_preview_short"] = lead_set.get("preview_short")
    elif top_ktru:
        lead = top_ktru[0]
        kpi["top_ktru_code"] = lead["ktru_code"]
        kpi["top_ktru_name"] = lead.get("position_display_name") or lead["ktru_code"]
        kpi["top_ktru_purchase_share_pct"] = lead.get("purchase_share_pct")

    result = {
        "db_exists": True,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "period_label": period_label,
        "ktru_query": query,
        "selected_ktru": selected,
        "kpi": kpi,
        "top_ktru": top_ktru[: max(1, int(top_ktru_limit))],
        "selected_detail": selected_detail,
    }
    _history_analytics_cache_set(path, cache_key, result)
    return result


def recent_events(path: Path, *, limit: int = 50, ktru_code: str | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    params: list[Any] = []
    ktru_sql = ""
    if ktru_code:
        ktru_sql = "WHERE k.ktru_code = ?"
        params.append(ktru_code)
    params.append(max(1, int(limit)))
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              n.purchase_number,
              n.org_region,
              n.document_type_44,
              n.shelf_confidence,
              n.publish_dt_utc,
              n.detected_at_utc,
              k.ktru_code,
              k.position_display_name,
              k.notice_total_text
            FROM ktru_lines k
            JOIN notice_events n ON n.purchase_key = k.purchase_key
            {ktru_sql}
            ORDER BY n.detected_at_utc DESC, k.ktru_code ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["detected_display_msk"] = _display_msk(d.get("detected_at_utc"))
        d["publish_display_msk"] = _display_msk(d.get("publish_dt_utc"))
        out.append(d)
    return out


def _parse_utc(raw: object) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _rss_subject_code_from_ikz(raw: object) -> str:
    ikz = re.sub(r"\D+", "", str(raw or ""))
    if len(ikz) < 5:
        return ""
    code = ikz[3:5]
    return code if code in {"35", "69", "76"} else ""


def _rss_detection_window_display(start_raw: object, end_raw: object) -> str:
    start = str(start_raw or "").strip()
    end = str(end_raw or "").strip()
    if start and end:
        return f"{_display_msk(start)} – {_display_msk(end)}"
    if end:
        return f"до {_display_msk(end)}"
    return "—"


def rss_vs_getdocs_recent(path: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with connect(path) as conn:
        latest_new_run = conn.execute(
            """
            SELECT id, finished_at_utc
            FROM rss_poll_runs
            WHERE items_new > 0
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        latest_new_id = int(latest_new_run["id"]) if latest_new_run else 0
        latest_new_finished = str(latest_new_run["finished_at_utc"] or "") if latest_new_run else ""
        rows = conn.execute(
            """
            SELECT
              r.reg_number,
              r.title,
              r.link,
              r.pub_date_utc AS rss_pub_date_utc,
              r.first_seen_at_utc AS rss_first_seen_at_utc,
              r.stage_text,
              r.customer_name,
              r.price_text,
              r.ikz,
              r.first_seen_poll_run_id,
              r.first_seen_poll_finished_at_utc,
              r.detection_window_start_utc,
              r.detection_window_end_utc,
              n.first_seen_at_utc AS getdocs_first_seen_at_utc,
              n.publish_dt_utc AS getdocs_publish_dt_utc,
              html_line.position_display_name AS html_position_name,
              html_line.characteristics_json AS html_characteristics_json,
              xml_line.position_display_name AS xml_position_name,
              xml_line.characteristics_json AS xml_characteristics_json
            FROM eis_rss_observations r
            LEFT JOIN notice_events n ON n.purchase_number = r.reg_number
            LEFT JOIN (
              SELECT purchase_number, position_display_name, characteristics_json
              FROM (
                SELECT l.purchase_number, l.position_display_name, l.characteristics_json,
                       ROW_NUMBER() OVER (PARTITION BY l.purchase_number ORDER BY l.line_key ASC) AS rn
                FROM notice_html_ktru_lines l
                JOIN notice_html_pages p ON p.purchase_number = l.purchase_number AND p.ok = 1
              ) ordered
              WHERE rn = 1
            ) html_line ON html_line.purchase_number = r.reg_number
            LEFT JOIN (
              SELECT purchase_number, position_display_name, characteristics_json
              FROM (
                SELECT l.purchase_number, l.position_display_name, l.characteristics_json,
                       ROW_NUMBER() OVER (PARTITION BY l.purchase_number ORDER BY l.line_key ASC) AS rn
                FROM ktru_lines l
              ) ordered
              WHERE rn = 1
            ) xml_line ON xml_line.purchase_number = r.reg_number
            ORDER BY r.pub_date_utc DESC, r.first_seen_at_utc DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        pub_dt = _parse_utc(d.get("rss_pub_date_utc"))
        rss_dt = _parse_utc(d.get("rss_first_seen_at_utc"))
        getdocs_dt = _parse_utc(d.get("getdocs_first_seen_at_utc"))
        lead: int | None = None
        rss_detect_lag: int | None = None
        if pub_dt and rss_dt:
            rss_detect_lag = int((rss_dt - pub_dt).total_seconds())
        klass = "rss_only"
        if rss_dt and getdocs_dt:
            lead = int((getdocs_dt - rss_dt).total_seconds())
            if lead > 30:
                klass = "rss_faster"
            elif lead < -30:
                klass = "getdocs_faster"
            else:
                klass = "same_window"
        d["rss_lead_seconds"] = lead
        d["rss_detect_lag_seconds"] = rss_detect_lag
        d["comparison_class"] = klass
        d["rss_pub_display_msk"] = _display_msk(d.get("rss_pub_date_utc"))
        d["rss_first_seen_display_msk"] = _display_msk(d.get("rss_first_seen_at_utc"))
        d["first_seen_poll_finished_display_msk"] = _display_msk(d.get("first_seen_poll_finished_at_utc"))
        d["detection_window_display_msk"] = _rss_detection_window_display(
            d.get("detection_window_start_utc"),
            d.get("detection_window_end_utc"),
        )
        d["getdocs_first_seen_display_msk"] = _display_msk(d.get("getdocs_first_seen_at_utc"))
        d["getdocs_publish_display_msk"] = _display_msk(d.get("getdocs_publish_dt_utc"))
        d["subject_code"] = _rss_subject_code_from_ikz(d.get("ikz")) or "—"
        d["is_latest_rss_new"] = (
            bool(latest_new_id and d.get("first_seen_poll_run_id") == latest_new_id)
            or bool(latest_new_finished and str(d.get("rss_first_seen_at_utc") or "") == latest_new_finished)
        )
        position_name = str(d.get("html_position_name") or d.get("xml_position_name") or "").strip()
        chars_json = str(d.get("html_characteristics_json") or d.get("xml_characteristics_json") or "")
        d["position_display_name"] = position_name
        d["trade_mark"] = _trade_mark_from_characteristics_json(chars_json)
        for key in (
            "html_position_name",
            "html_characteristics_json",
            "xml_position_name",
            "xml_characteristics_json",
        ):
            d.pop(key, None)
        out.append(d)
    return out


def last_rss_poll_run(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT id, started_at_utc, finished_at_utc, query_label, rss_url, ok,
                   previous_success_finished_at_utc, items_total, items_new, errors_json
            FROM rss_poll_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["errors"] = json.loads(str(d.get("errors_json") or "[]"))
    except json.JSONDecodeError:
        d["errors"] = []
    d["finished_display_msk"] = _display_msk(d.get("finished_at_utc"))
    d["started_display_msk"] = _display_msk(d.get("started_at_utc"))
    d["previous_success_finished_display_msk"] = _display_msk(d.get("previous_success_finished_at_utc"))
    return d


def _display_msk(raw: object) -> str:
    s = str(raw or "").strip()
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    return dt_to_msk_clean(dt)


def upsert_pp_eis_match(
    path: Path,
    *,
    purchase_number: str,
    line_key: str,
    offer_price_list_item_id: str,
    ktru_code: str,
    stack_key: str = "",
    matched_count: int,
    total_compared: int,
    notice_only_count: int = 0,
    pp_only_count: int = 0,
    diff_json: str = "[]",
    notice_pub_date_utc: str = "",
    pp_link_date: str = "",
    is_exact_match: bool = False,
    lag_minutes: float | None = None,
    match_origin: str = "live",
) -> None:
    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    oid = str(offer_price_list_item_id or "").strip()
    if not pn or not lk or not oid:
        raise ValueError("purchase_number, line_key, offer_price_list_item_id required")
    computed_iso = _utc_iso(datetime.now(timezone.utc))
    origin = str(match_origin or "live").strip().lower() or "live"
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO pp_eis_pp_match(
              purchase_number, line_key, offer_price_list_item_id, ktru_code, stack_key,
              matched_count, total_compared, notice_only_count, pp_only_count, diff_json,
              notice_pub_date_utc, pp_link_date, computed_at_utc, is_exact_match, lag_minutes,
              match_origin
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(purchase_number, line_key, offer_price_list_item_id) DO UPDATE SET
              ktru_code=excluded.ktru_code,
              stack_key=excluded.stack_key,
              matched_count=excluded.matched_count,
              total_compared=excluded.total_compared,
              notice_only_count=excluded.notice_only_count,
              pp_only_count=excluded.pp_only_count,
              diff_json=excluded.diff_json,
              notice_pub_date_utc=excluded.notice_pub_date_utc,
              pp_link_date=excluded.pp_link_date,
              computed_at_utc=excluded.computed_at_utc,
              is_exact_match=excluded.is_exact_match,
              lag_minutes=excluded.lag_minutes,
              match_origin=CASE
                WHEN pp_eis_pp_match.match_origin = 'manual'
                THEN pp_eis_pp_match.match_origin
                WHEN pp_eis_pp_match.match_origin = 'eis_winner_price'
                     AND excluded.match_origin IN ('live', 'historical')
                THEN pp_eis_pp_match.match_origin
                WHEN pp_eis_pp_match.match_origin = 'historical'
                     AND excluded.match_origin = 'live'
                THEN pp_eis_pp_match.match_origin
                ELSE excluded.match_origin
              END
            """,
            (
                pn,
                lk,
                oid,
                str(ktru_code or "")[:200],
                str(stack_key or "")[:200],
                int(matched_count),
                int(total_compared),
                int(notice_only_count),
                int(pp_only_count),
                diff_json,
                str(notice_pub_date_utc or "")[:200],
                str(pp_link_date or "")[:200],
                computed_iso,
                1 if is_exact_match else 0,
                lag_minutes,
                origin[:50],
            ),
        )
        conn.commit()


def set_pp_eis_match_status(
    path: Path,
    *,
    purchase_number: str,
    line_key: str,
    offer_price_list_item_id: str,
    status: str,
    analyst_user: str = "",
    analyst_note: str = "",
    scenario_tag: str | None = None,
    participation_intent: str | None = None,
) -> bool:
    from eis_pp_scenario_tags import normalize_participation_intent, normalize_scenario_tag

    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    oid = str(offer_price_list_item_id or "").strip()
    st = str(status or "").strip().lower()
    if st not in ("pending", "confirmed", "rejected"):
        raise ValueError("invalid analyst status")
    if not pn or not lk or not oid:
        return False
    at_iso = _utc_iso(datetime.now(timezone.utc))
    scenario = normalize_scenario_tag(scenario_tag) if scenario_tag is not None else None
    intent = normalize_participation_intent(participation_intent) if participation_intent is not None else None
    sets = [
        "analyst_status = ?",
        "analyst_user = ?",
        "analyst_note = ?",
        "analyst_at_utc = ?",
    ]
    params: list[Any] = [st, str(analyst_user or "")[:200], str(analyst_note or "")[:2000], at_iso]
    if scenario is not None:
        sets.append("scenario_tag = ?")
        params.append(scenario[:80])
    if intent is not None:
        sets.append("participation_intent = ?")
        params.append(intent[:40])
    params.extend([pn, lk, oid])
    with connect(path) as conn:
        cur = conn.execute(
            f"""
            UPDATE pp_eis_pp_match
            SET {', '.join(sets)}
            WHERE purchase_number = ? AND line_key = ? AND offer_price_list_item_id = ?
            """,
            params,
        )
        conn.commit()
        return int(cur.rowcount or 0) > 0


def latest_winner_price_pp_hint(
    path: Path,
    *,
    purchase_number: str,
) -> str:
    pn = str(purchase_number or "").strip()
    if not pn:
        return ""
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT event_type, payload_json
            FROM pp_eis_pp_events
            WHERE payload_json LIKE ?
              AND event_type IN ('winner_price_search_failed', 'winner_price_no_cookie')
            ORDER BY id DESC
            LIMIT 1
            """,
            (f"%{pn}%",),
        ).fetchone()
    if not row:
        return ""
    event_type = str(row[0] or "")
    try:
        payload = json.loads(str(row[1] or "{}"))
    except json.JSONDecodeError:
        payload = {}
    reason = str(payload.get("reason") or "").strip().lower()
    http_status = payload.get("http_status")
    error = str(payload.get("error") or "")
    if event_type == "winner_price_no_cookie" or reason == "no_cookie":
        return "поиск ПП: нет customer-cookie РТС"
    if reason == "rate_limited" or http_status == 429 or "429" in error:
        return "поиск ПП: лимит запросов РТС (429), повторите позже"
    if reason == "session_expired" or http_status in (401, 403):
        return "поиск ПП: customer-сессия РТС истекла"
    if event_type == "winner_price_search_failed":
        return "поиск ПП: ошибка API РТС"
    return ""


def append_pp_eis_event(
    path: Path,
    *,
    event_type: str,
    ktru_code: str = "",
    payload: dict[str, Any] | None = None,
    occurred_at_utc: datetime | None = None,
) -> None:
    when = occurred_at_utc or datetime.now(timezone.utc)
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO pp_eis_pp_events(event_type, occurred_at_utc, ktru_code, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(event_type or "")[:80],
                _utc_iso(when),
                str(ktru_code or "")[:200],
                _json_dumps(payload or {}),
            ),
        )
        conn.commit()


def _pp_eis_match_row(row: sqlite3.Row) -> dict[str, Any]:
    from eis_pp_link import format_lag_minutes

    d = dict(row)
    try:
        d["diff"] = json.loads(str(d.pop("diff_json", None) or "[]"))
    except json.JSONDecodeError:
        d["diff"] = []
    d["match_label"] = f"{d.get('matched_count', 0)}/{d.get('total_compared', 0)}"
    d["is_exact_match"] = bool(int(d.get("is_exact_match") or 0))
    lag = d.get("lag_minutes")
    d["lag_minutes"] = float(lag) if lag is not None else None
    d["lag_label"] = format_lag_minutes(d["lag_minutes"])
    d["notice_pub_display_msk"] = _display_msk(d.get("notice_pub_date_utc"))
    pp_dt = _parse_compare_datetime_utc(d.get("pp_link_date"))
    d["pp_link_display_msk"] = dt_to_msk_clean(pp_dt) if pp_dt is not None else "—"
    d["analyst_at_display_msk"] = _display_msk(d.get("analyst_at_utc"))
    pn = str(d.get("purchase_number") or "").strip()
    oid = str(d.get("offer_price_list_item_id") or "").strip()
    d["notice_url"] = (
        f"https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber={pn}"
        if pn
        else ""
    )
    d["pp_offer_url"] = (
        f"https://offers.rts-tender.ru/customer/offers/offer-item/{oid}"
        if oid
        else ""
    )
    origin = str(d.get("match_origin") or "live").strip().lower() or "live"
    d["match_origin"] = origin
    d["match_origin_label"] = {
        "historical": "исторические данные",
        "manual": "ручной импорт",
        "eis_winner_price": "цена победителя ЕИС",
    }.get(origin, "")
    from eis_pp_scenario_tags import participation_intent_label, scenario_tag_label

    scenario = str(d.get("scenario_tag") or "").strip()
    intent = str(d.get("participation_intent") or "").strip()
    d["scenario_tag"] = scenario
    d["participation_intent"] = intent
    d["scenario_tag_label"] = scenario_tag_label(scenario)
    d["participation_intent_label"] = participation_intent_label(intent)
    return d


def _notice_line_sum_text(line_sum: str, unit_text: str, quantity_text: str) -> str | None:
    """Display sum for notice line: parsed line_sum or unit × quantity."""
    stored = str(line_sum or "").strip()
    if stored:
        return stored
    from participation_price import format_money_ru, parse_money_amount, parse_quantity

    unit = parse_money_amount(str(unit_text or "").strip())
    qty = parse_quantity(str(quantity_text or "").strip())
    if unit is None or qty is None:
        return None
    return format_money_ru(unit * qty)


def enrich_pp_eis_matches_with_unit_prices(
    path: Path,
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach notice and PP unit prices for EIS↔PP link list rows."""
    if not matches or not path.is_file():
        return matches
    from participation_price import format_rts_unit_price_from_float

    line_keys: list[tuple[str, str]] = []
    oids: list[str] = []
    for match in matches:
        pn = str(match.get("purchase_number") or "").strip()
        lk = str(match.get("line_key") or "").strip()
        oid = str(match.get("offer_price_list_item_id") or "").strip()
        if pn and lk:
            line_keys.append((pn, lk))
        if oid:
            oids.append(oid)
    notice_prices: dict[tuple[str, str], str] = {}
    notice_line_sums: dict[tuple[str, str], str] = {}
    pp_prices: dict[str, float | None] = {}
    with connect(path) as conn:
        if line_keys:
            placeholders = ",".join("(?, ?)" for _ in line_keys)
            flat: list[str] = [part for pair in line_keys for part in pair]
            rows = conn.execute(
                f"""
                SELECT purchase_number, line_key, price_per_unit_text, quantity_text, line_sum_text
                FROM notice_html_ktru_lines
                WHERE (purchase_number, line_key) IN ({placeholders})
                """,
                flat,
            ).fetchall()
            for row in rows:
                key = (str(row[0]).strip(), str(row[1]).strip())
                notice_prices[key] = str(row[2] or "").strip()
                notice_line_sums[key] = _notice_line_sum_text(
                    str(row[4] or "").strip(),
                    str(row[2] or "").strip(),
                    str(row[3] or "").strip(),
                )
        if oids:
            placeholders = ",".join("?" for _ in oids)
            rows = conn.execute(
                f"""
                SELECT offer_price_list_item_id, unit_price
                FROM rts_pp_monitor_registry
                WHERE offer_price_list_item_id IN ({placeholders})
                """,
                oids,
            ).fetchall()
            for row in rows:
                oid = str(row[0]).strip()
                try:
                    pp_prices[oid] = float(row[1]) if row[1] is not None else None
                except (TypeError, ValueError):
                    pp_prices[oid] = None
    out: list[dict[str, Any]] = []
    for match in matches:
        row = dict(match)
        pn = str(row.get("purchase_number") or "").strip()
        lk = str(row.get("line_key") or "").strip()
        oid = str(row.get("offer_price_list_item_id") or "").strip()
        notice_text = notice_prices.get((pn, lk), "")
        row["notice_unit_price_text"] = notice_text or None
        row["notice_line_sum_text"] = notice_line_sums.get((pn, lk)) or None
        pp_raw = pp_prices.get(oid)
        row["pp_unit_price_text"] = (
            format_rts_unit_price_from_float(pp_raw) if pp_raw is not None else None
        )
        out.append(row)
    return out


def get_pp_eis_match(
    path: Path,
    *,
    purchase_number: str,
    line_key: str,
    offer_price_list_item_id: str,
) -> dict[str, Any] | None:
    pn = str(purchase_number or "").strip()
    lk = str(line_key or "").strip()
    oid = str(offer_price_list_item_id or "").strip()
    if not pn or not lk or not oid or not path.is_file():
        return None
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM pp_eis_pp_match
            WHERE purchase_number = ? AND line_key = ? AND offer_price_list_item_id = ?
            """,
            (pn, lk, oid),
        ).fetchone()
    return _pp_eis_match_row(row) if row else None


_PP_EIS_MATCH_SORT_COLUMNS: dict[str, str] = {
    "notice_pub": "notice_pub_date_utc",
    "pp_link": "pp_link_date",
    "lag": "lag_minutes",
}


def _pp_eis_match_order_sql(sort: str | None, sort_dir: str | None) -> str:
    sort_key = str(sort or "").strip()
    col = _PP_EIS_MATCH_SORT_COLUMNS.get(sort_key)
    if not col:
        return "ABS(COALESCE(lag_minutes, 999999)), notice_pub_date_utc DESC, computed_at_utc DESC"
    direction = "ASC" if str(sort_dir or "").strip().lower() == "asc" else "DESC"
    return f"{col} {direction}, computed_at_utc DESC"


def list_pp_eis_matches(
    path: Path,
    *,
    limit: int = 100,
    offset: int = 0,
    ktru_code: str | None = None,
    analyst_status: str | None = None,
    min_matched: int | None = None,
    exact_total: int | None = None,
    exact_only: bool | None = True,
    lag_direction: str | None = None,
    offer_price_list_item_id: str | None = None,
    purchase_number: str | None = None,
    line_key: str | None = None,
    sort: str | None = None,
    sort_dir: str | None = None,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    where: list[str] = []
    params: list[Any] = []
    if ktru_code:
        where.append("ktru_code = ?")
        params.append(str(ktru_code).strip())
    if analyst_status:
        where.append("analyst_status = ?")
        params.append(str(analyst_status).strip().lower())
    if exact_only is True:
        where.append("is_exact_match = 1")
    elif exact_only is False:
        where.append("is_exact_match = 0")
    if min_matched is not None:
        where.append("matched_count >= ?")
        params.append(int(min_matched))
    if exact_total is not None:
        where.append("total_compared = ?")
        params.append(int(exact_total))
    lag_dir = str(lag_direction or "").strip().lower()
    if lag_dir == "before":
        where.append("lag_minutes < 0")
    elif lag_dir == "after":
        where.append("lag_minutes > 0")
    elif lag_dir == "zero":
        where.append("lag_minutes = 0")
    if offer_price_list_item_id:
        where.append("offer_price_list_item_id = ?")
        params.append(str(offer_price_list_item_id).strip())
    if purchase_number:
        where.append("purchase_number = ?")
        params.append(str(purchase_number).strip())
    if line_key:
        where.append("line_key = ?")
        params.append(str(line_key).strip())
    where_sql = f" AND {' AND '.join(where)}" if where else ""
    order_sql = _pp_eis_match_order_sql(sort, sort_dir)
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM pp_eis_pp_match
            WHERE 1=1{where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    return [_pp_eis_match_row(row) for row in rows]


def count_pp_eis_matches(
    path: Path,
    *,
    ktru_code: str | None = None,
    analyst_status: str | None = None,
    min_matched: int | None = None,
    exact_total: int | None = None,
    exact_only: bool | None = True,
    lag_direction: str | None = None,
    offer_price_list_item_id: str | None = None,
    purchase_number: str | None = None,
    line_key: str | None = None,
) -> int:
    if not path.is_file():
        return 0
    where: list[str] = []
    params: list[Any] = []
    if ktru_code:
        where.append("ktru_code = ?")
        params.append(str(ktru_code).strip())
    if analyst_status:
        where.append("analyst_status = ?")
        params.append(str(analyst_status).strip().lower())
    if exact_only is True:
        where.append("is_exact_match = 1")
    elif exact_only is False:
        where.append("is_exact_match = 0")
    if min_matched is not None:
        where.append("matched_count >= ?")
        params.append(int(min_matched))
    if exact_total is not None:
        where.append("total_compared = ?")
        params.append(int(exact_total))
    lag_dir = str(lag_direction or "").strip().lower()
    if lag_dir == "before":
        where.append("lag_minutes < 0")
    elif lag_dir == "after":
        where.append("lag_minutes > 0")
    elif lag_dir == "zero":
        where.append("lag_minutes = 0")
    if offer_price_list_item_id:
        where.append("offer_price_list_item_id = ?")
        params.append(str(offer_price_list_item_id).strip())
    if purchase_number:
        where.append("purchase_number = ?")
        params.append(str(purchase_number).strip())
    if line_key:
        where.append("line_key = ?")
        params.append(str(line_key).strip())
    where_sql = f" AND {' AND '.join(where)}" if where else ""
    with connect(path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM pp_eis_pp_match WHERE 1=1{where_sql}",
            params,
        ).fetchone()
    return int(row[0]) if row else 0


def pp_eis_match_stats(path: Path, *, ktru_code: str | None = None, window_min: int = 60) -> dict[str, Any]:
    if not path.is_file():
        return {"by_status": {}, "by_score": [], "hypothesis": {}}
    where_parts: list[str] = ["is_exact_match = 1"]
    params: list[Any] = []
    if ktru_code:
        where_parts.append("ktru_code = ?")
        params.append(str(ktru_code).strip())
    where = f" WHERE {' AND '.join(where_parts)}"
    with connect(path) as conn:
        status_rows = conn.execute(
            f"""
            SELECT analyst_status, COUNT(*) AS cnt
            FROM pp_eis_pp_match{where}
            GROUP BY analyst_status
            """,
            params,
        ).fetchall()
        lag_before = conn.execute(
            f"""
            SELECT COUNT(*) FROM pp_eis_pp_match{where}
            {" AND lag_minutes < 0" if where else " WHERE lag_minutes < 0"}
            """,
            params,
        ).fetchone()[0]
        lag_after = conn.execute(
            f"""
            SELECT COUNT(*) FROM pp_eis_pp_match{where}
            {" AND lag_minutes > 0" if where else " WHERE lag_minutes > 0"}
            """,
            params,
        ).fetchone()[0]
        lag_zero = conn.execute(
            f"""
            SELECT COUNT(*) FROM pp_eis_pp_match{where}
            {" AND lag_minutes = 0" if where else " WHERE lag_minutes = 0"}
            """,
            params,
        ).fetchone()[0]
        total_exact = conn.execute(
            f"SELECT COUNT(*) FROM pp_eis_pp_match{where}",
            params,
        ).fetchone()[0]
    by_status = {str(r[0]): int(r[1]) for r in status_rows}
    return {
        "by_status": by_status,
        "by_score": [],
        "hypothesis": {
            "window_min": int(window_min),
            "total_candidates": int(total_exact or 0),
            "pending": int(by_status.get("pending", 0)),
            "confirmed": int(by_status.get("confirmed", 0)),
            "rejected": int(by_status.get("rejected", 0)),
            "lag_before_notice": int(lag_before or 0),
            "lag_after_notice": int(lag_after or 0),
            "lag_zero": int(lag_zero or 0),
        },
    }


def list_pp_monitor_findings_by_ktru(path: Path, ktru_code: str) -> list[dict[str, Any]]:
    code = str(ktru_code or "").strip()
    if not code or not path.is_file():
        return []
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT offer_price_list_item_id, ktru_code, trade_mark, link_date, unit_price,
                   card_url, find_json, fetched_at_utc, source, characteristics_hash
            FROM rts_pp_monitor_findings
            WHERE ktru_code = ? AND TRIM(COALESCE(find_json, '')) != ''
            ORDER BY link_date DESC
            """,
            (code,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _pp_monitor_finding_row(row)
        if item:
            out.append(item)
    return out


def analytics_summary(path: Path, *, days: int = 30) -> dict[str, Any]:
    if not path.is_file():
        return {"db_exists": False, "top_ktru": [], "top_characteristics": [], "recent_events": []}
    return {
        "db_exists": True,
        "days": days,
        "top_ktru": top_ktru(path, days=days, limit=15),
        "top_characteristics": top_ktru_characteristics(path, days=days, limit=10),
        "recent_events": recent_events(path, limit=10),
    }

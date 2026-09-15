"""SQLite for historical EIS XML backfill (local CLI).

Reuses fact tables from eis_analytics_db but stores full characteristic payloads
(display_label, characteristic_code, value_display) in ktru_lines.characteristics_json.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from eis_analytics_db import (
    _ensure_column,
    _json_dumps,
    _line_from_match,
    _publish_dt_and_source,
    _purchase_key,
    _utc_iso,
    classify_shelf_confidence,
    connect,
    init_db,
)
from eis_ezt_archive import NoticeMatch
from eis_ktru_characteristics import CustomerCharacteristic

HISTORY_DB_DEFAULT = "eis_history.sqlite3"
HISTORY_SCHEMA = 1

_JOB_STATUS = frozenset({"queued", "running", "done", "failed", "cancelled"})
_CHECKPOINT_STATUS = frozenset({"ok", "error", "no_data", "skipped"})


def history_db_path(raw: str | Path | None = None) -> Path:
    if raw is None:
        raw = HISTORY_DB_DEFAULT
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path.cwd() / p.name


def init_history_db(conn: sqlite3.Connection) -> None:
    init_db(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS backfill_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at_utc TEXT NOT NULL,
          started_at_utc TEXT,
          finished_at_utc TEXT,
          status TEXT NOT NULL DEFAULT 'queued',
          org_regions_json TEXT NOT NULL,
          ktru_codes_json TEXT NOT NULL,
          document_type_44 TEXT NOT NULL,
          date_from TEXT NOT NULL,
          date_to TEXT NOT NULL,
          date_to_max_utc TEXT NOT NULL,
          lag_days INTEGER NOT NULL DEFAULT 90,
          require_ezt_xml INTEGER NOT NULL DEFAULT 0,
          progress_done INTEGER NOT NULL DEFAULT 0,
          progress_total INTEGER NOT NULL DEFAULT 0,
          stats_json TEXT NOT NULL DEFAULT '{}',
          last_error TEXT,
          cancel_requested INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS backfill_checkpoints (
          job_id INTEGER NOT NULL,
          org_region TEXT NOT NULL,
          exact_date TEXT NOT NULL,
          status TEXT NOT NULL,
          zips INTEGER NOT NULL DEFAULT 0,
          matches INTEGER NOT NULL DEFAULT 0,
          lines INTEGER NOT NULL DEFAULT 0,
          error_text TEXT,
          finished_at_utc TEXT,
          PRIMARY KEY (job_id, org_region, exact_date),
          FOREIGN KEY(job_id) REFERENCES backfill_jobs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_backfill_jobs_status ON backfill_jobs(status);
        CREATE INDEX IF NOT EXISTS idx_backfill_cp_job ON backfill_checkpoints(job_id);
        CREATE INDEX IF NOT EXISTS idx_notice_events_publish_dt ON notice_events(publish_dt_utc);
        CREATE INDEX IF NOT EXISTS idx_notice_events_doc_publish ON notice_events(document_type_44, publish_dt_utc);
        """
    )
    _ensure_column(conn, "backfill_jobs", "document_types_json", "TEXT")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('history_schema', ?)",
        (str(HISTORY_SCHEMA),),
    )
    conn.commit()


def history_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    init_history_db(conn)
    return conn


def characteristics_set_hash(
    characteristics: Iterable[CustomerCharacteristic | dict[str, Any]],
) -> tuple[str, str]:
    items: list[dict[str, str]] = []
    for raw in characteristics:
        if isinstance(raw, CustomerCharacteristic):
            code = str(raw.characteristic_code or "").strip()
            display_label = str(raw.display_label or "").strip()
            match_key = str(raw.match_key or "").strip()
            value_display = str(raw.value_display or "").strip()
        elif isinstance(raw, dict):
            code = str(raw.get("characteristic_code") or "").strip()
            display_label = str(raw.get("display_label") or "").strip()
            match_key = str(raw.get("match_key") or "").strip()
            value_display = str(raw.get("value_display") or "").strip()
        else:
            continue
        if not (match_key or display_label or code or value_display):
            continue
        items.append(
            {
                "match_key": match_key,
                "characteristic_code": code,
                "display_label": display_label,
                "value_display": value_display,
            }
        )
    items.sort(
        key=lambda x: (
            x["match_key"],
            x["display_label"],
            x["characteristic_code"],
            x["value_display"],
        )
    )
    canonical = _json_dumps(items)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return digest, canonical


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


def record_eis_history_poll_run(
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
    with history_connect(path) as conn:
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
                ch_hash, _canonical = characteristics_set_hash(chars_list)
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_backfill_job(
    path: Path,
    *,
    org_regions: list[str],
    ktru_codes: list[str],
    document_type_44: str,
    document_types: list[str],
    date_from: date,
    date_to: date,
    date_to_max: date,
    lag_days: int,
    require_ezt_xml: bool,
    progress_total: int,
) -> int:
    with history_connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO backfill_jobs(
              created_at_utc, status, org_regions_json, ktru_codes_json,
              document_type_44, document_types_json, date_from, date_to, date_to_max_utc, lag_days,
              require_ezt_xml, progress_total
            )
            VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now_iso(),
                _json_dumps(org_regions),
                _json_dumps(ktru_codes),
                document_type_44,
                _json_dumps(document_types),
                date_from.isoformat(),
                date_to.isoformat(),
                date_to_max.isoformat(),
                int(lag_days),
                1 if require_ezt_xml else 0,
                int(progress_total),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def get_backfill_job(path: Path, job_id: int) -> dict[str, Any] | None:
    with history_connect(path) as conn:
        row = conn.execute("SELECT * FROM backfill_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def update_backfill_job(
    path: Path,
    job_id: int,
    *,
    status: str | None = None,
    started_at_utc: str | None = None,
    finished_at_utc: str | None = None,
    progress_done: int | None = None,
    stats_json: dict[str, Any] | None = None,
    last_error: str | None = None,
    cancel_requested: bool | None = None,
) -> None:
    fields: list[str] = []
    params: list[Any] = []
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if started_at_utc is not None:
        fields.append("started_at_utc = ?")
        params.append(started_at_utc)
    if finished_at_utc is not None:
        fields.append("finished_at_utc = ?")
        params.append(finished_at_utc)
    if progress_done is not None:
        fields.append("progress_done = ?")
        params.append(int(progress_done))
    if stats_json is not None:
        fields.append("stats_json = ?")
        params.append(_json_dumps(stats_json))
    if last_error is not None:
        fields.append("last_error = ?")
        params.append(last_error)
    if cancel_requested is not None:
        fields.append("cancel_requested = ?")
        params.append(1 if cancel_requested else 0)
    if not fields:
        return
    params.append(job_id)
    with history_connect(path) as conn:
        conn.execute(f"UPDATE backfill_jobs SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()


def job_cancel_requested(path: Path, job_id: int) -> bool:
    job = get_backfill_job(path, job_id)
    return bool(job and int(job.get("cancel_requested") or 0))


def get_checkpoint(
    path: Path, job_id: int, org_region: str, exact_date: str
) -> dict[str, Any] | None:
    day_key = exact_date_key(exact_date)
    with history_connect(path) as conn:
        row = conn.execute(
            """
            SELECT * FROM backfill_checkpoints
            WHERE job_id = ? AND org_region = ? AND exact_date = ?
            """,
            (job_id, org_region.strip(), day_key),
        ).fetchone()
    return dict(row) if row else None


def exact_date_key(exact_date: str) -> str:
    return str(exact_date or "").strip().split("T", 1)[0].split("+", 1)[0].split("Z", 1)[0]


def save_checkpoint(
    path: Path,
    job_id: int,
    org_region: str,
    exact_date: str,
    *,
    status: str,
    zips: int = 0,
    matches: int = 0,
    lines: int = 0,
    error_text: str | None = None,
) -> None:
    day_key = exact_date_key(exact_date)
    finished = _utc_now_iso()
    with history_connect(path) as conn:
        conn.execute(
            """
            INSERT INTO backfill_checkpoints(
              job_id, org_region, exact_date, status, zips, matches, lines,
              error_text, finished_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, org_region, exact_date) DO UPDATE SET
              status=excluded.status,
              zips=excluded.zips,
              matches=excluded.matches,
              lines=excluded.lines,
              error_text=excluded.error_text,
              finished_at_utc=excluded.finished_at_utc
            """,
            (
                job_id,
                org_region.strip(),
                day_key,
                status,
                int(zips),
                int(matches),
                int(lines),
                error_text,
                finished,
            ),
        )
        conn.commit()


def history_db_totals_for_ui(
    path: Path,
    job: dict[str, Any] | None = None,
    *,
    top_ktru_limit: int = 15,
) -> dict[str, Any]:
    """Fast totals for the web card: prefer snapshot from last done backfill job."""
    if job:
        try:
            stats = json.loads(str(job.get("stats_json") or "{}"))
        except json.JSONDecodeError:
            stats = {}
        db_tot = stats.get("db_totals")
        if isinstance(db_tot, dict) and int(db_tot.get("ktru_lines") or 0) > 0:
            top = db_tot.get("top_ktru")
            return {
                "notice_events": int(db_tot.get("notice_events") or 0),
                "ktru_lines": int(db_tot.get("ktru_lines") or 0),
                "unique_ktru_codes": int(db_tot.get("unique_ktru_codes") or 0),
                "top_ktru": top if isinstance(top, list) else [],
                "from_job_stats": True,
            }
    return history_db_totals(path, top_ktru_limit=top_ktru_limit)


def history_db_totals(path: Path, *, top_ktru_limit: int = 15) -> dict[str, Any]:
    if not path.is_file():
        return {"notice_events": 0, "ktru_lines": 0, "unique_ktru_codes": 0, "top_ktru": []}
    with history_connect(path) as conn:
        notices = int(conn.execute("SELECT COUNT(*) FROM notice_events").fetchone()[0])
        lines = int(conn.execute("SELECT COUNT(*) FROM ktru_lines").fetchone()[0])
        unique_ktru = int(conn.execute("SELECT COUNT(DISTINCT ktru_code) FROM ktru_lines").fetchone()[0])
        rows = conn.execute(
            """
            SELECT ktru_code, COUNT(*) AS line_count
            FROM ktru_lines
            GROUP BY ktru_code
            ORDER BY line_count DESC, ktru_code ASC
            LIMIT ?
            """,
            (max(1, int(top_ktru_limit)),),
        ).fetchall()
    return {
        "notice_events": notices,
        "ktru_lines": lines,
        "unique_ktru_codes": unique_ktru,
        "top_ktru": [{"ktru_code": r["ktru_code"], "line_count": int(r["line_count"])} for r in rows],
    }


def count_lines_for_ktru(path: Path, ktru_codes: list[str]) -> dict[str, int]:
    if not ktru_codes:
        return {}
    out: dict[str, int] = {}
    with history_connect(path) as conn:
        for code in ktru_codes:
            c = str(code or "").strip()
            if not c:
                continue
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ktru_lines WHERE ktru_code = ?",
                (c,),
            ).fetchone()
            out[c] = int(row["n"] if row else 0)
    return out


def checkpoint_summary_by_region(path: Path, job_id: int) -> list[dict[str, Any]]:
    with history_connect(path) as conn:
        rows = conn.execute(
            """
            SELECT org_region,
                   COUNT(*) AS cells,
                   SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS cells_ok,
                   SUM(CASE WHEN status = 'no_data' THEN 1 ELSE 0 END) AS cells_no_data,
                   SUM(zips) AS zips,
                   SUM(matches) AS matches
            FROM backfill_checkpoints
            WHERE job_id = ?
            GROUP BY org_region
            ORDER BY org_region ASC
            """,
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_backfill_job(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with history_connect(path) as conn:
        row = conn.execute(
            """
            SELECT * FROM backfill_jobs
            WHERE status = 'done'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


def get_current_backfill_job(path: Path) -> dict[str, Any] | None:
    """Latest queued or running backfill job."""
    if not path.is_file():
        return None
    with history_connect(path) as conn:
        row = conn.execute(
            """
            SELECT * FROM backfill_jobs
            WHERE status IN ('queued', 'running')
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


def get_latest_backfill_job_any(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with history_connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM backfill_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


_HISTORY_COVERAGE_CACHE: dict[str, tuple[int, tuple[date, date] | None]] = {}


def get_history_coverage_period(path: Path) -> tuple[date, date] | None:
    """Min..max period across all completed backfill jobs."""
    if not path.is_file():
        return None
    resolved = str(path.resolve())
    with history_connect(path) as conn:
        row = conn.execute("PRAGMA data_version").fetchone()
    data_version = int(row[0]) if row else 0
    cached = _HISTORY_COVERAGE_CACHE.get(resolved)
    if cached and cached[0] == data_version:
        return cached[1]
    result = _get_history_coverage_period_uncached(path)
    _HISTORY_COVERAGE_CACHE[resolved] = (data_version, result)
    return result


def _get_history_coverage_period_uncached(path: Path) -> tuple[date, date] | None:
    with history_connect(path) as conn:
        row = conn.execute(
            """
            SELECT MIN(date_from) AS date_from, MAX(date_to) AS date_to
            FROM backfill_jobs
            WHERE status = 'done'
            """
        ).fetchone()
    if not row or not row["date_from"] or not row["date_to"]:
        return None
    try:
        return date.fromisoformat(str(row["date_from"])), date.fromisoformat(str(row["date_to"]))
    except ValueError:
        return None


def history_backfill_status(path: Path) -> dict[str, Any]:
    active = get_current_backfill_job(path)
    done = get_latest_backfill_job(path)
    display = active or done
    progress_done = int((display or {}).get("progress_done") or 0)
    progress_total = int((display or {}).get("progress_total") or 0)
    progress_pct: float | None = None
    if progress_total > 0:
        progress_pct = round(100.0 * progress_done / progress_total, 2)
    return {
        "active_job": active,
        "latest_done_job": done,
        "display_job": display,
        "is_running": active is not None,
        "progress_done": progress_done,
        "progress_total": progress_total,
        "progress_pct": progress_pct,
    }


def label_quality_stats(path: Path, *, limit: int = 5000) -> dict[str, int]:
    """Rows where display_label equals characteristic_code (weak names in XML)."""
    weak = 0
    total = 0
    with history_connect(path) as conn:
        rows = conn.execute(
            """
            SELECT characteristics_json FROM ktru_lines
            WHERE characteristics_count > 0
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    for row in rows:
        try:
            chars = json.loads(str(row["characteristics_json"] or "[]"))
        except json.JSONDecodeError:
            continue
        if not isinstance(chars, list):
            continue
        for ch in chars:
            if not isinstance(ch, dict):
                continue
            total += 1
            label = str(ch.get("display_label") or "").strip()
            code = str(ch.get("characteristic_code") or "").strip()
            if code and label == code:
                weak += 1
    return {"characteristics_total": total, "weak_label_equals_code": weak}

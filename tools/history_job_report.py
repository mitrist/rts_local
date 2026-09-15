#!/usr/bin/env python3
"""Сводка по backfill job в eis_history.sqlite3."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eis_history_db import (
    checkpoint_summary_by_region,
    get_backfill_job,
    get_current_backfill_job,
    get_latest_backfill_job_any,
    history_backfill_status,
    history_db_path,
    history_db_totals,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--job-id", type=int, default=None)
    p.add_argument("--latest", action="store_true", help="Последний job любого статуса")
    p.add_argument("--active", action="store_true", help="Текущий running/queued job")
    p.add_argument("--status", action="store_true", help="Краткий статус (для мониторинга)")
    args = p.parse_args()
    db = history_db_path(args.db)
    if args.status:
        st = history_backfill_status(db)
        job = st.get("display_job")
        if not job:
            print("Нет jobs в БД")
            return 2
        print(
            f"job_id={job.get('id')} status={job.get('status')} "
            f"progress={st.get('progress_done')}/{st.get('progress_total')} "
            f"({st.get('progress_pct') or 0}%) running={st.get('is_running')}"
        )
        print(f"  period {job.get('date_from')} .. {job.get('date_to')}")
        if job.get("last_error"):
            print(f"  last_error: {str(job.get('last_error'))[:200]}")
        return 0
    if args.active:
        job = get_current_backfill_job(db)
        if not job:
            print("Нет активного backfill job")
            return 2
        job_id = int(job["id"])
    elif args.latest:
        job = get_latest_backfill_job_any(db)
        if not job:
            print(f"Jobs не найдены в {db}")
            return 2
        job_id = int(job["id"])
    else:
        job_id = args.job_id if args.job_id is not None else 1
        job = get_backfill_job(db, job_id)
    if not job:
        print(f"Job #{job_id} не найден в {db}")
        return 2
    print(f"Job #{job_id}: status={job.get('status')}")
    print(f"  period {job.get('date_from')} .. {job.get('date_to')} lag={job.get('lag_days')}")
    print(f"  progress {job.get('progress_done')}/{job.get('progress_total')}")
    print(f"  document_type_44: {job.get('document_type_44')}")
    print(f"  document_types_json: {job.get('document_types_json')}")
    totals = history_db_totals(db)
    print(f"DB totals: notices={totals.get('notice_events')} lines={totals.get('ktru_lines')}")
    rows = checkpoint_summary_by_region(db, job_id)
    print("By region:")
    for row in rows:
        print(
            f"  {row['org_region']}: zips={row.get('zips')} "
            f"ok={row.get('cells_ok')} no_data={row.get('cells_no_data')} cells={row.get('cells')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

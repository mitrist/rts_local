#!/usr/bin/env python3
"""Проверка покрытия eis_history.sqlite3 по периодам backfill."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eis_analytics_db import _history_period_purchase_count, history_analytics_v2_summary
from eis_history_db import get_history_coverage_period, get_latest_backfill_job, history_connect, history_db_path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=None)
    args = p.parse_args()
    db = history_db_path(args.db)
    if not db.is_file():
        print(f"Нет файла: {db}")
        return 2

    with history_connect(db) as conn:
        jobs = conn.execute(
            "SELECT id, status, date_from, date_to, progress_done, progress_total FROM backfill_jobs ORDER BY id"
        ).fetchall()
        row = conn.execute(
            """
            SELECT COUNT(*) AS notices,
                   (SELECT COUNT(*) FROM ktru_lines) AS lines,
                   (SELECT COUNT(DISTINCT ktru_code) FROM ktru_lines) AS ktru
            FROM notice_events
            WHERE document_type_44 = 'epNotificationEZT2020'
            """
        ).fetchone()

    print(f"DB: {db}")
    print(f"EZT notices={row['notices']} lines={row['lines']} unique_ktru={row['ktru']}")
    for j in jobs:
        print(
            f"  job #{j['id']} {j['status']} {j['date_from']}..{j['date_to']} "
            f"{j['progress_done']}/{j['progress_total']}"
        )

    cov = get_history_coverage_period(db)
    latest = get_latest_backfill_job(db)
    print(f"coverage: {cov}")
    print(f"latest_done: job #{latest.get('id') if latest else None}")

    if cov:
        d0, d1 = cov
        for label, a, b in (
            ("full_coverage", d0, d1),
            ("2025H2", date(2025, 6, 1), date(2025, 12, 31)),
            ("2026_H1", date(2026, 1, 1), date(2026, 5, 31)),
        ):
            n = _history_period_purchase_count(db, date_from=a, date_to=b)
            s = history_analytics_v2_summary(db, date_from=a, date_to=b)
            print(
                f"  {label}: purchases={n} analytics_kpi={s['kpi'].get('total_purchases')} "
                f"top_ktru={len(s['top_ktru'])}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

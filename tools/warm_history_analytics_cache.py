"""Pre-build on-disk analytics cache for the default history coverage period."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eis_analytics_db import history_analytics_v2_summary, history_dashboard_summary
from eis_history_db import get_history_coverage_period, history_db_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="",
        help="Path to eis_history.sqlite3 (default: EIS_HISTORY_DB or ./eis_history.sqlite3)",
    )
    args = parser.parse_args()
    raw = (args.db or os.environ.get("EIS_HISTORY_DB", "")).strip()
    path = history_db_path(raw) if raw else history_db_path()
    if not path.is_file():
        print(f"DB not found: {path}")
        return 1
    coverage = get_history_coverage_period(path)
    if not coverage:
        print("No done backfill jobs; skip warm-up.")
        return 0
    date_from, date_to = coverage
    print(f"Warming cache for {path} ({date_from} .. {date_to})")
    t0 = time.perf_counter()
    history_analytics_v2_summary(path, date_from=date_from, date_to=date_to)
    t1 = time.perf_counter()
    print(f"  analysis_v2: {t1 - t0:.1f}s")
    t0 = time.perf_counter()
    history_dashboard_summary(path, date_from=date_from, date_to=date_to)
    print(f"  dashboard: {time.perf_counter() - t0:.1f}s")
    cache_dir = path.parent / ".history_analytics_cache"
    n = len(list(cache_dir.glob("*.json"))) if cache_dir.is_dir() else 0
    print(f"Cache files: {n} in {cache_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

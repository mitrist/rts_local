"""Backfill EIS↔PP hypothesis candidates (exact match, ±window) for recent notices."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eis_analytics_db import analytics_db_path
from eis_pp_link_service import backfill_pp_eis_links, pp_eis_link_window_min


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill EIS-PP link hypothesis candidates")
    parser.add_argument("--days", type=int, default=7, help="Notice publication lookback days")
    parser.add_argument("--meta-dir", type=str, default=os.environ.get("RTS_META_DIR", ""))
    parser.add_argument("--db", type=str, default="")
    parser.add_argument(
        "--history-db",
        type=str,
        default=os.environ.get("EIS_HISTORY_DB", ""),
        help="Optional eis_history.sqlite3 for characteristic enrichment",
    )
    args = parser.parse_args()

    meta = Path(args.meta_dir) if args.meta_dir else analytics_db_path().parent
    db = Path(args.db) if args.db else analytics_db_path()
    history = Path(args.history_db) if args.history_db else None
    if history is not None and not history.is_file():
        history = None

    result = backfill_pp_eis_links(
        db,
        meta_dir=meta,
        days=args.days,
        history_path=history,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"window_min=±{pp_eis_link_window_min()}")


if __name__ == "__main__":
    main()

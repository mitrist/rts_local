"""Historical EIS↔PP scan: registry without price filter, exact match + ±window."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eis_analytics_db import analytics_db_path, init_db, connect
from eis_pp_link_service import pp_eis_link_window_min, scan_historical_pp_eis_links


def main() -> None:
    parser = argparse.ArgumentParser(description="Historical EIS-PP scan (exact + ±window, no price filter)")
    parser.add_argument("--days", type=int, default=30, help="Notice publication lookback days")
    parser.add_argument("--meta-dir", type=str, default=os.environ.get("RTS_META_DIR", ""))
    parser.add_argument("--db", type=str, default="")
    parser.add_argument("--history-db", type=str, default=os.environ.get("EIS_HISTORY_DB", ""))
    parser.add_argument("--fetch-delay", type=float, default=0.25, help="Delay between Find API calls")
    parser.add_argument("--max-fetch-per-ktru", type=int, default=0, help="0 = unlimited")
    args = parser.parse_args()

    meta = Path(args.meta_dir) if args.meta_dir else analytics_db_path().parent
    db = Path(args.db) if args.db else analytics_db_path()
    history = Path(args.history_db) if args.history_db else None
    if history is not None and not history.is_file():
        history = None

    with connect(db) as conn:
        init_db(conn)

    max_fetch = int(args.max_fetch_per_ktru) if int(args.max_fetch_per_ktru) > 0 else None
    result = scan_historical_pp_eis_links(
        db,
        meta_dir=meta,
        days=args.days,
        history_path=history,
        fetch_delay_sec=max(0.0, float(args.fetch_delay)),
        max_fetch_per_ktru=max_fetch,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"window_min=±{pp_eis_link_window_min()}")


if __name__ == "__main__":
    main()

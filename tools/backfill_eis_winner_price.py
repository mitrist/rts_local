#!/usr/bin/env python3
"""Backfill EIS winner-price matches for pilot KTRU notices."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from eis_analytics_db import analytics_db_path
from eis_pp_link_service import backfill_eis_winner_price_links, pp_eis_winner_search_max


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill EIS winner-price PP matches")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--meta-dir", type=str, default=os.environ.get("RTS_META_DIR", ""))
    parser.add_argument("--db", type=str, default="")
    parser.add_argument("--history-db", type=str, default=os.environ.get("EIS_HISTORY_DB", ""))
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    meta = Path(args.meta_dir) if args.meta_dir else analytics_db_path().parent
    db = Path(args.db) if args.db else analytics_db_path()
    history = Path(args.history_db) if args.history_db and Path(args.history_db).is_file() else None
    result = backfill_eis_winner_price_links(
        db,
        meta_dir=meta,
        days=args.days,
        history_path=history,
        force_refresh=args.force_refresh,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"search_max={pp_eis_winner_search_max()}")


if __name__ == "__main__":
    main()

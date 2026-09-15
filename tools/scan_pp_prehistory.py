"""Scan EIS↔PP matches for RTS PP prehistory (hash + EIS region)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eis_analytics_db import analytics_db_path, list_pp_eis_matches
from pp_prehistory import find_predecessors


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan PP prehistory for EIS↔PP matches")
    parser.add_argument("--db", type=str, default="")
    parser.add_argument("--all-exact", action="store_true", help="All is_exact_match rows")
    parser.add_argument("--historical-only", action="store_true", help="Only match_origin=historical")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    db = Path(args.db) if args.db else analytics_db_path()
    matches = list_pp_eis_matches(db, limit=max(1, int(args.limit)), offset=0, exact_only=True)
    if args.historical_only:
        matches = [m for m in matches if str(m.get("match_origin") or "") == "historical"]
    elif not args.all_exact:
        matches = [m for m in matches if str(m.get("match_origin") or "") == "historical"]

    report: list[dict] = []
    for match in matches:
        ph = find_predecessors(db, match)
        report.append(
            {
                "purchase_number": match.get("purchase_number"),
                "offer_price_list_item_id": match.get("offer_price_list_item_id"),
                "lag_minutes": match.get("lag_minutes"),
                "match_origin": match.get("match_origin"),
                "prehistory_count": ph.predecessor_count,
                "has_prehistory": ph.has_prehistory,
                "skipped_no_find": ph.skipped_no_find,
                "notice_region": ph.notice_region,
                "anchor_unit_price": ph.anchor_unit_price,
                "predecessors": [p.as_dict() for p in ph.predecessors[:5]],
                "error": ph.error,
            }
        )
    summary = {
        "scanned": len(report),
        "with_prehistory": sum(1 for r in report if r.get("has_prehistory")),
        "total_predecessors": sum(int(r.get("prehistory_count") or 0) for r in report),
    }
    print(json.dumps({"summary": summary, "rows": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Batch recalculate EIS procedure outcomes (journal-driven, not real-time)."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from eis_analytics_db import msk_day_bounds_utc
from eis_changelog import MSK
from eis_pp_link_service import backfill_eis_outcomes

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_msk_date(text: str) -> date:
    raw = str(text or "").strip()
    if not _DATE_RE.match(raw):
        raise ValueError(f"expected YYYY-MM-DD, got {text!r}")
    return date.fromisoformat(raw)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch recalculate EIS procedure outcomes (journal-driven, not real-time)",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--yesterday",
        action="store_true",
        help="Recalculate notices published yesterday (MSK calendar day)",
    )
    scope.add_argument(
        "--date",
        type=str,
        default="",
        help="Recalculate notices for a specific MSK calendar day (YYYY-MM-DD)",
    )
    scope.add_argument("--days", type=int, default=0, help="Rolling lookback window (days)")
    scope.add_argument("--purchase", type=str, default="", help="Single purchase number")
    parser.add_argument(
        "--db",
        type=str,
        default=os.environ.get("EIS_ANALYTICS_DB", "/var/lib/rts_tender/eis_analytics.sqlite3"),
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Always re-fetch EIS journal (implicit for --yesterday/--date)",
    )
    args = parser.parse_args()
    db = Path(args.db)
    purchase = str(args.purchase or "").strip() or None

    target_day: date | None = None
    pub_from: str | None = None
    pub_to: str | None = None
    days = max(0, int(args.days or 0))
    force_refresh = bool(args.force_refresh)

    if args.yesterday:
        target_day = datetime.now(MSK).date() - timedelta(days=1)
    elif args.date:
        target_day = _parse_msk_date(args.date)

    if target_day is not None:
        if purchase:
            parser.error("--purchase cannot be combined with --yesterday/--date")
        pub_from, pub_to = msk_day_bounds_utc(target_day)
        force_refresh = True
    elif not days and not purchase:
        days = 30

    result = backfill_eis_outcomes(
        db,
        days=days or 30,
        pub_from_utc=pub_from,
        pub_to_utc=pub_to,
        force_refresh=force_refresh,
        purchase_number=purchase,
    )
    if target_day is not None:
        result["msk_day"] = target_day.isoformat()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

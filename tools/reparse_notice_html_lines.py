#!/usr/bin/env python3
"""Refresh notice_html_ktru_lines from stored notice_html_pages HTML."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from eis_analytics_db import analytics_db_path, reparse_notice_html_ktru_lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("purchase_number", nargs="?", help="One purchase number; omit with --all")
    parser.add_argument(
        "--meta",
        type=Path,
        default=Path("/var/lib/rts_tender"),
        help="Metadata directory with eis_analytics.sqlite3",
    )
    parser.add_argument("--all", action="store_true", help="Reparse every loaded notice HTML page")
    args = parser.parse_args()

    db = analytics_db_path(args.meta)
    if args.all:
        import sqlite3

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT purchase_number FROM notice_html_pages WHERE ok = 1 ORDER BY purchase_number"
        ).fetchall()
        total = 0
        for (pn,) in rows:
            total += reparse_notice_html_ktru_lines(db, str(pn))
        print(f"reparsed {len(rows)} purchases, {total} lines")
        return 0

    pn = str(args.purchase_number or "").strip()
    if not pn:
        parser.error("purchase_number is required unless --all is set")
    count = reparse_notice_html_ktru_lines(db, pn)
    print(f"{pn}: {count} line(s)")
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())

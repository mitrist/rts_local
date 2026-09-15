#!/usr/bin/env python3
"""Compare EIS notice characteristics with live RTS PP draft."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eis_analytics_db import analytics_db_path, enrich_notice_characteristics, get_rts_pp_draft, notice_line_for_pp
from notice_to_rts_pp import compare_notice_to_draft
from rts_ktru_cache import cache_dir_default, load_cached_template
from rts_pp_api import RtsPpApiError, get_draft, load_offers_cookie


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("purchase_number")
    parser.add_argument("line_key")
    parser.add_argument("--meta-dir", type=Path, default=Path("/var/lib/rts_tender"))
    parser.add_argument("--cookie-file", type=Path, default=Path("/etc/rts-tender/cookies_rts.txt"))
    parser.add_argument("--draft-id", type=int, default=0, help="Override draft id from DB")
    parser.add_argument("--with-ids", action="store_true", help="Show preset ids in output")
    args = parser.parse_args(argv)

    db = analytics_db_path(args.meta_dir)
    line = notice_line_for_pp(db, args.purchase_number, args.line_key)
    if not line:
        print("position not found", file=sys.stderr)
        return 2

    ktru_code = str(line.get("ktru_code") or "").strip()
    notice_chars = enrich_notice_characteristics(
        db,
        purchase_number=args.purchase_number,
        ktru_code=ktru_code,
        html_chars=line.get("characteristics") or [],
    )
    cache = cache_dir_default(args.meta_dir)
    ktru_item = load_cached_template(cache, ktru_code, ttl_sec=0)

    draft_id = args.draft_id
    if not draft_id:
        record = get_rts_pp_draft(db, args.purchase_number, args.line_key)
        if not record or not record.get("rts_draft_id"):
            print("no rts_pp_drafts record", file=sys.stderr)
            return 2
        draft_id = int(record["rts_draft_id"])

    try:
        cookie = load_offers_cookie(args.cookie_file)
        draft_item = get_draft(cookie, draft_id, timeout=30)
    except RtsPpApiError as exc:
        print(exc, file=sys.stderr)
        return 2

    report = compare_notice_to_draft(
        notice_chars,
        draft_item,
        ktru_item=ktru_item,
        include_id_in_render=args.with_ids,
    )
    headers = ("status", "label", "notice", "draft", "detail")
    print("\t".join(headers))
    for row in report.get("rows") or []:
        print(
            "\t".join(
                [
                    str(row.get("status") or ""),
                    str(row.get("notice_label") or row.get("rts_name") or ""),
                    "; ".join(row.get("notice_values") or []),
                    "; ".join(row.get("draft_values") or []),
                    str(row.get("detail") or ""),
                ]
            )
        )
    print(
        f"\nok={report.get('ok', 0)} issues={report.get('issues', 0)} total={report.get('total', 0)}",
        file=sys.stderr,
    )
    return 1 if int(report.get("issues") or 0) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())

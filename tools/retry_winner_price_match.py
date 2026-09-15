"""Retry winner-price PP search for one EIS notice line."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _default_db() -> Path:
    raw = str(os.environ.get("EIS_ANALYTICS_DB") or "").strip()
    if raw:
        return Path(raw)
    return Path("/var/lib/rts_tender/eis_analytics.sqlite3")


def _default_meta() -> Path:
    raw = str(os.environ.get("RTS_META_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path("/var/lib/rts_tender/meta")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("purchase_number", help="EIS purchase number")
    parser.add_argument("--line-key", default="", help="Notice line key (optional if single pilot line)")
    parser.add_argument("--db", type=Path, default=_default_db())
    parser.add_argument("--meta-dir", type=Path, default=_default_meta())
    parser.add_argument("--force-refresh", action="store_true", help="Re-fetch EIS outcome before search")
    args = parser.parse_args()

    from eis_analytics_db import connect, get_notice_eis_outcome, list_pp_eis_matches
    from eis_pp_link_service import match_notice_by_winner_price, pilot_ktru_codes

    pn = str(args.purchase_number).strip()
    with connect(args.db) as conn:
        if args.line_key:
            line = conn.execute(
                """
                SELECT line_key, ktru_code, quantity_text, characteristics_json
                FROM notice_html_ktru_lines
                WHERE purchase_number = ? AND line_key = ?
                """,
                (pn, args.line_key),
            ).fetchone()
        else:
            pilot = tuple(pilot_ktru_codes())
            placeholders = ",".join("?" for _ in pilot)
            line = conn.execute(
                f"""
                SELECT line_key, ktru_code, quantity_text, characteristics_json
                FROM notice_html_ktru_lines
                WHERE purchase_number = ? AND ktru_code IN ({placeholders})
                ORDER BY line_key
                LIMIT 1
                """,
                (pn, *pilot),
            ).fetchone()

    if not line:
        print(json.dumps({"ok": False, "error": "line_not_found"}, ensure_ascii=False))
        return 1

    lk = str(line[0] or "")
    ktru_code = str(line[1] or "")
    quantity_text = str(line[2] or "")
    try:
        html_chars = json.loads(str(line[3] or "[]"))
    except json.JSONDecodeError:
        html_chars = []

    saved = match_notice_by_winner_price(
        args.db,
        meta_dir=args.meta_dir,
        purchase_number=pn,
        line_key=lk,
        ktru_code=ktru_code,
        html_chars=html_chars if isinstance(html_chars, list) else None,
        quantity_text=quantity_text,
        force_refresh=args.force_refresh,
        skip_outcome_refresh=not args.force_refresh,
    )
    outcome = get_notice_eis_outcome(args.db, purchase_number=pn, line_key=lk)
    matches = list_pp_eis_matches(
        args.db,
        limit=5,
        purchase_number=pn,
        line_key=lk,
        exact_only=True,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "purchase_number": pn,
                "line_key": lk,
                "ktru_code": ktru_code,
                "matches_saved": saved,
                "outcome": outcome,
                "matches": matches,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if saved else 2


if __name__ == "__main__":
    raise SystemExit(main())

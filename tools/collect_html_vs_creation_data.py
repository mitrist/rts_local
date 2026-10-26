#!/usr/bin/env python3
"""Collect HTML vs RTS creation-data diffs for PP-from-HTML calibration."""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date, datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eis_analytics_db import (  # noqa: E402
    analytics_db_path,
    list_collector_candidates,
    save_creation_data_snapshot,
    save_html_creation_diffs,
)
from html_creation_compare import (  # noqa: E402
    aggregate_pattern_summary,
    compare_html_to_creation_data,
)
from rts_cookie_profiles import cookie_path_for_profile, normalize_rts_profile  # noqa: E402
from rts_ktru_cache import cache_dir_default, load_cached_template  # noqa: E402
from rts_pp_api import RtsPpApiError, load_offers_cookie, resolve_offer_creation_data  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")


def _parse_since(raw: str | None) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if "T" in s or " " in s:
        return s.replace(" ", "T")
    try:
        day = date.fromisoformat(s)
    except ValueError as exc:
        raise SystemExit(f"Bad --since: {s}") from exc
    start = datetime.combine(day, dt_time.min, tzinfo=MSK).astimezone(
        ZoneInfo("UTC")
    ).replace(microsecond=0)
    return start.isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-dir", type=Path, default=Path("/var/lib/rts_local"))
    parser.add_argument("--db", type=Path, default=None, help="Override analytics sqlite path")
    parser.add_argument("--cookie-file", type=Path, default=None)
    parser.add_argument("--rts-profile", default="IPSokolova")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--purchase-number", default="")
    parser.add_argument("--ktru", default="")
    parser.add_argument("--since", default="", help="MSK date YYYY-MM-DD or ISO UTC")
    parser.add_argument("--sleep", type=float, default=0.4)
    parser.add_argument("--out-csv", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-existing-snapshot",
        action="store_true",
        help="Skip lines that already have a creation-data snapshot for same KTRU",
    )
    parser.add_argument(
        "--with-draft",
        action="store_true",
        help="Only lines that already have a created rts_pp_draft (creation-data likely available)",
    )
    parser.add_argument(
        "--oldest-first",
        action="store_true",
        help="Prefer older notices (more likely to have RTS creation-data)",
    )
    args = parser.parse_args(argv)

    meta = args.meta_dir
    db = args.db if args.db else analytics_db_path(meta)
    if not db.is_file():
        print(f"DB not found: {db}", file=sys.stderr)
        return 2

    profile = normalize_rts_profile(args.rts_profile)
    cookie_path = args.cookie_file or cookie_path_for_profile(profile, meta, _ROOT)
    pub_from = _parse_since(args.since or None)

    candidates = list_collector_candidates(
        db,
        limit=max(1, int(args.limit)),
        purchase_number=str(args.purchase_number or "").strip() or None,
        ktru_code=str(args.ktru or "").strip() or None,
        pub_from_utc=pub_from,
        require_no_snapshot=bool(args.skip_existing_snapshot),
        require_created_draft=bool(args.with_draft),
        oldest_first=bool(args.oldest_first),
    )
    print(f"candidates={len(candidates)} db={db}")
    if args.dry_run:
        for row in candidates[:20]:
            print(
                f"  {row.get('purchase_number')} | {row.get('ktru_code')} | "
                f"pos={row.get('position_index')} | {row.get('line_key')}"
            )
        if len(candidates) > 20:
            print(f"  ... +{len(candidates) - 20} more")
        return 0

    if not cookie_path or not Path(cookie_path).is_file():
        print(f"cookie file missing: {cookie_path}", file=sys.stderr)
        return 2

    try:
        cookie = load_offers_cookie(Path(cookie_path))
    except Exception as exc:  # noqa: BLE001
        print(f"cookie load failed: {exc}", file=sys.stderr)
        return 2

    cache = cache_dir_default(meta)
    all_diff_rows: list[dict] = []
    notices_risk = 0
    notices_ok = 0
    errors = 0

    for idx, cand in enumerate(candidates):
        pn = str(cand.get("purchase_number") or "").strip()
        lk = str(cand.get("line_key") or "").strip()
        ktru = str(cand.get("ktru_code") or "").strip()
        try:
            preferred = int(cand.get("position_index") or 0) + 1
        except (TypeError, ValueError):
            preferred = 1
        if idx > 0 and args.sleep > 0:
            time.sleep(float(args.sleep))
        try:
            creation_data, star = resolve_offer_creation_data(
                cookie,
                pn,
                expected_ktru=ktru,
                preferred_lot_index=max(1, preferred),
                empty_backoffs=(),  # one pass for batch speed
                sleep=lambda _s: None,
            )
        except RtsPpApiError as exc:
            errors += 1
            print(f"FAIL {pn}/{ktru}: {exc.reason} {exc}", file=sys.stderr)
            continue

        save_creation_data_snapshot(
            db,
            purchase_number=pn,
            star=star,
            creation_data=creation_data,
            source="collector",
        )
        ktru_item = load_cached_template(cache, ktru, ttl_sec=0) if ktru else None
        report = compare_html_to_creation_data(
            cand.get("characteristics") or [],
            creation_data,
            ktru_item=ktru_item,
        )
        diff_rows = report.get("rows") or []
        save_html_creation_diffs(
            db,
            purchase_number=pn,
            line_key=lk,
            star=star,
            rows=diff_rows,
        )
        notices_ok += 1
        if (report.get("summary") or {}).get("has_create_risk"):
            notices_risk += 1
        for row in diff_rows:
            annotated = dict(row)
            annotated["purchase_number"] = pn
            annotated["line_key"] = lk
            annotated["star"] = star
            all_diff_rows.append(annotated)
        summary = report.get("summary") or {}
        print(
            f"OK {pn} star={star} rows={summary.get('total_rows')} "
            f"by_status={summary.get('by_status')} risk={summary.get('has_create_risk')}"
        )

    patterns = aggregate_pattern_summary(
        all_diff_rows,
        notices_with_risk=notices_risk,
        notices_total=notices_ok,
    )
    print("--- pattern summary ---")
    for p in patterns:
        print(
            f"{p['status']}\tcount={p['count']}\tshare={p['share']}\t"
            f"{p.get('sample_html_label') or ''}\t{p.get('sample_detail') or ''}"
        )
    print(f"done ok={notices_ok} errors={errors} risk_notices={notices_risk}")

    out_csv = args.out_csv
    if out_csv is None:
        exports = _ROOT / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        out_csv = exports / "html_creation_patterns.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "status",
                "count",
                "share",
                "sample_purchase",
                "sample_html_label",
                "sample_detail",
            ],
        )
        w.writeheader()
        w.writerows(patterns)
    print(f"wrote {out_csv}")
    return 0 if errors == 0 or notices_ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

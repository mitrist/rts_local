#!/usr/bin/env python3
"""Refresh cached_at for KTRU templates whose TTL expired but JSON item is still valid."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rts_ktru_cache import (  # noqa: E402
    _DEFAULT_TTL_SEC,
    _cache_path,
    get_ktru_template,
    load_cached_template,
    save_cached_template,
)
from rts_pp_api import RtsPpApiError, load_offers_cookie  # noqa: E402
from rts_preoffers import load_preoffers  # noqa: E402


def _catalog_codes(catalog: Path) -> set[str]:
    return {o.ktru_code for o in load_preoffers(catalog) if o.ktru_code}


def _read_item(path: Path, ktru_code: str) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    item = raw.get("item")
    if isinstance(item, dict) and str(item.get("code") or "").strip() == ktru_code:
        return item
    if str(raw.get("code") or "").strip() == ktru_code:
        return raw
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Optional rts_preoffers.json — refresh only catalog codes",
    )
    parser.add_argument("--ttl-sec", type=int, default=_DEFAULT_TTL_SEC)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--fetch-missing",
        action="store_true",
        help="After TTL refresh, fetch catalog codes absent from cache via RTS API",
    )
    parser.add_argument("--cookie-file", type=Path, help="Required with --fetch-missing")
    parser.add_argument("--delay", type=float, default=0.6, help="Pause between RTS fetches")
    args = parser.parse_args(argv)

    cache_dir = args.cache_dir.resolve()
    if not cache_dir.is_dir():
        print(f"Cache dir not found: {cache_dir}", file=sys.stderr)
        return 1

    catalog_codes = _catalog_codes(args.catalog.resolve()) if args.catalog else None
    now = time.time()

    refreshed: list[str] = []
    still_fresh: list[str] = []
    invalid: list[str] = []
    missing_from_catalog: list[str] = []

    paths = sorted(p for p in cache_dir.glob("*.json") if p.name != "import_report.json")
    seen_codes: set[str] = set()

    for path in paths:
        code = path.stem
        seen_codes.add(code)
        if catalog_codes is not None and code not in catalog_codes:
            continue

        fresh = load_cached_template(cache_dir, code, ttl_sec=args.ttl_sec)
        if fresh is not None:
            still_fresh.append(code)
            continue

        item = _read_item(path, code)
        if item is None:
            invalid.append(code)
            continue

        if not args.dry_run:
            save_cached_template(cache_dir, code, item)
        refreshed.append(code)

    if catalog_codes is not None:
        missing_from_catalog = sorted(catalog_codes - seen_codes)

    print(f"cache: {cache_dir}")
    print(f"ttl_sec: {args.ttl_sec} ({args.ttl_sec // 86400} days)")
    print(f"still_fresh: {len(still_fresh)}")
    print(f"refreshed:   {len(refreshed)}")
    print(f"invalid:     {len(invalid)}")
    if catalog_codes is not None:
        print(f"missing:     {len(missing_from_catalog)}")
    if args.dry_run:
        print("(dry-run, files not written)")
    if refreshed[:5]:
        print("sample refreshed:", ", ".join(refreshed[:5]))
    if invalid[:5]:
        print("sample invalid:", ", ".join(invalid[:5]))
    if missing_from_catalog[:5]:
        print("sample missing:", ", ".join(missing_from_catalog[:5]))
    if missing_from_catalog:
        print("missing codes:")
        for code in missing_from_catalog:
            print(f"  {code}")

    if args.fetch_missing and missing_from_catalog:
        if args.dry_run:
            print("fetch-missing skipped in dry-run")
            return 0
        if not args.cookie_file:
            print("--cookie-file required with --fetch-missing", file=sys.stderr)
            return 1
        try:
            cookie = load_offers_cookie(args.cookie_file.resolve())
        except RtsPpApiError as exc:
            print(f"Cookie error: {exc}", file=sys.stderr)
            return 1
        fetched = 0
        failed: list[str] = []
        for idx, code in enumerate(missing_from_catalog, start=1):
            print(f"[{idx}/{len(missing_from_catalog)}] FETCH {code} ...", end=" ", flush=True)
            try:
                item = get_ktru_template(cookie, code, cache_dir, ttl_sec=0, allow_stub=False)
                print(f"OK chars={len(item.get('characteristics') or [])}")
                fetched += 1
            except RtsPpApiError as exc:
                print(f"FAIL ({exc.reason})")
                failed.append(code)
            if idx < len(missing_from_catalog) and args.delay > 0:
                time.sleep(args.delay)
        print(f"fetch-missing: ok={fetched} failed={len(failed)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

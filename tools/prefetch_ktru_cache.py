#!/usr/bin/env python3
"""Prefetch RTS Ktru/search templates for codes from the preoffers catalog."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import requests

from rts_ktru_cache import cache_dir_default, get_ktru_template, load_cached_template
from rts_pp_api import (
    KTRU_SEARCH_URL,
    RtsPpApiError,
    load_offers_cookie,
    offers_headers,
    probe_offers_session,
)
from rts_preoffers import load_preoffers


def _probe_ktru_dictionary(cookie: str, test_code: str) -> dict:
    """One-shot Ktru/search probe (separate from draft/search probe)."""
    body = {"code": test_code, "page": 0, "size": 20, "onlyActual": True}
    try:
        resp = requests.post(
            KTRU_SEARCH_URL,
            headers=offers_headers(cookie),
            json=body,
            timeout=30,
        )
    except requests.RequestException as exc:
        return {"ok": False, "reason": "network_error", "detail": str(exc)[:300]}
    if resp.status_code in (401, 403):
        return {"ok": False, "reason": "http_auth", "http_status": resp.status_code}
    if resp.status_code != 200:
        return {
            "ok": False,
            "reason": "http_error",
            "http_status": resp.status_code,
            "detail": (resp.text or "")[:300],
        }
    try:
        data = resp.json() if resp.text else {}
    except ValueError:
        return {"ok": False, "reason": "not_json", "detail": (resp.text or "")[:300]}
    page = data.get("page") if isinstance(data, dict) else None
    page_len = len(page) if isinstance(page, list) else 0
    hit = None
    if isinstance(page, list):
        for row in page:
            if isinstance(row, dict) and str(row.get("code") or "").strip() == test_code:
                hit = row
                break
    return {
        "ok": page_len > 0 and hit is not None,
        "http_status": resp.status_code,
        "total": data.get("total") if isinstance(data, dict) else None,
        "page_len": page_len,
        "test_code": test_code,
        "hit_name": hit.get("name") if hit else None,
        "hit_chars": len(hit.get("characteristics") or []) if hit else 0,
        "reason": "empty_page" if page_len == 0 else ("code_missing" if not hit else "ok"),
    }


def _catalog_ktru_codes(catalog: Path, *, enabled_only: bool = True) -> list[str]:
    offers = load_preoffers(catalog)
    if not enabled_only:
        from rts_preoffers import load_preoffers_raw

        raw = load_preoffers_raw(catalog).get("offers") or []
        codes = {
            str(x.get("ktru_code") or "").strip()
            for x in raw
            if isinstance(x, dict) and str(x.get("ktru_code") or "").strip()
        }
        return sorted(codes)
    return sorted({o.ktru_code for o in offers if o.ktru_code})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=_ROOT / "rts_preoffers.json",
        help="Path to rts_preoffers.json",
    )
    parser.add_argument("--cookie-file", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, help="Output ktru_rts_cache directory")
    parser.add_argument("--meta-dir", type=Path, default=_ROOT, help="Used if --cache-dir omitted")
    parser.add_argument("--delay", type=float, default=0.6, help="Pause between API calls (sec)")
    parser.add_argument("--ttl-sec", type=int, default=7 * 24 * 3600, help="Skip cache younger than TTL")
    parser.add_argument("--skip-cached", action="store_true", help="Skip codes already in cache")
    parser.add_argument("--limit", type=int, default=0, help="Max codes to process (0 = all)")
    parser.add_argument("--report-json", type=Path, help="Write summary JSON to file")
    parser.add_argument(
        "--test-code",
        help="KTRU for dictionary preflight (default: first catalog code)",
    )
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="Skip session/dictionary preflight checks",
    )
    args = parser.parse_args(argv)

    catalog = args.catalog.resolve()
    cache = (args.cache_dir or cache_dir_default(args.meta_dir)).resolve()
    cache.mkdir(parents=True, exist_ok=True)

    codes = _catalog_ktru_codes(catalog)
    if args.limit > 0:
        codes = codes[: args.limit]

    if not codes:
        print(f"No KTRU codes in catalog: {catalog}", file=sys.stderr)
        return 1

    try:
        cookie = load_offers_cookie(args.cookie_file)
    except RtsPpApiError as exc:
        print(f"Cookie error: {exc}", file=sys.stderr)
        return 1

    print(f"catalog: {catalog} ({len(codes)} codes)")
    print(f"cache:   {cache}")

    if not args.no_preflight:
        draft_probe = probe_offers_session(cookie)
        test_code = (args.test_code or codes[0]).strip()
        ktru_probe = _probe_ktru_dictionary(cookie, test_code)
        print(f"draft probe: {draft_probe}")
        print(f"ktru probe:  {ktru_probe}")
        if not draft_probe.get("ok"):
            print(
                "\nСессия РТС не прошла проверку (401/403). "
                "Обновите cookie с offers.rts-tender.ru (DevTools → Network → Cookie).",
                file=sys.stderr,
            )
            return 1
        if not ktru_probe.get("ok"):
            print(
                f"\nKtru/search недоступен для {test_code}: {ktru_probe.get('reason')}. "
                "Скопируйте cookie из запроса Ktru/search в браузере (не только draft/search). "
                "Пока page пустой — массовый опрос бессмысленен.",
                file=sys.stderr,
            )
            return 1

    ok: list[dict] = []
    skipped: list[str] = []
    failed: list[dict] = []

    for idx, code in enumerate(codes, start=1):
        if args.skip_cached and load_cached_template(cache, code, ttl_sec=args.ttl_sec):
            skipped.append(code)
            print(f"[{idx}/{len(codes)}] SKIP {code} (cached)")
            continue

        print(f"[{idx}/{len(codes)}] FETCH {code} ...", end=" ", flush=True)
        try:
            item = get_ktru_template(
                cookie,
                code,
                cache,
                allow_stub=False,
                ttl_sec=0,
            )
            n_chars = len(item.get("characteristics") or [])
            print(f"OK v{item.get('version')} chars={n_chars} — {item.get('name') or ''}")
            ok.append(
                {
                    "ktru_code": code,
                    "version": item.get("version"),
                    "name": item.get("name"),
                    "characteristics": n_chars,
                }
            )
        except RtsPpApiError as exc:
            print(f"FAIL ({exc.reason})")
            failed.append({"ktru_code": code, "reason": exc.reason, "message": str(exc)[:300]})
            if exc.reason in ("session_expired", "no_cookie"):
                print("Остановка: сессия истекла.", file=sys.stderr)
                break
        except (OSError, ValueError) as exc:
            print(f"FAIL ({exc})")
            failed.append({"ktru_code": code, "reason": "error", "message": str(exc)[:300]})

        if idx < len(codes) and args.delay > 0:
            time.sleep(args.delay)

    summary = {
        "catalog": str(catalog),
        "cache_dir": str(cache),
        "total": len(codes),
        "ok": len(ok),
        "skipped": len(skipped),
        "failed": len(failed),
        "ok_items": ok,
        "failed_items": failed,
        "skipped_codes": skipped,
    }

    print()
    print(f"Done: ok={len(ok)} skipped={len(skipped)} failed={len(failed)} / {len(codes)}")

    if args.report_json:
        args.report_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report: {args.report_json}")

    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())

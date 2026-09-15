#!/usr/bin/env python3
"""Fetch KTRU template from RTS API (or import JSON) into local cache."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rts_ktru_cache import cache_dir_default, get_ktru_template, import_ktru_template
from rts_pp_api import RtsPpApiError, load_offers_cookie


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ktru-code", required=True)
    parser.add_argument("--cache-dir")
    parser.add_argument("--meta-dir", default=".")
    parser.add_argument("--cookie-file", default="/etc/rts-tender/cookies_rts.txt")
    parser.add_argument("--import-json", help="Import from Ktru/search JSON file instead of live search")
    args = parser.parse_args(argv)

    code = args.ktru_code.strip()
    cache = Path(args.cache_dir) if args.cache_dir else cache_dir_default(Path(args.meta_dir))

    try:
        if args.import_json:
            raw = json.loads(Path(args.import_json).read_text(encoding="utf-8"))
            item = import_ktru_template(cache, code, raw)
        else:
            cookie = load_offers_cookie(Path(args.cookie_file))
            item = get_ktru_template(cookie, code, cache, allow_stub=False)
    except RtsPpApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    chars = item.get("characteristics") or []
    print(f"OK {item.get('code')} v{item.get('version')} — {item.get('name')}")
    print(f"cache: {cache}")
    print(f"characteristics: {len(chars)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create test PP draft from cache and print GET summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rts_ktru_cache import cache_dir_default, load_cached_template
from notice_to_rts_pp import build_notice_create_payload, ensure_item_ktru_fields
from rts_pp_api import apply_draft_defaults, create_draft, ensure_delivery_stub, get_draft, load_offers_cookie, update_draft


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ktru-code", required=True)
    parser.add_argument("--meta-dir", type=Path, default=Path("/var/lib/rts_tender"))
    parser.add_argument("--cookie-file", type=Path, default=Path("/etc/rts-tender/cookies_rts.txt"))
    parser.add_argument("--apply-update", action="store_true")
    args = parser.parse_args(argv)

    cache = cache_dir_default(args.meta_dir)
    item = load_cached_template(cache, args.ktru_code)
    if not item:
        print(f"no cache for {args.ktru_code}", file=sys.stderr)
        return 1
    cookie = load_offers_cookie(args.cookie_file)
    payload, report = build_notice_create_payload(item, [])
    print("create_payload_chars", len(payload.get("ktruCharacteristics") or []))
    draft_id = create_draft(cookie, payload)
    got = get_draft(cookie, draft_id)
    print("after_create", json.dumps({
        "id": draft_id,
        "name": got.get("name"),
        "ktru": got.get("ktru"),
        "chars": len(got.get("ktruCharacteristics") or []),
    }, ensure_ascii=False))
    if args.apply_update:
        ensure_item_ktru_fields(got, item)
        ensure_delivery_stub(got)
        apply_draft_defaults(got)
        update_draft(cookie, got)
        got2 = get_draft(cookie, draft_id)
        print("after_update", json.dumps({
            "id": draft_id,
            "name": got2.get("name"),
            "ktru": got2.get("ktru"),
            "chars": len(got2.get("ktruCharacteristics") or []),
            "protectedId": got2.get("protectedId"),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

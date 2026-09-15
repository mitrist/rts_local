#!/usr/bin/env python3
"""Inspect RTS PP draft by id (GET + draft/search)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rts_pp_api import draft_search_by_id, get_draft, load_offers_cookie


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft_id", type=int)
    parser.add_argument("--cookie-file", type=Path, default=Path("/etc/rts-tender/cookies_rts.txt"))
    args = parser.parse_args(argv)

    cookie = load_offers_cookie(args.cookie_file)
    item = get_draft(cookie, args.draft_id)
    found = draft_search_by_id(cookie, args.draft_id)

    token_fields = {
        k: item.get(k)
        for k in ("id", "protectedId", "priceListItemId", "ktruCode", "status", "name")
        if item.get(k) is not None
    }
    summary = {
        "draft_id": args.draft_id,
        "top_level_keys": sorted(item.keys()),
        "token_fields": token_fields,
        "ktru": item.get("ktru"),
        "ktruCharacteristics_count": len(item.get("ktruCharacteristics") or []),
        "deliveryPlaces_count": len(item.get("deliveryPlaces") or []),
        "countriesOfOrigin": item.get("countriesOfOrigin"),
        "search_hit": found,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if item.get("ktruCharacteristics"):
        first = item["ktruCharacteristics"][0]
        print("first_char:", json.dumps({k: first.get(k) for k in ("code", "name", "isRequired", "characteristicValues")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

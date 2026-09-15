"""One-off smoke test: POST OfferPriceListItem/draft/create on RTS."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_PATH = (
    ROOT
    / "Схемы 16.1 итерация 3(1)"
    / "Схемы 16.1 итерация 3"
    / "ПП"
    / "requestpayload.json"
)
CREATE_URL = "https://offers.rts-tender.ru/api/v1/OfferPriceListItem/draft/create"
DRAFT_SEARCH_URL = (
    "https://platform.rts-tender.ru/api-gateway/preliminary-offers/"
    "supplier/offer-price-list-item-registry/draft/search"
)


def _headers(cookie: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": cookie.strip(),
        "Origin": "https://offers.rts-tender.ru",
        "Referer": "https://offers.rts-tender.ru/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "x-api-request": "True",
        "x-client-app": "Angular",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie-file", type=Path, help="File with Cookie header value")
    args = ap.parse_args()

    cookie = os.environ.get("RTS_OFFERS_COOKIE", "").strip()
    if args.cookie_file and args.cookie_file.is_file():
        cookie = args.cookie_file.read_text(encoding="utf-8").strip()
    if not cookie:
        print("Set RTS_OFFERS_COOKIE or pass --cookie-file.", file=sys.stderr)
        return 2

    payload = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    print(f"KTRU: {payload['ktru']['code']} — {payload['ktru']['name']}")
    print(f"POST {CREATE_URL}")

    r = requests.post(
        CREATE_URL,
        headers=_headers(cookie),
        json=payload,
        timeout=60,
    )
    print(f"create status={r.status_code}")
    print(f"create body={r.text[:2000]}")

    if r.status_code != 200:
        return 1

    data = r.json()
    draft_id = data.get("id")
    if not draft_id:
        print("No id in response.", file=sys.stderr)
        return 1

    print(f"draft id={draft_id}")

    search_body = {"ids": [draft_id], "page": 0, "size": 10}
    print(f"POST {DRAFT_SEARCH_URL}")
    r2 = requests.post(
        DRAFT_SEARCH_URL,
        headers=_headers(cookie),
        json=search_body,
        timeout=60,
    )
    print(f"search status={r2.status_code}")
    if r2.status_code == 200:
        found = r2.json().get("total", 0)
        print(f"search total={found}")
        if found:
            item = r2.json()["page"][0]
            print(f"  name={item.get('name')}")
            print(f"  protectedId={item.get('protectedId')}")
            print(f"  deliveryPlaces={len(item.get('deliveryPlaces') or [])}")
    else:
        print(f"search body={r2.text[:1500]}")

    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())

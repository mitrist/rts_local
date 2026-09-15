"""Smoke test: save draft + prepareforpublish + publish on RTS."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
PP_DIR = ROOT / "Схемы 16.1 итерация 3(1)" / "Схемы 16.1 итерация 3" / "ПП"
OFFERS = "https://offers.rts-tender.ru/api/v1/OfferPriceListItem"
PLATFORM_TSA = (
    "https://platform.rts-tender.ru/signature-time-stamp/api/v1/"
    "signature-time-stamps/append/signed-data"
)

DEFAULT_DELIVERY = {
    "kladr": None,
    "oktmo": {
        "code": "04500000",
        "name": "Муниципальные округа Красноярского края",
    },
    "unitPrice": "10500,0",
    "minQuantity": "1,0",
    "maxQuantity": "5,0",
    "minDeliveryDays": 20,
    "maxDeliveryDays": 40,
    "blocked": "0,0",
    "available": "5,0",
}


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


def _load_cookie(args: argparse.Namespace) -> str:
    cookie = os.environ.get("RTS_OFFERS_COOKIE", "").strip()
    if args.cookie_file and args.cookie_file.is_file():
        cookie = args.cookie_file.read_text(encoding="utf-8").strip()
    if not cookie and (PP_DIR / "publish.json").is_file():
        for line in (PP_DIR / "publish.json").read_text(encoding="utf-8").splitlines():
            if line.startswith("Cookie:"):
                cookie = line.split(":", 1)[1].strip()
                break
    return cookie


def _print_step(name: str, r: requests.Response) -> Any | None:
    print(f"\n=== {name} ===")
    print(f"status={r.status_code}")
    text = r.text or ""
    print(f"body={text[:2500]}")
    if r.status_code == 200 and "application/json" in (r.headers.get("content-type") or ""):
        try:
            return r.json()
        except json.JSONDecodeError:
            return None
    return None


def _fill_characteristics(item: dict[str, Any]) -> None:
    """Minimal required values from search.json reference draft."""
    fills = {
        "8a2ba52d-4": {
            "id": 5327335,
            "concreteValue": "2,0",
            "okeiCode": "796",
            "okeiFullName": "Штука",
            "valueFormatId": 1,
        },
        "4a3daf4b-d": {"id": 5327327, "qualityDescription": "Модульный"},
        "4da0e391-f": {"id": 5327330, "qualityDescription": "Металлический"},
        "572ee567-0": {"id": 5327331, "qualityDescription": "Нет"},
    }
    for ch in item.get("ktruCharacteristics") or []:
        code = ch.get("code") or ""
        if code not in fills:
            continue
        if ch.get("characteristicValues"):
            continue
        f = fills[code]
        ch["characteristicValues"] = [
            {
                "id": f["id"],
                "qualityDescription": f.get("qualityDescription"),
                "concreteValue": f.get("concreteValue"),
                "minValue": None,
                "maxValue": None,
                "characteristicId": ch.get("id"),
                "characteristicCode": code,
                "characteristicName": ch.get("name"),
                "okeiCode": f.get("okeiCode", ""),
                "okeiFullName": f.get("okeiFullName"),
                "valueFormatId": f.get("valueFormatId"),
            }
        ]


def _ensure_delivery(item: dict[str, Any]) -> None:
    places = item.get("deliveryPlaces") or []
    if places:
        dp = places[0]
        dp.setdefault("unitPrice", DEFAULT_DELIVERY["unitPrice"])
        dp.setdefault("oktmo", DEFAULT_DELIVERY["oktmo"])
        dp.setdefault("minDeliveryDays", DEFAULT_DELIVERY["minDeliveryDays"])
        dp.setdefault("maxDeliveryDays", DEFAULT_DELIVERY["maxDeliveryDays"])
        return
    item["deliveryPlaces"] = [dict(DEFAULT_DELIVERY)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft-id", type=int, default=495423)
    ap.add_argument("--cookie-file", type=Path)
    ap.add_argument("--skip-save", action="store_true")
    ap.add_argument(
        "--signatures-file",
        type=Path,
        help="JSON array for signature-time-stamp (from browser dump)",
    )
    args = ap.parse_args()

    cookie = _load_cookie(args)
    if not cookie:
        print("Set RTS_OFFERS_COOKIE or --cookie-file.", file=sys.stderr)
        return 2

    h = _headers(cookie)
    draft_id = args.draft_id
    print(f"draft id={draft_id}")

    r = requests.get(f"{OFFERS}/{draft_id}", headers=h, timeout=60)
    item = _print_step("GET draft", r)
    if r.status_code != 200 or not isinstance(item, dict):
        return 1

    if not args.skip_save:
        _fill_characteristics(item)
        _ensure_delivery(item)
        item.setdefault("countriesOfOrigin", [{"code": "643", "name": "Российская Федерация"}])
        item.setdefault(
            "nationalModeInfoGroups",
            [
                {
                    "countryCode": "643",
                    "nationalModeInfoTypeId": "IndustrialProductsRussianFederation",
                    "nationalModeInfos": [{"registryNumberNotProvided": True}],
                }
            ],
        )
        item.setdefault(
            "softwareNationalModeInfoGroups",
            [
                {
                    "countryCode": "643",
                    "nationalModeInfoTypeId": "SoftwareRussianFederation",
                    "nationalModeInfos": [{"registryNumberNotProvided": True}],
                }
            ],
        )
        r_save = requests.post(f"{OFFERS}/draft/update", headers=h, json=item, timeout=60)
        _print_step("POST draft/update", r_save)
        if r_save.status_code != 200:
            print("Could not save draft.", file=sys.stderr)
            return 1
        r_reload = requests.get(f"{OFFERS}/{draft_id}", headers=h, timeout=60)
        reloaded = _print_step("GET draft (after save)", r_reload)
        if isinstance(reloaded, dict):
            item = reloaded

    prep_body = {"offerPriceListItemId": draft_id, "fileHashes": "[]"}
    r_prep = requests.post(f"{OFFERS}/prepareforpublish", headers=h, json=prep_body, timeout=90)
    prep_result = _print_step("prepareforpublish", r_prep)
    if r_prep.status_code != 200 or not isinstance(prep_result, dict):
        return 1

    string_for_sign = prep_result.get("stringForSign") or prep_result.get("StringForSign")
    if string_for_sign:
        try:
            xml = base64.b64decode(string_for_sign).decode("utf-8")
            print(f"\nstringForSign XML snippet: {xml[:400]}...")
        except Exception as exc:
            print(f"\nstringForSign decode error: {exc}")

    sig_payload = None
    if args.signatures_file and args.signatures_file.is_file():
        sig_payload = json.loads(args.signatures_file.read_text(encoding="utf-8"))
    elif (PP_DIR / "prepare_to_publish.json").is_file():
        raw = (PP_DIR / "prepare_to_publish.json").read_text(encoding="utf-8")
        start = raw.find("[")
        if start >= 0:
            end = raw.find("]", start) + 1
            try:
                sig_payload = json.loads(raw[start:end])
            except json.JSONDecodeError:
                sig_payload = None

    if sig_payload:
        r_tsa = requests.post(PLATFORM_TSA, headers=h, json=sig_payload, timeout=90)
        _print_step("signature-time-stamp", r_tsa)
    else:
        print("\n=== signature-time-stamp ===")
        print("skipped (no signatures file)")

    publish_body: dict[str, Any] = dict(prep_result)
    if sig_payload:
        publish_body["signatures"] = sig_payload

    r_pub = requests.post(f"{OFFERS}/publish", headers=h, json=publish_body, timeout=90)
    _print_step("publish", r_pub)
    if r_pub.status_code == 200:
        print("\nPublish OK.")
        return 0

    print(
        "\nPublish blocked: need ЭП (КриптоПро) signatures for stringForSign "
        "from prepareforpublish, then signature-time-stamp, then publish.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

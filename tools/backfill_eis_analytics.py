#!/usr/bin/env python3
"""Seed SQLite EIS analytics from current JSON sidecars.

This is best-effort: historical data is limited by the short JSON retention
window and by what is present in last_eis_poll.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eis_analytics_db import DEFAULT_DB_NAME, analytics_db_path, record_eis_poll_run
from eis_ezt_archive import NoticeMatch
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _dt_from_iso(raw: object) -> datetime:
    s = str(raw or "").strip()
    if not s:
        return datetime.now(timezone.utc).replace(microsecond=0)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _line_from_dict(item: dict[str, Any]) -> KtruPurchaseLine:
    chars = []
    for ch in item.get("characteristics") if isinstance(item.get("characteristics"), list) else []:
        if not isinstance(ch, dict):
            continue
        chars.append(
            CustomerCharacteristic(
                match_key=str(ch.get("match_key") or ""),
                characteristic_kind=str(ch.get("characteristic_kind") or ""),
                characteristic_code=(
                    str(ch.get("characteristic_code")).strip()
                    if ch.get("characteristic_code") is not None
                    else None
                ),
                display_label=str(ch.get("display_label") or ""),
                value_display=str(ch.get("value_display") or ""),
            )
        )
    return KtruPurchaseLine(
        line_key=str(item.get("line_key") or ""),
        purchase_number=str(item.get("purchase_number") or ""),
        object_sid=str(item.get("object_sid") or ""),
        object_external_sid=str(item.get("object_external_sid") or ""),
        position_index=int(item.get("position_index") or 0),
        ktru_code=str(item.get("ktru_code") or ""),
        position_display_name=str(item.get("position_display_name") or ""),
        price_per_unit_display=str(item.get("price_per_unit_display") or ""),
        line_sum_display=str(item.get("line_sum_display") or ""),
        notice_total_display=str(item.get("notice_total_display") or ""),
        customer_organization_display=str(item.get("customer_organization_display") or ""),
        characteristics=chars,
    )


def build_matches(last_eis: dict[str, Any], changelog: dict[str, Any]) -> list[NoticeMatch]:
    by_purchase: dict[str, NoticeMatch] = {}
    match_meta: dict[str, dict[str, Any]] = {}
    for raw in last_eis.get("matches") if isinstance(last_eis.get("matches"), list) else []:
        if not isinstance(raw, dict):
            continue
        pn = str(raw.get("purchase_number") or "").strip()
        if pn:
            match_meta[pn] = raw

    for raw in last_eis.get("customer_characteristics") if isinstance(last_eis.get("customer_characteristics"), list) else []:
        if not isinstance(raw, dict):
            continue
        line = _line_from_dict(raw)
        if not line.line_key:
            continue
        pn = (line.purchase_number or "").strip()
        if not pn:
            continue
        meta = match_meta.get(pn, {})
        if pn not in by_purchase:
            by_purchase[pn] = NoticeMatch(
                purchase_number=pn,
                ktru_codes=[],
                purchase_method_codes=[
                    str(x) for x in meta.get("purchase_method_codes", []) if str(x or "").strip()
                ],
                source_file=str(meta.get("source_file") or ""),
                publish_dt_in_eis=str(meta.get("publish_dt_in_eis") or ""),
                publish_dt_in_etp=str(meta.get("publish_dt_in_etp") or ""),
                customer_ktru_lines=[],
            )
        by_purchase[pn].customer_ktru_lines.append(line)
        if line.ktru_code and line.ktru_code not in by_purchase[pn].ktru_codes:
            by_purchase[pn].ktru_codes.append(line.ktru_code)

    for raw in changelog.get("entries") if isinstance(changelog.get("entries"), list) else []:
        if not isinstance(raw, dict):
            continue
        pn = str(raw.get("purchase_number") or "").strip()
        ktru = str(raw.get("ktru") or "").strip()
        if not pn or not ktru or pn in by_purchase:
            continue
        pub_raw = str(raw.get("publish_raw") or "")
        src = str(raw.get("publish_source") or "")
        by_purchase[pn] = NoticeMatch(
            purchase_number=pn,
            ktru_codes=[ktru],
            publish_dt_in_eis=pub_raw if src == "ЕИС" else "",
            publish_dt_in_etp=pub_raw if src == "ЭТП" else "",
        )

    return list(by_purchase.values())


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill EIS analytics SQLite from JSON sidecars.")
    p.add_argument("--meta-dir", type=Path, default=Path("."))
    p.add_argument("--db", default=DEFAULT_DB_NAME)
    p.add_argument("--last-eis", default="last_eis_poll.json")
    p.add_argument("--changelog", default="eis_ktru_changelog.json")
    args = p.parse_args()

    meta = args.meta_dir.resolve()
    last_eis = _read_json(meta / args.last_eis)
    changelog = _read_json(meta / args.changelog)
    matches = build_matches(last_eis, changelog)
    finished = _dt_from_iso(last_eis.get("at"))
    db_path = analytics_db_path(meta, args.db)
    meta_block = last_eis.get("meta") if isinstance(last_eis.get("meta"), dict) else {}
    run_id = record_eis_poll_run(
        db_path,
        matches,
        finished_at_utc=finished,
        org_region=str(meta_block.get("org_region") or ""),
        document_type_44=str(meta_block.get("document_type_44") or "epNotificationEZT2020"),
        ktru_filter=None,
        exact_dates=[str(x) for x in meta_block.get("exact_dates", []) if str(x or "").strip()],
        zips_downloaded=int(meta_block.get("zips_downloaded") or 0),
        ok=not bool(last_eis.get("errors")),
        errors=[str(x) for x in last_eis.get("errors", []) if str(x or "").strip()]
        if isinstance(last_eis.get("errors"), list)
        else [],
    )
    print(f"Backfilled {len(matches)} purchase events into {db_path} (run_id={run_id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

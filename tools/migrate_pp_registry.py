#!/usr/bin/env python3
"""Export/import PP monitor registry + settings (VM -> VPS migration)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REGISTRY_COLS = (
    "offer_price_list_item_id",
    "ktru_code",
    "price_list_item_id",
    "name",
    "trade_mark",
    "link_date",
    "validity_date",
    "unit_price",
    "delivery_regions_count",
    "card_url",
    "first_seen_at_utc",
    "last_seen_at_utc",
)


def export_registry(db_path: Path, out_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(f"SELECT {', '.join(REGISTRY_COLS)} FROM rts_pp_monitor_registry")]
    conn.close()
    out_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return len(rows)


def import_registry(db_path: Path, in_path: Path) -> int:
    rows = json.loads(in_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit("registry JSON must be a list")
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM rts_pp_monitor_registry")
    placeholders = ", ".join("?" for _ in REGISTRY_COLS)
    sql = (
        f"INSERT INTO rts_pp_monitor_registry ({', '.join(REGISTRY_COLS)}) "
        f"VALUES ({placeholders})"
    )
    for row in rows:
        conn.execute(sql, tuple(row.get(c) for c in REGISTRY_COLS))
    conn.commit()
    conn.close()
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export")
    ex.add_argument("--db", type=Path, required=True)
    ex.add_argument("--out", type=Path, required=True)

    im = sub.add_parser("import")
    im.add_argument("--db", type=Path, required=True)
    im.add_argument("--in", dest="in_path", type=Path, required=True)

    args = p.parse_args()
    if args.cmd == "export":
        n = export_registry(args.db, args.out)
        print(f"exported {n} registry rows -> {args.out}")
    else:
        n = import_registry(args.db, args.in_path)
        print(f"imported {n} registry rows into {args.db}")


if __name__ == "__main__":
    main()

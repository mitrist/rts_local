"""Register a manually imported EIS↔PP ground-truth case (analyst_status stays pending)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eis_pp_manual_register import register_manual_eis_pp_pair


def main() -> None:
    parser = argparse.ArgumentParser(description="Register manual EIS↔PP ground-truth case")
    parser.add_argument("--db", default="/var/lib/rts_tender/eis_analytics.sqlite3")
    parser.add_argument("--meta-dir", default="/var/lib/rts_tender")
    parser.add_argument("--find", type=Path, default=None, help="Local find JSON (optional if --offer-id)")
    parser.add_argument("--offer-id", default="", help="Fetch find from RTS by offer id")
    parser.add_argument("--purchase-number", required=True)
    parser.add_argument("--line-key", default="")
    parser.add_argument("--link-date-msk", default="", help="DD.MM.YYYY HH:MM[:SS] MSK")
    parser.add_argument("--oktmo", default="", help="Expected OKTMO code for validation")
    parser.add_argument("--note", default="")
    parser.add_argument("--source", default="manual_ground_truth")
    args = parser.parse_args()

    find_item = None
    if args.find:
        data = json.loads(Path(args.find).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SystemExit("find JSON must be an object")
        find_item = data
    offer_id = str(args.offer_id or "").strip()
    if find_item is None and not offer_id:
        raise SystemExit("укажите --find или --offer-id")

    out = register_manual_eis_pp_pair(
        db_path=Path(args.db),
        meta_dir=Path(args.meta_dir),
        purchase_number=str(args.purchase_number).strip(),
        offer_price_list_item_id=offer_id or str(find_item.get("offerPriceListItemId") or "").strip(),
        line_key=str(args.line_key or "").strip(),
        find_item=find_item,
        link_date_msk=str(args.link_date_msk or "").strip(),
        expected_oktmo=str(args.oktmo or "").strip(),
        analyst_note=str(args.note or "").strip(),
        source=str(args.source).strip(),
    )
    print(json.dumps(out.as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

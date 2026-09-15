"""Import catalog positions from ктру.csv into rts_preoffers.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rts_preoffers import import_preoffers_from_ktru_csv


def main() -> int:
    ap = argparse.ArgumentParser(description="Import ктру.csv into rts_preoffers.json")
    ap.add_argument(
        "--csv",
        type=Path,
        default=ROOT / "ктру.csv",
        help="Path to semicolon-separated CSV",
    )
    ap.add_argument(
        "--catalog",
        type=Path,
        default=ROOT / "rts_preoffers.json",
        help="Target preoffers JSON",
    )
    ap.add_argument(
        "--replace-existing-ktru",
        action="store_true",
        help="Add even when KTRU already exists in catalog",
    )
    args = ap.parse_args()
    try:
        stats = import_preoffers_from_ktru_csv(
            args.csv,
            catalog_path=args.catalog,
            skip_existing_ktru=not args.replace_existing_ktru,
        )
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"CSV rows: {stats['total_in_csv']}; "
        f"added: {stats['added']}; "
        f"skipped existing KTRU: {stats['skipped_existing']}; "
        f"skipped duplicate CSV rows: {stats['skipped_duplicate_row']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

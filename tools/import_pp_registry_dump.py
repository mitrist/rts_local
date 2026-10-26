#!/usr/bin/env python3
"""Import concatenated RTS customer-registry JSON into findings (prices/regions)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rts_pp_monitor import import_registry_find_dump  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--ktru", default="")
    parser.add_argument("--source", default="dump")
    args = parser.parse_args()
    text = args.file.read_text(encoding="utf-8")
    result = import_registry_find_dump(args.db, text, ktru_code=args.ktru, source=args.source)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

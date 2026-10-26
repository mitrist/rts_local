#!/usr/bin/env python3
"""Build demand-core snapshot from an RTS NMCK justification contract dump."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pp_core_catalog import CoreSnapshotError  # noqa: E402
from pp_core_nmck import NmckImportError, STUDENT_CHAIR_KTRU_CODE, import_nmck_core  # noqa: E402
from rts_ktru_cache import cache_dir_default, load_cached_template  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--ktru", default=STUDENT_CHAIR_KTRU_CODE)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--template", type=Path, default=None)
    args = parser.parse_args()
    text = args.file.read_text(encoding="utf-8")
    template = None
    if args.template and args.template.is_file():
        raw = json.loads(args.template.read_text(encoding="utf-8"))
        template = raw.get("item") if isinstance(raw.get("item"), dict) else raw
    cache = args.cache_dir or cache_dir_default(Path("."))
    if template is None:
        template = load_cached_template(cache, args.ktru, ttl_sec=0)
    try:
        result = import_nmck_core(
            args.db,
            text,
            ktru_code=args.ktru,
            template=template,
            cache_dir=cache,
        )
    except (NmckImportError, CoreSnapshotError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "stats": result.get("stats"), "meta": result.get("meta")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

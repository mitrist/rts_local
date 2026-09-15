#!/usr/bin/env python3
"""Import KTRU/draft_ktru_dict.txt into ktru_rts_cache (PP template dictionary).

Usage (on VM or locally):
  python tools/import_draft_ktru_dict.py
  python tools/import_draft_ktru_dict.py --source KTRU/draft_ktru_dict.txt \\
      --cache-dir /var/lib/rts_local/ktru_rts_cache
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from import_ktru_bulk import main as bulk_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=_ROOT / "KTRU" / "draft_ktru_dict.txt",
        help="Path to draft_ktru_dict.txt (concatenated Ktru/search JSON)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Output ktru_rts_cache (default: RTS_META_DIR/ktru_rts_cache or ./ktru_rts_cache)",
    )
    parser.add_argument("--report-json", type=Path, default=None)
    args = parser.parse_args(argv)

    source = args.source
    if not source.is_file():
        print(f"ERROR: source not found: {source}", file=sys.stderr)
        print(
            "Place draft_ktru_dict.txt under KTRU/ or pass --source. "
            "On co-hosted VM you can instead run: bash deploy/sync_ktru_cache_from_prod.sh",
            file=sys.stderr,
        )
        return 1

    cache = args.cache_dir
    if cache is None:
        meta = Path(os.environ.get("RTS_META_DIR", str(_ROOT))).resolve()
        cache = meta / "ktru_rts_cache"

    bulk_argv = [str(source), "--cache-dir", str(cache)]
    if args.report_json is not None:
        bulk_argv.extend(["--report-json", str(args.report_json)])
    return bulk_main(bulk_argv)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Import concatenated Ktru/search JSON blocks into ktru_rts_cache."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rts_ktru_cache import cache_dir_default, import_ktru_search_response

_KTRU_CODE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{5,8}$")


def _iter_json_objects(text: str) -> list[dict]:
    """Parse consecutive JSON objects (e.g. Ktru/search responses glued together)."""
    decoder = json.JSONDecoder()
    raw = text.strip()
    if not raw:
        return []
    out: list[dict] = []
    idx = 0
    n = len(raw)
    while idx < n:
        while idx < n and raw[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, end = decoder.raw_decode(raw, idx)
        if not isinstance(obj, dict):
            raise ValueError(f"block {len(out) + 1}: expected JSON object")
        out.append(obj)
        idx = end
    return out


def _load_catalog_codes(path: Path) -> set[str]:
    codes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        code = line.split(";", 1)[0].strip()
        if code:
            codes.add(code)
    return codes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="File with Ktru/search JSON blocks separated by blank lines")
    parser.add_argument("--cache-dir", type=Path, help="Output ktru_rts_cache directory")
    parser.add_argument("--meta-dir", type=Path, default=_ROOT, help="Used if --cache-dir omitted")
    parser.add_argument("--catalog-csv", type=Path, help="Optional ktru.csv for coverage report")
    parser.add_argument("--report-json", type=Path, help="Write import report JSON")
    args = parser.parse_args(argv)

    cache = args.cache_dir if args.cache_dir else cache_dir_default(args.meta_dir)
    try:
        blocks = _iter_json_objects(args.source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    ok = 0
    errors: list[dict[str, str]] = []
    imported: dict[str, int] = {}
    duplicates: list[str] = []

    for idx, data in enumerate(blocks, start=1):
        try:
            items = import_ktru_search_response(cache, data)
        except (OSError, ValueError) as exc:
            errors.append({"block": str(idx), "error": str(exc)})
            continue
        for item in items:
            code = str(item.get("code") or "").strip()
            if not code or not _KTRU_CODE_RE.match(code):
                errors.append({"block": str(idx), "error": f"invalid ktru code: {code!r}"})
                continue
            if code in imported:
                duplicates.append(code)
            imported[code] = len(item.get("characteristics") or [])
            ok += 1

    report: dict[str, object] = {
        "source": str(args.source),
        "cache_dir": str(cache),
        "blocks_total": len(blocks),
        "imported_ok": ok,
        "unique_codes": len(imported),
        "duplicates": sorted(set(duplicates)),
        "errors": errors,
        "empty_characteristics": sorted(c for c, n in imported.items() if n == 0),
    }

    if args.catalog_csv and args.catalog_csv.is_file():
        catalog = _load_catalog_codes(args.catalog_csv)
        have = set(imported)
        report["catalog_codes"] = len(catalog)
        report["missing_in_cache"] = sorted(catalog - have)
        report["extra_in_cache"] = sorted(have - catalog)

    print(f"blocks: {len(blocks)}")
    print(f"imported: {ok} unique={len(imported)} errors={len(errors)}")
    if duplicates:
        print(f"duplicates overwritten: {len(set(duplicates))}")
    if report.get("missing_in_cache"):
        print(f"missing vs catalog: {len(report['missing_in_cache'])}")
    print(f"cache: {cache}")

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report: {args.report_json}")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

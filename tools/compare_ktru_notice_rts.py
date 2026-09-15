#!/usr/bin/env python3
"""Compare EIS notice characteristics with RTS KTRU template."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eis_analytics_db import analytics_db_path, enrich_notice_characteristics, notice_line_for_pp
from notice_to_rts_pp import compare_notice_to_template
from rts_ktru_cache import cache_dir_default, get_ktru_template, import_ktru_template, load_cached_template
from rts_pp_api import load_offers_cookie, RtsPpApiError


def _load_notice_chars(args: argparse.Namespace) -> tuple[list[dict], str | None]:
    if args.notice_json:
        data = json.loads(Path(args.notice_json).read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data, None
        if isinstance(data, dict) and isinstance(data.get("characteristics"), list):
            return data["characteristics"], None
        raise SystemExit("notice JSON must be a list or {characteristics: [...]}")

    if not args.purchase_number or not args.line_key:
        raise SystemExit("Укажите --notice-json или пару --purchase-number + --line-key")

    db = analytics_db_path(Path(args.meta_dir))
    line = notice_line_for_pp(db, args.purchase_number, args.line_key)
    if not line:
        raise SystemExit("Позиция не найдена в SQLite")
    chars = enrich_notice_characteristics(
        db,
        purchase_number=args.purchase_number,
        ktru_code=str(line.get("ktru_code") or args.ktru_code or ""),
        html_chars=line.get("characteristics") or [],
    )
    return chars, str(line.get("ktru_code") or "")


def _load_rts_template(args: argparse.Namespace, ktru_code: str) -> dict:
    cache = Path(args.cache_dir) if args.cache_dir else cache_dir_default(Path(args.meta_dir))
    if args.import_rts_json:
        raw = json.loads(Path(args.import_rts_json).read_text(encoding="utf-8"))
        return import_ktru_template(cache, ktru_code, raw)

    if args.rts_json:
        raw = json.loads(Path(args.rts_json).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("code"):
            return raw
        page = raw.get("page") if isinstance(raw, dict) else None
        if isinstance(page, list):
            for row in page:
                if isinstance(row, dict) and str(row.get("code") or "").strip() == ktru_code:
                    return row
        raise SystemExit(f"В --rts-json нет шаблона {ktru_code}")

    cached = load_cached_template(cache, ktru_code)
    if cached:
        return cached

    if args.cookie_file:
        cookie = load_offers_cookie(Path(args.cookie_file))
        return get_ktru_template(cookie, ktru_code, cache, allow_stub=False)

    raise SystemExit("Нет шаблона: укажите --rts-json, --cache-dir с кэшем или --cookie-file")


def _print_table(rows: list[dict]) -> None:
    headers = ("notice", "notice_val", "rts_code", "rts_name", "char", "value")
    print("\t".join(headers))
    for row in rows:
        print(
            "\t".join(
                [
                    str(row.get("notice_label") or ""),
                    str(row.get("notice_value") or ""),
                    str(row.get("rts_code") or ""),
                    str(row.get("rts_name") or ""),
                    str(row.get("char_match") or ""),
                    str(row.get("value_match") or ""),
                ]
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ktru-code", help="Код КТРУ")
    parser.add_argument("--notice-json", help="JSON характеристик извещения")
    parser.add_argument("--purchase-number")
    parser.add_argument("--line-key")
    parser.add_argument("--meta-dir", default=".", help="RTS_META_DIR для SQLite и кэша")
    parser.add_argument("--cache-dir", help="Каталог ktru_rts_cache")
    parser.add_argument("--rts-json", help="Файл ответа Ktru/search или один item")
    parser.add_argument("--import-rts-json", help="Импортировать JSON в кэш и сравнить")
    parser.add_argument("--cookie-file", help="Cookie для live Ktru/search")
    parser.add_argument("--json-out", action="store_true", help="Вывод в JSON")
    args = parser.parse_args(argv)

    notice_chars, ktru_from_line = _load_notice_chars(args)
    ktru_code = str(args.ktru_code or ktru_from_line or "").strip()
    if not ktru_code:
        raise SystemExit("Код КТРУ не задан")

    try:
        ktru_item = _load_rts_template(args, ktru_code)
    except RtsPpApiError as exc:
        raise SystemExit(str(exc)) from exc

    rows = compare_notice_to_template(notice_chars, ktru_item)
    if args.json_out:
        print(json.dumps({"ktru_code": ktru_code, "rows": rows}, ensure_ascii=False, indent=2))
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

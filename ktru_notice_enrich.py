"""Обогащение извещений ЕИС: ОКТМО, quantity, ИНН, срок поставки.

Работает только с полями, уже лежащими в eis_history (XML-теги quantity /
deliveryTerm / ОКТМО места в таблицу не пишутся). Не заполняет пропуски догадками.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eis_history_db import history_connect  # noqa: E402
from eis_oktmo_subjects import subject_oktmo_for_org_region  # noqa: E402
from participation_price import parse_money_amount  # noqa: E402
from rts_pp_api import parse_oktmo_from_delivery_place  # noqa: E402

KTRU_DEFAULT = "31.01.11.150-00000003"
INN_LABELED_RE = re.compile(r"ИНН\s+(\d{10})(?:\D|$)")
INN_BARE_RE = re.compile(r"\b(\d{10})\b")
DAYS_IN_TERM_RE = re.compile(
    r"в\s+течение\s+(\d+)\s+(?:календарн\w+\s+)?(?:рабоч\w+\s+)?дн",
    re.IGNORECASE,
)
DAYS_CAL_RE = re.compile(r"(\d+)\s+календарн\w+\s+дн", re.IGNORECASE)
DATE_UNTIL_RE = re.compile(r"до\s+(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)
OKTMO_CODE_RE = re.compile(r"^\d{8}(\d{3})?$")
QTY_LABEL_RE = re.compile(r"количеств", re.IGNORECASE)


def inn10_checksum_ok(inn: str) -> bool:
    if not re.fullmatch(r"\d{10}", inn):
        return False
    coefs = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    total = sum(int(inn[i]) * coefs[i] for i in range(9))
    return (total % 11) % 10 == int(inn[9])


def extract_inn10(customer_text: str) -> tuple[str | None, bool | None, str]:
    text = str(customer_text or "")
    labeled = INN_LABELED_RE.search(text)
    if labeled:
        inn = labeled.group(1)
        if inn10_checksum_ok(inn):
            return inn, True, "customer_text:ИНН"
        return None, False, "inn_checksum_failed"
    for match in INN_BARE_RE.finditer(text):
        inn = match.group(1)
        span_start = match.start()
        prefix = text[max(0, span_start - 12) : span_start].upper()
        if "СПЗ" in prefix:
            continue
        if inn10_checksum_ok(inn):
            return inn, True, "customer_text:\\b\\d{10}\\b"
        continue
    if re.search(r"ИНН\s+\d{12}\b", text):
        return None, None, "no_inn_in_source"
    if "ИНН" in text.upper() and re.search(r"ИНН\s+\d+", text):
        return None, False, "inn_checksum_failed"
    return None, None, "no_inn_in_source"


def extract_quantity(
    *,
    price_per_unit_text: str,
    line_sum_text: str,
    characteristics_json: str,
    notice_total_text: str,
) -> tuple[int | None, str, bool | None, str]:
    del notice_total_text  # НМЦК, не объём; не используем как quantity.
    unit = parse_money_amount(price_per_unit_text)
    line_sum = parse_money_amount(line_sum_text)
    if unit is not None and unit > 0 and line_sum is not None:
        raw = line_sum / unit
        qty = int(round(raw))
        if qty <= 0:
            return None, "calc", None, "no_source"
        suspect = abs(raw - qty) / raw > 0.05
        return qty, "calc", suspect, "line_sum/price"
    try:
        chars = json.loads(characteristics_json or "[]")
    except json.JSONDecodeError:
        chars = []
    if isinstance(chars, list):
        for ch in chars:
            if not isinstance(ch, dict):
                continue
            lab = str(ch.get("display_label") or "")
            val = str(ch.get("value_display") or "")
            if not QTY_LABEL_RE.search(lab):
                continue
            digits = re.search(r"(\d+(?:[.,]\d+)?)", val.replace(" ", ""))
            if not digits:
                continue
            try:
                raw = float(digits.group(1).replace(",", "."))
            except ValueError:
                continue
            qty = int(round(raw))
            if qty <= 0:
                continue
            return qty, "text", False, "characteristics_json:количество"
    return None, "null", None, "no_source"


def extract_delivery(customer_text: str) -> tuple[str | None, int | None, str, str]:
    text = str(customer_text or "")
    date_m = DATE_UNTIL_RE.search(text)
    if date_m:
        raw = date_m.group(1)
        try:
            iso = datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
            return iso, None, "date_pattern", "customer_text:до DD.MM.YYYY"
        except ValueError:
            pass
    in_term = DAYS_IN_TERM_RE.search(text)
    if in_term:
        days = int(in_term.group(1))
        if days > 0:
            return None, days, "days_pattern", "customer_text:в течение N дней"
    cal = DAYS_CAL_RE.search(text)
    if cal:
        days = int(cal.group(1))
        if days > 0:
            return None, days, "days_pattern", "customer_text:N календарных дней"
    return None, None, "null", "no_source"


def extract_oktmo(customer_text: str, org_region: str) -> tuple[str | None, str | None, str]:
    parsed = parse_oktmo_from_delivery_place(customer_text or "")
    if parsed and parsed.get("code"):
        code = re.sub(r"\D", "", str(parsed["code"]))
        if OKTMO_CODE_RE.fullmatch(code):
            return code, "place", "customer_text:oktmo"
    subject = subject_oktmo_for_org_region(org_region)
    if subject:
        return subject, "region", "notice_events.org_region"
    if str(org_region or "").strip():
        return None, None, "no_official_oktmo_for_region"
    return None, None, "no_source"


def enrich_row(row: dict[str, Any]) -> dict[str, Any]:
    inn, inn_valid, inn_reason = extract_inn10(str(row.get("customer_text") or ""))
    qty, qty_src, qty_suspect, qty_reason = extract_quantity(
        price_per_unit_text=str(row.get("price_per_unit_text") or ""),
        line_sum_text=str(row.get("line_sum_text") or ""),
        characteristics_json=str(row.get("characteristics_json") or "[]"),
        notice_total_text=str(row.get("notice_total_text") or ""),
    )
    delivery_date, delivery_days, delivery_src, delivery_reason = extract_delivery(
        str(row.get("customer_text") or "")
    )
    oktmo, oktmo_precision, oktmo_reason = extract_oktmo(
        str(row.get("customer_text") or ""),
        str(row.get("org_region") or ""),
    )
    return {
        "notice_id": str(row.get("purchase_number") or "").strip(),
        "line_key": str(row.get("line_key") or "").strip(),
        "oktmo": oktmo,
        "oktmo_precision": oktmo_precision,
        "oktmo_source": oktmo_reason,
        "quantity": qty,
        "quantity_source": qty_src if qty is not None else "null",
        "quantity_suspect": qty_suspect,
        "quantity_reason": qty_reason,
        "inn": inn,
        "inn_valid": inn_valid,
        "inn_source": inn_reason,
        "delivery_date": delivery_date,
        "delivery_days": delivery_days,
        "delivery_source": delivery_src if (delivery_date or delivery_days) else "null",
        "delivery_reason": delivery_reason,
    }


def load_ktru_rows(db: Path, ktru: str) -> list[dict[str, Any]]:
    with history_connect(db) as conn:
        rows = conn.execute(
            """
            SELECT
              l.line_key,
              l.purchase_number,
              l.price_per_unit_text,
              l.line_sum_text,
              l.notice_total_text,
              l.customer_text,
              l.characteristics_json,
              n.org_region
            FROM ktru_lines l
            JOIN notice_events n ON n.purchase_key = l.purchase_key
            WHERE l.ktru_code = ?
            ORDER BY l.purchase_number, l.line_key
            """,
            (ktru,),
        ).fetchall()
    return [dict(r) for r in rows]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1
    total = len(rows)

    def pct(filled: int) -> float:
        return round(100.0 * filled / n, 2) if total else 0.0

    oktmo_src = Counter(r["oktmo_precision"] or "null" for r in rows)
    qty_src = Counter(r["quantity_source"] for r in rows)
    inn_filled = sum(1 for r in rows if r["inn"])
    inn_invalid = sum(1 for r in rows if r["inn_valid"] is False)
    deliv_src = Counter(r["delivery_source"] for r in rows)
    return {
        "total": total,
        "oktmo_filled": sum(1 for r in rows if r["oktmo"]),
        "oktmo_pct": pct(sum(1 for r in rows if r["oktmo"])),
        "oktmo_by_precision": dict(oktmo_src),
        "quantity_filled": sum(1 for r in rows if r["quantity"] is not None),
        "quantity_pct": pct(sum(1 for r in rows if r["quantity"] is not None)),
        "quantity_by_source": dict(qty_src),
        "quantity_suspect": sum(1 for r in rows if r["quantity_suspect"] is True),
        "inn_filled": inn_filled,
        "inn_pct": pct(inn_filled),
        "inn_checksum_failed": inn_invalid,
        "inn_by_reason": dict(Counter(r["inn_source"] for r in rows)),
        "delivery_filled": sum(1 for r in rows if r["delivery_date"] or r["delivery_days"] is not None),
        "delivery_pct": pct(sum(1 for r in rows if r["delivery_date"] or r["delivery_days"] is not None)),
        "delivery_by_source": dict(deliv_src),
        "gaps": {
            "xml_quantity": "нет колонки/тега quantity в eis_history",
            "xml_deliveryTerm": "нет колонки deliveryTerm в eis_history",
            "xml_oktmo_place": "в XML места поставки пишется КЛАДР; код ОКТМО места в БД не хранится",
            "notice_total_not_quantity": "notice_total_text = НМЦК, не используется как quantity",
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--ktru", default=KTRU_DEFAULT)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()
    raw = load_ktru_rows(args.db, args.ktru)
    enriched = [enrich_row(r) for r in raw]
    payload = {
        "ktru": args.ktru,
        "summary": summarize(enriched),
        "rows": enriched if not args.limit else enriched[: args.limit],
        "rows_truncated": bool(args.limit) and len(enriched) > args.limit,
        "rows_total": len(enriched),
    }
    if args.out:
        args.out.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        print(json.dumps({"ok": True, "out": str(args.out), "summary": payload["summary"]}, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

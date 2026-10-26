"""Таблица контрактов Мастер-Зак из сохранённого ответа страницы."""

from __future__ import annotations

import json
from pathlib import Path

from datetime import datetime

from tools.fetch_masterzak_contracts import (
    MSK,
    expected_last_page,
    format_api_datetime,
    header_from_dump,
    rows_from_response,
    split_sign_range,
    write_csv,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "deploy" / "contract.txt"


def test_sample_page_flattens_to_fifty_rows():
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    rows = rows_from_response(payload, page=26)
    assert len(rows) == 50
    assert payload["total"] == 10000
    first = rows[0]
    assert first["page"] == "26"
    assert first["number"] == "1391400875226000138"
    assert first["unit_price"] == "124300"
    assert first["ktru"] == "58.29.11.000-00000003"
    assert first["supplier_name"] == 'ООО "АМБИМЕД"'
    assert "Вид лицензии:" in first["characteristics"]
    assert rows[1]["unit_price"] == "50281,67"
    assert rows[1]["quantity"] == "25"


def test_page_count_for_ten_thousand():
    # 50 + 49 новых на каждой следующей странице: последняя страница 205.
    assert expected_last_page(10000, 50, 1) == 205


def test_sign_range_splits_on_moscow_midnight():
    start = datetime(2025, 12, 1, tzinfo=MSK)
    end = datetime(2026, 1, 1, tzinfo=MSK)
    mid = split_sign_range(start, end)
    assert mid == datetime(2025, 12, 16, tzinfo=MSK)
    assert format_api_datetime(start) == "2025-11-30T21:00:00Z"


def test_devtools_header_dump_reads_cookie_line():
    dump = "authority\nmaster-zak.ru\ncookie\nUser=abc; useSso=True\nreferer\nhttps://master-zak.ru/\n"
    assert header_from_dump(dump, "cookie") == "User=abc; useSso=True"
    assert header_from_dump(dump, "authorization") == ""


def test_csv_header_is_excel_friendly(tmp_path: Path):
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    rows = rows_from_response(payload, page=1)
    out = tmp_path / "contracts.csv"
    write_csv(rows[:1], out)
    text = out.read_text(encoding="utf-8-sig")
    assert text.startswith("Страница;Номер контракта;")
    assert "1391400875226000138" in text

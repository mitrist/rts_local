from __future__ import annotations

from pathlib import Path

from eis_supplier_results_parse import parse_supplier_results_html

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "supplier_results_sample.html"


def test_parse_supplier_results_winner():
    html = FIXTURE.read_text(encoding="utf-8")
    winner = parse_supplier_results_html(html)
    assert winner is not None
    assert winner.participant_id == "121373449"
    assert winner.offer_total == 445_000.0
    assert "победитель" in winner.rank_label.lower()
    assert winner.formed_at_utc is not None


def test_parse_supplier_results_empty():
    assert parse_supplier_results_html("") is None

"""Сравнение характеристик заказчик / поставщик (stub)."""

from __future__ import annotations

from eis_supplier_characteristics import (
    SupplierCharacteristic,
    NullSupplierCharacteristicsSource,
    build_comparison_rows,
)


def test_null_supplier_marks_placeholder():
    cust = [{"match_key": "a1", "display_label": "X", "value_display": "1"}]
    rows, empty = build_comparison_rows(cust, [])
    assert empty is True
    assert len(rows) == 1
    assert rows[0].row_class == "no_supplier"


def test_both_match_mismatch():
    cust = [{"match_key": "k", "display_label": "N", "value_display": "  same  "}]
    sup = [SupplierCharacteristic(match_key="k", display_label="N", display_value="same")]
    rows, empty = build_comparison_rows(cust, sup)
    assert empty is False
    assert rows[0].row_class == "match"

    cust2 = [{"match_key": "k", "display_label": "N", "value_display": "a"}]
    sup2 = [SupplierCharacteristic(match_key="k", display_label="N", display_value="b")]
    rows2, _ = build_comparison_rows(cust2, sup2)
    assert rows2[0].row_class == "mismatch"


def test_stub_source_empty():
    s = NullSupplierCharacteristicsSource()
    assert s.get_for_ktru("x.y") == []

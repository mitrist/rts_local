"""
Характеристики товаров поставщика для сравнения с заказчиком из ЕИС.

Сейчас: заглушка NullSupplierCharacteristicsSource (ожидается БД позже).

Сравнение: MVP по нормализованной строке значения после join по match_key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal, Protocol, runtime_checkable


_WS_RE = re.compile(r"\s+")


def normalize_compare_value(val: str) -> str:
    return _WS_RE.sub(" ", (val or "").strip()).lower()


@dataclass(frozen=True, slots=True)
class SupplierCharacteristic:
    match_key: str
    display_label: str
    display_value: str

    def as_dict(self) -> dict[str, str]:
        return {
            "match_key": normalize_compare_value(self.match_key),
            "display_label": self.display_label,
            "display_value": self.display_value,
        }


@runtime_checkable
class SupplierCharacteristicsSource(Protocol):
    def get_for_ktru(self, ktru_code: str) -> list[SupplierCharacteristic]:
        ...


class NullSupplierCharacteristicsSource:
    """Профиль поставщика недоступен до подключения БД."""

    def get_for_ktru(self, ktru_code: str) -> list[SupplierCharacteristic]:
        _ = ktru_code
        return []


CmpLabel = Literal["match", "mismatch", "customer_only", "supplier_only", "no_supplier"]


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    match_key: str
    label: str
    customer_value: str
    supplier_value: str
    row_class: CmpLabel

    def as_dict(self) -> dict[str, str]:
        return {
            "match_key": self.match_key,
            "label": self.label,
            "customer_value": self.customer_value,
            "supplier_value": self.supplier_value,
            "row_class": self.row_class,
        }


def build_comparison_rows(
    customer: Iterable[dict[str, str]],
    supplier: Iterable[SupplierCharacteristic],
) -> tuple[list[ComparisonRow], bool]:
    """
    customer: элементы как dict(display_label, match_key, value_display)
    supplier: список характеристик поставщика (match_key уже нормализуем внутри)
    Возвращает строки таблицы и флаг supplier_empty.
    """
    cust_map: dict[str, tuple[str, str]] = {}
    for raw in customer:
        mk_raw = str(raw.get("match_key") or "").strip()
        mk = normalize_compare_value(mk_raw) if mk_raw else ""
        if not mk:
            continue
        label = str(raw.get("display_label") or "").strip() or "(без имени)"
        cust_map[mk] = (label, str(raw.get("value_display") or "").strip())

    sup_list = list(supplier)
    if not sup_list:
        rows = [
            ComparisonRow(
                match_key=mk,
                label=lbl,
                customer_value=v,
                supplier_value="—",
                row_class="no_supplier",
            )
            for mk, (lbl, v) in sorted(cust_map.items(), key=lambda x: x[1][0])
        ]
        return rows, True

    sup_map: dict[str, tuple[str, str]] = {}
    for s in sup_list:
        mk = normalize_compare_value(s.match_key)
        if not mk:
            continue
        sup_map[mk] = (s.display_label.strip() or "(без имени)", (s.display_value or "").strip())

    all_keys = sorted(set(cust_map) | set(sup_map))

    rows: list[ComparisonRow] = []
    for mk in all_keys:
        c_ent = cust_map.get(mk)
        s_ent = sup_map.get(mk)
        if c_ent and s_ent:
            cl, cv = c_ent
            sl, sv = s_ent
            label = cl if normalize_compare_value(cl) == normalize_compare_value(sl) else f"{cl} / {sl}"
            same = normalize_compare_value(cv) == normalize_compare_value(sv) and bool(cv or sv)
            rc: CmpLabel = "match" if same else "mismatch"
            rows.append(
                ComparisonRow(
                    match_key=mk,
                    label=label,
                    customer_value=cv or "—",
                    supplier_value=sv or "—",
                    row_class=rc,
                )
            )
        elif c_ent:
            rows.append(
                ComparisonRow(
                    match_key=mk,
                    label=c_ent[0],
                    customer_value=c_ent[1] or "—",
                    supplier_value="—",
                    row_class="customer_only",
                )
            )
        else:
            rows.append(
                ComparisonRow(
                    match_key=mk,
                    label=s_ent[0] if s_ent else "",
                    customer_value="—",
                    supplier_value=s_ent[1] if s_ent else "—",
                    row_class="supplier_only",
                )
            )

    rows.sort(key=lambda r: ((r.row_class != "match"), r.label.lower()))
    return rows, False

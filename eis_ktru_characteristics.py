"""
Извлечение из XML архива ЕИС характеристик КТРУ заказчика (purchaseObject/KTRU/characteristics).

Покрывает characteristicsUsingReferenceInfo и characteristicsUsingTextForm по схемам ЕИС 16.x.
Не обрабатывает drugPurchaseObjectsInfo и только-ОКПД2 — см. PHASE2_PARSE_NOTE.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# Фаза 2 (план): OKPD2/characteristics, расширенное числовое сравнение.
PHASE2_PARSE_NOTE = "Парсер: характеристики только по ОКПД2 пока не извлекаются."


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


_WS_RE = re.compile(r"\s+")


def _norm_key(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").strip()).lower()


def _t(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _child(parent: ET.Element | None, *local_names: str) -> ET.Element | None:
    if parent is None:
        return None
    ls = frozenset(local_names)
    for c in parent:
        if _local(c.tag) in ls:
            return c
    return None


def _first_desc(parent: ET.Element | None, *local_names: str) -> ET.Element | None:
    if parent is None:
        return None
    ls = frozenset(local_names)
    for sub in parent.iter():
        if sub is parent:
            continue
        if _local(sub.tag) in ls:
            return sub
    return None


def _children(parent: ET.Element | None, local_name: str) -> list[ET.Element]:
    if parent is None:
        return []
    return [c for c in parent if _local(c.tag) == local_name]


def _format_customer_organization_block(*, full_name: str, inn: str, reg_num: str, delivery: str) -> str:
    """Текст для модалки: название, ИНН или СПЗ, адрес доставки (каждый с новой строки при наличии)."""
    lines: list[str] = []
    nm = (full_name or "").strip()
    if nm:
        lines.append(nm)
    i = (inn or "").strip()
    if i:
        lines.append(f"ИНН {i}")
    else:
        r = (reg_num or "").strip()
        if r and r != "00000000000":
            lines.append(f"Код заказчика по СПЗ: {r}")
    d = (delivery or "").strip()
    if d:
        lines.append(f"Адрес поставки: {d}")
    return "\n".join(lines)


def _org_first_inn_text(org_el: ET.Element | None) -> str:
    """ИНН может быть расширением к organizationRef или в типовых вложенных блоках."""
    if org_el is None:
        return ""
    direct = _t(_child(org_el, "inn"))
    if direct:
        return direct
    for sub in org_el.iter():
        if sub is org_el:
            continue
        if _local(sub.tag) == "inn" and _t(sub):
            return _t(sub)
    return ""


def _delivery_places_text_from_info(delivery_places_info: ET.Element | None) -> str:
    if delivery_places_info is None:
        return ""
    parts: list[str] = []
    for dpl in _children(delivery_places_info, "deliveryPlaceInfo"):
        place_text = _t(_child(dpl, "deliveryPlace"))
        kl = _child(dpl, "kladr")
        kl_name = ""
        if kl is not None:
            kl_name = _t(_child(kl, "fullName"))
        blob = " — ".join(x for x in (place_text, kl_name) if x)
        if blob:
            parts.append(blob)
    return " | ".join(parts)


@dataclass
class CustomerRequirementSnap:
    reg_num: str
    full_name: str
    inn: str
    delivery: str


def _iter_customer_requirement_info_elements(doc_root: ET.Element) -> list[ET.Element]:
    out: list[ET.Element] = []
    seen: set[int] = set()
    for parent in doc_root.iter():
        if _local(parent.tag) != "customerRequirementsInfo":
            continue
        for ch in parent:
            if _local(ch.tag) != "customerRequirementInfo":
                continue
            iid = id(ch)
            if iid not in seen:
                seen.add(iid)
                out.append(ch)
    return out


def _customer_requirements_map(doc_root: ET.Element) -> tuple[dict[str, CustomerRequirementSnap], list[CustomerRequirementSnap]]:
    by_reg: dict[str, CustomerRequirementSnap] = {}
    ordered: list[CustomerRequirementSnap] = []
    for cri in _iter_customer_requirement_info_elements(doc_root):
        cust_el = _child(cri, "customer")
        reg = (_t(_child(cust_el, "regNum")) or "").strip()
        fn = (_t(_child(cust_el, "fullName")) or "").strip()
        inn = (_org_first_inn_text(cust_el) or "").strip()
        cci = _child(cri, "contractConditionsInfo")
        dpi = _child(cci, "deliveryPlacesInfo") if cci is not None else None
        dv_raw = (_delivery_places_text_from_info(dpi) or "").strip()
        snap = CustomerRequirementSnap(reg_num=reg, full_name=fn, inn=inn, delivery=dv_raw)
        ordered.append(snap)
        if not reg:
            continue
        if reg not in by_reg:
            by_reg[reg] = snap
            continue
        prev = by_reg[reg]
        merged_deliv = prev.delivery
        if dv_raw:
            parts = [p.strip() for p in merged_deliv.split(" | ") if p.strip()] if merged_deliv else []
            if dv_raw not in parts:
                parts.append(dv_raw)
            merged_deliv = " | ".join(parts)
        merged_fn = prev.full_name or fn
        merged_inn = prev.inn or inn
        by_reg[reg] = CustomerRequirementSnap(reg_num=reg, full_name=merged_fn, inn=merged_inn, delivery=merged_deliv)
    return by_reg, ordered


def _longest_delivery_places_fallback(doc_root: ET.Element) -> str:
    best = ""
    for el in doc_root.iter():
        if _local(el.tag) != "deliveryPlacesInfo":
            continue
        t = _delivery_places_text_from_info(el)
        if len(t) > len(best):
            best = t
    return best


def _purchase_object_linked_customer_regs(po: ET.Element) -> list[str]:
    regs: list[str] = []
    for container_nm in ("customerQuantities", "customerQuantitiesCH"):
        cnt = _child(po, container_nm)
        if cnt is None:
            continue
        for qty_nm in ("customerQuantity", "customerQuantityCH"):
            for qty_el in _children(cnt, qty_nm):
                c = _child(qty_el, "customer")
                rn = (_t(_child(c, "regNum")) or "").strip()
                if rn and rn not in regs:
                    regs.append(rn)
    return regs


def _purchase_object_customer_organization_display(
    po: ET.Element,
    *,
    by_reg: dict[str, CustomerRequirementSnap],
    ordered: list[CustomerRequirementSnap],
    global_delivery_fallback: str,
) -> str:
    if not ordered and not global_delivery_fallback:
        return ""

    linked_regs = _purchase_object_linked_customer_regs(po)

    chosen: CustomerRequirementSnap | None = None
    for rn in linked_regs:
        chosen = by_reg.get(rn)
        if chosen is not None:
            break

    if chosen is not None:
        delivery = chosen.delivery.strip() if chosen.delivery.strip() else global_delivery_fallback.strip()
        return _format_customer_organization_block(
            full_name=chosen.full_name,
            inn=chosen.inn,
            reg_num=chosen.reg_num,
            delivery=delivery,
        ).strip()

    if len(ordered) == 1:
        snap = ordered[0]
        delivery = snap.delivery.strip() if snap.delivery.strip() else global_delivery_fallback.strip()
        return _format_customer_organization_block(
            full_name=snap.full_name,
            inn=snap.inn,
            reg_num=snap.reg_num,
            delivery=delivery,
        ).strip()

    if len(ordered) > 1:
        blocks = []
        for snap in ordered:
            delivery = snap.delivery.strip() if snap.delivery.strip() else global_delivery_fallback.strip()
            block = _format_customer_organization_block(
                full_name=snap.full_name,
                inn=snap.inn,
                reg_num=snap.reg_num,
                delivery=delivery,
            ).strip()
            if block:
                blocks.append(block)
        return "\n\n".join(blocks).strip()

    if global_delivery_fallback.strip():
        return _format_customer_organization_block(full_name="", inn="", reg_num="", delivery=global_delivery_fallback).strip()

    return ""


def _customer_display_by_regs(
    regs: list[str],
    *,
    by_reg: dict[str, CustomerRequirementSnap],
    ordered: list[CustomerRequirementSnap],
    global_delivery_fallback: str,
) -> str:
    blocks: list[str] = []
    seen: set[str] = set()
    for rn in regs:
        snap = by_reg.get(rn)
        if snap is None or rn in seen:
            continue
        seen.add(rn)
        delivery = snap.delivery.strip() if snap.delivery.strip() else global_delivery_fallback.strip()
        block = _format_customer_organization_block(
            full_name=snap.full_name,
            inn=snap.inn,
            reg_num=snap.reg_num,
            delivery=delivery,
        ).strip()
        if block:
            blocks.append(block)
    if blocks:
        return "\n\n".join(blocks)
    if len(ordered) == 1:
        snap = ordered[0]
        delivery = snap.delivery.strip() if snap.delivery.strip() else global_delivery_fallback.strip()
        return _format_customer_organization_block(
            full_name=snap.full_name,
            inn=snap.inn,
            reg_num=snap.reg_num,
            delivery=delivery,
        ).strip()
    if global_delivery_fallback.strip():
        return _format_customer_organization_block(
            full_name="",
            inn="",
            reg_num="",
            delivery=global_delivery_fallback,
        ).strip()
    return ""


def extract_notice_max_contract_display(doc_root: ET.Element) -> str:
    """Начальная (макс.) цена контракта по извещению (maxPriceInfo/maxPrice + валюта)."""
    for el in doc_root.iter():
        if _local(el.tag) != "maxPriceInfo":
            continue
        price_txt = _t(_child(el, "maxPrice"))
        if not price_txt:
            continue
        cur_el = _child(el, "currency")
        code = ""
        if cur_el is not None:
            code = (_t(_child(cur_el, "code"))) or (_t(_child(cur_el, "digitalCode")) or _t(_child(cur_el, "name")))
        return f"{price_txt} {code}".strip() if code else price_txt
    return ""


def _okei_caption(elem: ET.Element) -> str:
    """Краткая подпись единицы ОКЕИ из дочернего дерева."""
    codes = [_t(ch) for ch in elem.iter() if _local(ch.tag) == "code" and _t(ch)]
    names = [_t(ch) for ch in elem.iter() if _local(ch.tag) == "name" and _t(ch)]
    if names:
        return names[0]
    return codes[0] if codes else ""


def _format_value_range(elem: ET.Element) -> str:
    """Сводка диапазона valueRange/outValueRange (min/max/MathNotation)."""
    parts: list[str] = []
    for ch in elem.iter():
        ln = _local(ch.tag)
        if ln in ("minMathNotation", "maxMathNotation"):
            txt = _t(ch)
            if txt:
                parts.append(txt)
        if ln == "min" and _t(ch):
            parts.append(f"мин {_t(ch)}")
        if ln == "max" and _t(ch):
            parts.append(f"макс {_t(ch)}")
    dedup_seen: list[str] = []
    for p in parts:
        if p not in dedup_seen:
            dedup_seen.append(p)
    return " ".join(dedup_seen)


def _format_numeric_value_bundle(val_el: ET.Element) -> str:
    qs: list[str] = []
    okei = _child(val_el, "OKEI")
    if okei is not None:
        oc = _okei_caption(okei)
        if oc:
            qs.append(f"ОКЕИ {oc}")

    vd = _t(_child(val_el, "qualityDescription"))
    if vd:
        qs.append(vd)

    cv = _t(_child(val_el, "concreteValue"))
    if cv:
        qs.append(cv)

    for rs in ("rangeSet",):
        rng = _child(val_el, rs)
        if rng is None:
            continue
        for vr in rng.iter():
            if _local(vr.tag) in ("valueRange", "outValueRange"):
                s = _format_value_range(vr)
                if s:
                    qs.append(s)

    for vs in ("valueSet",):
        block = _child(val_el, vs)
        if block is None:
            continue
        for conc in block.iter():
            if _local(conc.tag) == "concreteValue":
                vx = _t(conc)
                if vx:
                    qs.append(vx)

    return " ".join(dict.fromkeys(qs)) if qs else ""


def _values_display(values_parent: ET.Element | None, *, ref_style: bool) -> str:
    if values_parent is None:
        return ""
    blocks: list[str] = []
    values_el = _child(values_parent, "values")
    if values_el is None:
        return ""
    for v in _children(values_el, "value"):
        blob = _format_numeric_value_bundle(v)
        if blob:
            blocks.append(blob)
        elif ref_style:
            inner = [_t(ch) for ch in v.iter() if _local(ch.tag) == "qualityDescription" or _local(ch.tag) == "code"]
            if inner:
                blocks.append("; ".join(x for x in inner if x))
    return " | ".join(blocks) if blocks else ""


def _parse_reference_characteristic(elem: ET.Element) -> CustomerCharacteristic:
    code = ""
    nm = ""
    typ = ""
    kind = ""
    for ch in elem:
        ln = _local(ch.tag)
        if ln == "code":
            code = _t(ch) or code
        elif ln == "name":
            nm = _t(ch) or nm
        elif ln == "type":
            typ = _t(ch) or typ
        elif ln == "kind":
            kind = _t(ch) or kind
        elif ln == "values":
            break
    vd = _values_display(elem, ref_style=True)

    lbl = nm or code or "(характеристика без имени)"
    mk = code if code.strip() else f"name:{_norm_key(nm) or lbl}"
    type_suffix = ""
    if typ or kind:
        type_suffix = f" [тип {typ}]" if typ else ""
        if kind:
            type_suffix += f" вид {kind}"
    if type_suffix:
        vd = (vd + type_suffix).strip()
    return CustomerCharacteristic(
        match_key=_norm_key(mk),
        characteristic_kind="reference",
        characteristic_code=code.strip() or None,
        display_label=lbl.strip() or lbl,
        value_display=(vd.strip() or "—").strip(),
    )


def _parse_manual_characteristic(elem: ET.Element) -> CustomerCharacteristic:
    nm = _t(_child(elem, "name")) or "(характеристика без имени)"
    typ = _t(_child(elem, "type"))
    vd = _values_display(elem, ref_style=False)

    lbl = nm
    mk = f"name:{_norm_key(nm)}"
    if typ:
        vd = f"{vd} [тип {typ}]".strip()
    return CustomerCharacteristic(
        match_key=_norm_key(mk) if mk != "name:" else _norm_key(f"manual:{lbl}"),
        characteristic_kind="manual",
        characteristic_code=None,
        display_label=lbl.strip(),
        value_display=(vd.strip() or "—").strip(),
    )


def _parse_characteristics_block(ch_block: ET.Element) -> list[CustomerCharacteristic]:
    out: list[CustomerCharacteristic] = []
    for ch in ch_block:
        ln = _local(ch.tag)
        if ln == "characteristicsUsingReferenceInfo":
            out.append(_parse_reference_characteristic(ch))
        elif ln == "characteristicsUsingTextForm":
            out.append(_parse_manual_characteristic(ch))
        elif ln == "addCharacteristicInfoReason":
            t = _t(ch)
            if t:
                out.append(
                    CustomerCharacteristic(
                        match_key=_norm_key("addCharacteristicInfoReason"),
                        characteristic_kind="manual",
                        characteristic_code=None,
                        display_label="Обоснование доп. информации о товаре",
                        value_display=t,
                    )
                )
    return out


def _ktru_position_code(ktru_elem: ET.Element | None) -> str:
    if ktru_elem is None:
        return ""
    code_el = _child(ktru_elem, "code")
    return _t(code_el)


def iter_purchase_object_elements_under_poi(poi: ET.Element) -> list[ET.Element]:
    items: list[ET.Element] = []
    for ch in poi:
        ln = _local(ch.tag)
        if ln == "purchaseObject":
            items.append(ch)
        elif ln == "notDrugPurchaseObjectsInfo":
            for gc in ch:
                if _local(gc.tag) == "purchaseObject":
                    items.append(gc)
    return items


def iter_purchase_object_elements(doc_root: ET.Element) -> list[ET.Element]:
    found: list[ET.Element] = []
    seen: set[int] = set()
    for el in doc_root.iter():
        if _local(el.tag) != "purchaseObjectsInfo":
            continue
        for po in iter_purchase_object_elements_under_poi(el):
            iid = id(po)
            if iid not in seen:
                seen.add(iid)
                found.append(po)
    return found


def iter_drug_purchase_object_elements(doc_root: ET.Element) -> list[ET.Element]:
    found: list[ET.Element] = []
    seen: set[int] = set()
    for el in doc_root.iter():
        if _local(el.tag) != "drugPurchaseObjectInfo":
            continue
        iid = id(el)
        if iid in seen:
            continue
        seen.add(iid)
        found.append(el)
    return found


@dataclass
class CustomerCharacteristic:
    match_key: str
    characteristic_kind: str  # reference | manual
    characteristic_code: str | None
    display_label: str
    value_display: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "match_key": self.match_key,
            "characteristic_kind": self.characteristic_kind,
            "characteristic_code": self.characteristic_code,
            "display_label": self.display_label,
            "value_display": self.value_display,
        }


@dataclass
class KtruPurchaseLine:
    line_key: str
    purchase_number: str
    object_sid: str
    object_external_sid: str
    position_index: int
    ktru_code: str
    #: Наименование объекта закупки (purchaseObject/name)
    position_display_name: str = ""
    #: Единица измерения (HTML common-info / purchaseObject OKEI)
    unit_display: str = ""
    #: Количество (объём работ, услуг)
    quantity_display: str = ""
    #: Цена за единицу измерения (purchaseObject/price), строка как в XML
    price_per_unit_display: str = ""
    #: Стоимость позиции (purchaseObject/sum), если есть
    line_sum_display: str = ""
    #: НМЦК / макс. цена контракта по извещению (дублируется на каждой строке для удобства JSON)
    notice_total_display: str = ""
    #: Наименование заказчика, ИНН/СПЗ, адрес поставки (многострочный текст из customerRequirementsInfo)
    customer_organization_display: str = ""
    characteristics: list[CustomerCharacteristic] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "line_key": self.line_key,
            "purchase_number": self.purchase_number,
            "object_sid": self.object_sid,
            "object_external_sid": self.object_external_sid,
            "position_index": self.position_index,
            "ktru_code": self.ktru_code,
            "position_display_name": self.position_display_name,
            "unit_display": self.unit_display,
            "quantity_display": self.quantity_display,
            "price_per_unit_display": self.price_per_unit_display,
            "line_sum_display": self.line_sum_display,
            "notice_total_display": self.notice_total_display,
            "customer_organization_display": self.customer_organization_display,
            "characteristics": [c.as_dict() for c in self.characteristics],
        }


def _make_line_key(
    *,
    pn: str,
    ktru_code: str,
    sid: str,
    ext_sid: str,
    idx: int,
) -> str:
    base = f"{pn}|{ktru_code}"
    if sid.strip():
        return f"{base}|sid:{sid.strip()}"
    if ext_sid.strip():
        return f"{base}|ext:{ext_sid.strip()}"
    return f"{base}|idx:{idx}"


def _first_desc_text(parent: ET.Element | None, *local_names: str) -> str:
    el = _first_desc(parent, *local_names)
    return _t(el)


def _direct_or_desc_text(parent: ET.Element | None, *local_names: str) -> str:
    direct = _t(_child(parent, *local_names))
    return direct or _first_desc_text(parent, *local_names)


def _append_drug_characteristic(out: list[CustomerCharacteristic], key: str, label: str, value: str) -> None:
    val = (value or "").strip()
    if not val:
        return
    pair = (_norm_key(key), val)
    if any((c.match_key, c.value_display) == pair for c in out):
        return
    out.append(
        CustomerCharacteristic(
            match_key=_norm_key(key),
            characteristic_kind="manual",
            characteristic_code=None,
            display_label=label,
            value_display=val,
        )
    )


def _drug_object_linked_customer_regs(dpo: ET.Element) -> list[str]:
    regs: list[str] = []
    for block_name in ("drugQuantityCustomerInfo", "drugPurchaseObjectCustomerInfo"):
        for item in dpo.iter():
            if _local(item.tag) != block_name:
                continue
            c = _child(item, "customer")
            rn = (_t(_child(c, "regNum")) or "").strip()
            if rn and rn not in regs:
                regs.append(rn)
    return regs


def _drug_ktru_element(dpo: ET.Element) -> ET.Element | None:
    for sub in dpo.iter():
        if sub is dpo:
            continue
        if _local(sub.tag) in ("KTRU", "KTRUInfo"):
            return sub
    return None


def _drug_position_name(dpo: ET.Element) -> str:
    direct_name = _t(_child(dpo, "name"))
    if direct_name:
        return direct_name
    parts = [
        _first_desc_text(dpo, "MNNName"),
        _first_desc_text(dpo, "tradeName"),
        _first_desc_text(dpo, "medicamentalFormName"),
        _first_desc_text(dpo, "dosageGRLSValue"),
    ]
    return " · ".join(dict.fromkeys(x for x in parts if x))


def _parse_drug_characteristics(dpo: ET.Element, ktru_el: ET.Element | None) -> list[CustomerCharacteristic]:
    out: list[CustomerCharacteristic] = []
    ch_el = _child(ktru_el, "characteristics", "characteristicsInfo") if ktru_el is not None else None
    if ch_el is not None:
        out.extend(_parse_characteristics_block(ch_el))

    _append_drug_characteristic(out, "drug:mnn", "МНН", _first_desc_text(dpo, "MNNName"))
    _append_drug_characteristic(out, "drug:tradeName", "Торговое наименование", _first_desc_text(dpo, "tradeName"))
    _append_drug_characteristic(
        out,
        "drug:medicamentalForm",
        "Лекарственная форма",
        _first_desc_text(dpo, "medicamentalFormName"),
    )
    _append_drug_characteristic(out, "drug:dosage", "Дозировка", _first_desc_text(dpo, "dosageGRLSValue"))
    _append_drug_characteristic(
        out,
        "drug:packaging1Quantity",
        "Количество лекарственных форм в первичной упаковке",
        _first_desc_text(dpo, "packaging1Quantity"),
    )
    _append_drug_characteristic(
        out,
        "drug:packaging2Quantity",
        "Количество первичных упаковок во вторичной упаковке",
        _first_desc_text(dpo, "packaging2Quantity"),
    )
    _append_drug_characteristic(
        out,
        "drug:sumaryPackagingQuantity",
        "Количество потребительских единиц в упаковке",
        _first_desc_text(dpo, "sumaryPackagingQuantity"),
    )
    _append_drug_characteristic(out, "drug:quantity", "Количество", _first_desc_text(dpo, "drugQuantity", "total"))
    _append_drug_characteristic(out, "drug:isZNVLP", "ЖНВЛП", _direct_or_desc_text(dpo, "isZNVLP"))
    _append_drug_characteristic(
        out,
        "drug:isNarcotic",
        "Наркотические/психотропные вещества",
        _direct_or_desc_text(dpo, "isNarcotic"),
    )
    return out


def extract_ktru_customer_lines_from_root(doc_root: ET.Element, *, purchase_number: str) -> list[KtruPurchaseLine]:
    pn = purchase_number.strip()
    lines: list[KtruPurchaseLine] = []
    notice_total = extract_notice_max_contract_display(doc_root)
    by_reg, ordered = _customer_requirements_map(doc_root)
    global_delivery = _longest_delivery_places_fallback(doc_root)
    pos = iter_purchase_object_elements(doc_root)
    for ix, po in enumerate(pos):
        sid = _t(_child(po, "sid"))
        ext = _t(_child(po, "externalSid"))
        pos_name = _t(_child(po, "name"))
        price_txt = _t(_child(po, "price"))
        sum_txt = _t(_child(po, "sum"))
        cust_org = _purchase_object_customer_organization_display(
            po,
            by_reg=by_reg,
            ordered=ordered,
            global_delivery_fallback=global_delivery,
        )
        ktru_el = _child(po, "KTRU")
        if ktru_el is None:
            continue
        kc = _ktru_position_code(ktru_el)
        if not kc:
            continue
        ch_el = _child(ktru_el, "characteristics")
        chs = _parse_characteristics_block(ch_el) if ch_el is not None else []
        lk = _make_line_key(pn=pn, ktru_code=kc, sid=sid, ext_sid=ext, idx=ix)
        lines.append(
            KtruPurchaseLine(
                line_key=lk,
                purchase_number=pn,
                object_sid=sid,
                object_external_sid=ext,
                position_index=ix,
                ktru_code=kc,
                position_display_name=(pos_name or "").strip(),
                price_per_unit_display=(price_txt or "").strip(),
                line_sum_display=(sum_txt or "").strip(),
                notice_total_display=(notice_total or "").strip(),
                customer_organization_display=(cust_org or "").strip(),
                characteristics=chs,
            )
        )
    for ix, dpo in enumerate(iter_drug_purchase_object_elements(doc_root), start=len(lines)):
        sid = _t(_child(dpo, "sid"))
        ext = _t(_child(dpo, "externalSid"))
        ktru_el = _drug_ktru_element(dpo)
        kc = _ktru_position_code(ktru_el)
        if not kc:
            continue
        regs = _drug_object_linked_customer_regs(dpo)
        cust_org = _customer_display_by_regs(
            regs,
            by_reg=by_reg,
            ordered=ordered,
            global_delivery_fallback=global_delivery,
        )
        price_txt = _direct_or_desc_text(dpo, "pricePerUnit", "price")
        sum_txt = _direct_or_desc_text(dpo, "positionPrice")
        chs = _parse_drug_characteristics(dpo, ktru_el)
        lk = _make_line_key(pn=pn, ktru_code=kc, sid=sid, ext_sid=ext, idx=ix)
        lines.append(
            KtruPurchaseLine(
                line_key=lk,
                purchase_number=pn,
                object_sid=sid,
                object_external_sid=ext,
                position_index=ix,
                ktru_code=kc,
                position_display_name=_drug_position_name(dpo),
                price_per_unit_display=(price_txt or "").strip(),
                line_sum_display=(sum_txt or "").strip(),
                notice_total_display=(notice_total or "").strip(),
                customer_organization_display=(cust_org or "").strip(),
                characteristics=chs,
            )
        )
    return lines


def extract_ktru_customer_lines_from_xml_bytes(data: bytes, *, purchase_number: str) -> list[KtruPurchaseLine]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    return extract_ktru_customer_lines_from_root(root, purchase_number=purchase_number)

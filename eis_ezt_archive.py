"""
Разбор ZIP из getDocsIP: поиск реестрового номера закупки и кодов КТРУ в XML (ЭЗТ и др.).

Используется локальное имя тега без NS (после '}'), чтобы не зависеть от префиксов.
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from eis_ktru_characteristics import KtruPurchaseLine, extract_ktru_customer_lines_from_xml_bytes


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


@dataclass
class NoticeMatch:
    purchase_number: str
    ktru_codes: list[str] = field(default_factory=list)
    purchase_method_codes: list[str] = field(default_factory=list)
    source_file: str = ""
    #: Дата/время из publishDTInEIS (размещение в ЕИС)
    publish_dt_in_eis: str = ""
    #: Дата/время из publishDTInETP (размещение на ЭТП), если нет даты ЕИС в XML
    publish_dt_in_etp: str = ""
    #: Объекты закупки (КТРУ) с характеристиками заказчика
    customer_ktru_lines: list[KtruPurchaseLine] = field(default_factory=list)


def _build_parent_map(root: ET.Element) -> dict[ET.Element, ET.Element | None]:
    """Родитель каждого узла без изменения Element (в Py3.12+ нельзя вешать произвольные атрибуты)."""
    parent_of: dict[ET.Element, ET.Element | None] = {}
    for p in root.iter():
        for c in p:
            parent_of[c] = p
    return parent_of


# Типичный номер позиции КТРУ: 21.20.10.231-00013, 26.60.12.119-00000013; иногда с суффиксом @…
_KTRU_CODE_RE = re.compile(
    r"^\d{2}\.\d{2}\.\d{2}\.\d{2,5}-\d{2,}(?:@\d+(?:-\d+)*)?$",
)


def _looks_like_ktru_catalog_code(text: str) -> bool:
    t = text.strip()
    if not t or "," in t or " " in t:
        return False
    return bool(_KTRU_CODE_RE.fullmatch(t))


def _is_code_directly_under_ktru(
    elem: ET.Element, parent_of: dict[ET.Element, ET.Element | None]
) -> bool:
    """Только <code> непосредственно внутри KTRU/KTRUInfo — не из характеристик/вложенных блоков."""
    p = parent_of.get(elem)
    if p is None:
        return False
    return _local(p.tag) in ("KTRU", "KTRUInfo")


def _xml_indicates_ezt_hint(raw: bytes) -> bool:
    """Грубая проверка по тексту XML (без полного парсинга NS)."""
    s = raw.decode("utf-8", errors="ignore")
    return any(
        x in s
        for x in (
            "notificationEZT",
            "epNotificationEZT",
            "protocolEZT",
            "purchaseNotificationEZT",
        )
    )


def extract_from_xml_bytes(data: bytes, filename: str = "") -> NoticeMatch | None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    parent_of = _build_parent_map(root)

    purchase_number = ""
    publish_eis = ""
    publish_etp = ""
    ktru_codes: list[str] = []
    method_codes: list[str] = []

    for el in root.iter():
        ln = _local(el.tag)
        if ln == "purchaseNumber" and el.text and el.text.strip():
            if not purchase_number:
                purchase_number = el.text.strip()
        if ln == "publishDTInEIS" and el.text and el.text.strip():
            if not publish_eis:
                publish_eis = el.text.strip()
        if ln == "publishDTInETP" and el.text and el.text.strip():
            if not publish_etp:
                publish_etp = el.text.strip()
        if ln == "purchaseCode":
            for child in el:
                if _local(child.tag) == "code" and child.text and child.text.strip():
                    method_codes.append(child.text.strip())
        if ln == "code" and el.text and el.text.strip():
            t = el.text.strip()
            if _is_code_directly_under_ktru(el, parent_of) and _looks_like_ktru_catalog_code(t):
                ktru_codes.append(t)

    ktru_sorted = sorted(set(ktru_codes))
    methods = sorted(set(method_codes))

    if not purchase_number and not ktru_sorted:
        return None

    pn_use = purchase_number.strip()
    cust_lines = extract_ktru_customer_lines_from_xml_bytes(data, purchase_number=pn_use)
    return NoticeMatch(
        purchase_number=purchase_number,
        ktru_codes=ktru_sorted,
        purchase_method_codes=methods,
        source_file=filename,
        publish_dt_in_eis=publish_eis.strip(),
        publish_dt_in_etp=publish_etp.strip(),
        customer_ktru_lines=cust_lines,
    )


def iter_xml_entries_from_zip(zip_bytes: bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            low = name.lower()
            if not low.endswith(".xml"):
                continue
            with zf.open(name) as f:
                yield name, f.read()


def ktru_matches_prefixes(codes: list[str], prefixes: list[str]) -> bool:
    if not prefixes:
        return True
    for p in prefixes:
        p = p.strip()
        if not p:
            continue
        for c in codes:
            if c == p or c.startswith(p):
                return True
    return False


def collect_matches_from_zip(
    zip_bytes: bytes,
    *,
    ktru_prefixes: list[str],
    require_ezt_xml_hint: bool,
) -> list[NoticeMatch]:
    out: list[NoticeMatch] = []
    seen: set[str] = set()
    for fname, raw in iter_xml_entries_from_zip(zip_bytes):
        if require_ezt_xml_hint and not _xml_indicates_ezt_hint(raw):
            continue
        m = extract_from_xml_bytes(raw, fname)
        if not m:
            continue
        if not ktru_matches_prefixes(m.ktru_codes, ktru_prefixes):
            continue
        key = m.purchase_number.strip() if m.purchase_number.strip() else f"{fname}:{','.join(m.ktru_codes)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out

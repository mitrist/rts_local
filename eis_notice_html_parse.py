"""Parse EIS notice common-info HTML pages for KTRU positions."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine

_WS_RE = re.compile(r"\s+")
_INFO_RE = re.compile(r"showInfo\('([^']+)'")


def _clean_text(value: object) -> str:
    return _WS_RE.sub(" ", str(value or "").replace("\xa0", " ").strip())


def _norm_key(value: str) -> str:
    return _clean_text(value).lower()


def _direct_text(tag: Tag | None) -> str:
    if tag is None:
        return ""
    chunks: list[str] = []
    for child in tag.children:
        if isinstance(child, str):
            txt = _clean_text(child)
            if txt:
                chunks.append(txt)
        elif isinstance(child, Tag) and child.name == "br":
            continue
    return " ".join(chunks).strip()


def _cells(row: Tag) -> list[Tag]:
    return [c for c in row.find_all(["td", "th"], recursive=False)]


def _position_info_class(row: Tag) -> str:
    marker = row.find(attrs={"onclick": _INFO_RE})
    if not isinstance(marker, Tag):
        return ""
    raw = str(marker.get("onclick") or "")
    m = _INFO_RE.search(raw)
    return m.group(1).strip() if m else ""


def _cell_value_parts(cell: Tag) -> list[str]:
    """Extract one or more values from a characteristics table cell."""
    if cell.find("br") is not None:
        chunks: list[str] = []
        for piece in cell.get_text("\n", strip=True).splitlines():
            text = _clean_text(piece)
            if text:
                chunks.append(text)
        if chunks:
            return chunks
    if cell.find("li") is not None:
        chunks = [_clean_text(li.get_text(" ", strip=True)) for li in cell.find_all("li")]
        chunks = [x for x in chunks if x]
        if chunks:
            return chunks
    text = _clean_text(cell.get_text(" ", strip=True))
    return [text] if text else []


def _parse_characteristics(detail_row: Tag | None) -> list[CustomerCharacteristic]:
    if detail_row is None:
        return []
    out: list[CustomerCharacteristic] = []
    table = detail_row.find("table")
    if not isinstance(table, Tag):
        return out
    tbody = table.find("tbody")
    rows = tbody.find_all("tr", recursive=False) if isinstance(tbody, Tag) else table.find_all("tr")
    last_label = ""
    last_unit = ""
    for row in rows:
        cells = _cells(row)
        if len(cells) == 1:
            if not last_label:
                continue
            label = last_label
            unit = last_unit
            value_parts = _cell_value_parts(cells[0])
        elif len(cells) < 2:
            continue
        else:
            label = _clean_text(cells[0].get_text(" ", strip=True))
            unit = _clean_text(cells[2].get_text(" ", strip=True)) if len(cells) > 2 else ""
            if not label and not last_label:
                continue
            if not label and last_label:
                label = last_label
                unit = last_unit
            elif label:
                last_label = label
                last_unit = unit
            value_parts = _cell_value_parts(cells[1])
        if not value_parts:
            continue
        for value in value_parts:
            display = f"{value} {unit}".strip() if unit else value
            out.append(
                CustomerCharacteristic(
                    match_key=f"name:{_norm_key(label)}" if label else "name:",
                    characteristic_kind="html_common_info",
                    characteristic_code=None,
                    display_label=label or "(характеристика без имени)",
                    value_display=display or "—",
                )
            )
    return out


def _ktru_code_from_cell(cell: Tag | None) -> str:
    if cell is None:
        return ""
    link = cell.find("a", href=re.compile(r"/epz/ktru/ktruCard/commonInfo\.html"))
    if isinstance(link, Tag):
        return _clean_text(link.get_text(" ", strip=True))
    text = _clean_text(cell.get_text(" ", strip=True))
    for part in text.split():
        if re.match(r"\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{8}$", part):
            return part
    return ""


def _section_value_by_title(soup: BeautifulSoup, title: str) -> str:
    wanted = _norm_key(title)
    for section in soup.find_all("section", class_=lambda c: c and "blockInfo__section" in str(c)):
        if not isinstance(section, Tag):
            continue
        title_el = section.find(class_=lambda c: c and "section__title" in str(c))
        if not isinstance(title_el, Tag):
            continue
        if _norm_key(title_el.get_text(" ", strip=True)) != wanted:
            continue
        info_el = section.find(class_=lambda c: c and "section__info" in str(c))
        if isinstance(info_el, Tag):
            return _clean_text(info_el.get_text(" ", strip=True))
    fallback = _value_after_text(soup, title)
    if fallback and _norm_key(fallback) != wanted:
        return fallback
    return ""


def _value_after_text(soup: BeautifulSoup, title: str) -> str:
    wanted = _norm_key(title)
    for node in soup.find_all(string=True):
        if _norm_key(str(node)) != wanted:
            continue
        parent = node.parent if isinstance(node.parent, Tag) else None
        current: Tag | None = parent
        for _ in range(5):
            if current is None:
                break
            value = _first_meaningful_text_after(current, wanted)
            if value:
                return value
            current = current.parent if isinstance(current.parent, Tag) else None
    return ""


def _first_meaningful_text_after(start: Tag, title_norm: str) -> str:
    for el in start.find_all_next(["span", "div", "td", "p"], limit=40):
        if not isinstance(el, Tag):
            continue
        text = _clean_text(el.get_text(" ", strip=True))
        if not text:
            continue
        if _norm_key(text) == title_norm:
            continue
        if el.find(class_=lambda c: c and "section__title" in str(c)):
            continue
        return text
    return ""


def parse_notice_common_info_context(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html or "", "html.parser")
    return {
        "contract_start_date": _section_value_by_title(soup, "Дата начала исполнения контракта"),
        "contract_execution_term": _section_value_by_title(soup, "Срок исполнения контракта"),
        "region": _section_value_by_title(soup, "Регион"),
        "delivery_place": _section_value_by_title(soup, "Место поставки товара"),
        "delivery_days": _section_value_by_title(soup, "Срок поставки товара (календарных дней)"),
    }


def parse_notice_common_info_html(
    html: str,
    *,
    purchase_number: str,
    source_url: str = "",
) -> list[KtruPurchaseLine]:
    soup = BeautifulSoup(html or "", "html.parser")
    root = soup.find(id="positionKTRU")
    if not isinstance(root, Tag):
        return []
    main_table = root.find("table", class_=lambda c: c and "tableBlock" in str(c))
    if not isinstance(main_table, Tag):
        return []
    tbody = main_table.find("tbody", class_=lambda c: c and "tableBlock__body" in str(c))
    rows = tbody.find_all("tr", recursive=False) if isinstance(tbody, Tag) else main_table.find_all("tr")
    lines: list[KtruPurchaseLine] = []
    for row in rows:
        if not isinstance(row, Tag):
            continue
        info_class = _position_info_class(row)
        if not info_class:
            continue
        cells = _cells(row)
        if len(cells) < 7:
            continue
        ktru_code = _ktru_code_from_cell(cells[1])
        if not ktru_code:
            continue
        detail = root.find("tr", class_=lambda c, cls=info_class: c and cls in str(c).split())
        characteristics = _parse_characteristics(detail if isinstance(detail, Tag) else None)
        position_name = _direct_text(cells[2]) or _clean_text(cells[2].get_text(" ", strip=True))
        unit_text = _clean_text(cells[3].get_text(" ", strip=True))
        quantity_text = _clean_text(cells[4].get_text(" ", strip=True))
        line_key = f"{purchase_number}|{ktru_code}|html:{len(lines)}"
        lines.append(
            KtruPurchaseLine(
                line_key=line_key,
                purchase_number=purchase_number,
                object_sid="",
                object_external_sid=source_url,
                position_index=len(lines),
                ktru_code=ktru_code,
                position_display_name=position_name,
                unit_display=unit_text,
                quantity_display=quantity_text,
                price_per_unit_display=_clean_text(cells[5].get_text(" ", strip=True)),
                line_sum_display=_clean_text(cells[6].get_text(" ", strip=True)),
                notice_total_display="",
                customer_organization_display="",
                characteristics=characteristics,
            )
        )
    return lines

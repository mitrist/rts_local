from __future__ import annotations

from pathlib import Path

from eis_notice_html_parse import parse_notice_common_info_context, parse_notice_common_info_html


def test_parse_common_info_html_fixture():
    html = Path(".cursor/plans/common_info.html").read_text(encoding="utf-8")

    lines = parse_notice_common_info_html(
        html,
        purchase_number="0117300004426000001",
        source_url="https://zakupki.gov.ru/epz/order/notice/ezt20/view/common-info.html?regNumber=0117300004426000001",
    )

    assert len(lines) == 1
    line = lines[0]
    assert line.purchase_number == "0117300004426000001"
    assert line.ktru_code == "08.12.12.140-00000005"
    assert line.position_display_name == "Щебень"
    assert "Тонна" in line.unit_display
    assert line.quantity_display == "1 053,00"
    assert line.price_per_unit_display == "2 200,00"
    assert line.line_sum_display == "2 316 600,00"
    assert len(line.characteristics) == 5
    assert line.characteristics[0].display_label == "Вид"
    assert line.characteristics[0].value_display == "Гранитный"

    context = parse_notice_common_info_context(html)
    assert context["contract_start_date"] == "с даты заключения контракта"
    assert context["contract_execution_term"] == "09.09.2026"
    assert context["region"] == "Алтайский край"
    assert "ул. Садовая" in context["delivery_place"]
    assert context["delivery_days"] == "30"


def test_parse_common_info_context_fallback_for_plain_neighbor_text():
    html = """
    <html><body>
      <div><span>Регион</span><span>Санкт-Петербург</span></div>
      <div><span>Срок поставки товара (календарных дней)</span><span>10</span></div>
      <div><span>Место поставки товара</span><span>муниципальный округ Смольнинское, пер Виленский, д. 15 литера В</span></div>
    </body></html>
    """

    context = parse_notice_common_info_context(html)

    assert context["region"] == "Санкт-Петербург"
    assert context["delivery_days"] == "10"
    assert "пер Виленский" in context["delivery_place"]


def test_parse_characteristics_continuation_rows():
    from bs4 import BeautifulSoup

    from eis_notice_html_parse import _parse_characteristics

    html = """
    <tr>
      <td colspan="3">
        <table>
          <tbody>
            <tr><td>Совместимость</td><td>Windows</td></tr>
            <tr><td></td><td>Linux</td></tr>
            <tr><td></td><td>MacOS</td></tr>
          </tbody>
        </table>
      </td>
    </tr>
    """
    soup = BeautifulSoup(html, "html.parser")
    row = soup.find("tr")
    chars = _parse_characteristics(row)
    labels = {c.display_label for c in chars}
    values = [c.value_display for c in chars if c.display_label == "Совместимость"]
    assert labels == {"Совместимость"}
    assert values == ["Windows", "Linux", "MacOS"]


def test_parse_characteristics_multiline_cell():
    from bs4 import BeautifulSoup

    from eis_notice_html_parse import _parse_characteristics

    html = """
    <tr>
      <td colspan="3">
        <table>
          <tbody>
            <tr><td>Совместимость</td><td>Windows<br/>Linux<br/>MacOS</td></tr>
          </tbody>
        </table>
      </td>
    </tr>
    """
    soup = BeautifulSoup(html, "html.parser")
    chars = _parse_characteristics(soup.find("tr"))
    assert [c.value_display for c in chars] == ["Windows", "Linux", "MacOS"]


def test_parse_characteristics_rowspan_continuation_rows():
    from bs4 import BeautifulSoup

    from eis_notice_html_parse import _parse_characteristics

    html = """
    <tr>
      <td colspan="3">
        <table>
          <tbody>
            <tr>
              <td rowspan="4">Наличие модулей и интерфейсов</td>
              <td>M.2</td>
              <td rowspan="4"></td>
            </tr>
            <tr><td>Type-C</td></tr>
            <tr><td>Gigabit Ethernet RJ45 8P8C</td></tr>
            <tr><td>HDMI</td></tr>
          </tbody>
        </table>
      </td>
    </tr>
    """
    soup = BeautifulSoup(html, "html.parser")
    chars = _parse_characteristics(soup.find("tr"))
    mods = [c for c in chars if c.display_label == "Наличие модулей и интерфейсов"]
    assert [c.value_display for c in mods] == [
        "M.2",
        "Type-C",
        "Gigabit Ethernet RJ45 8P8C",
        "HDMI",
    ]

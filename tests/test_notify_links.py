"""Форматирование ссылок в уведомлениях."""

from __future__ import annotations

from notify import html_link, markdown_link


def test_html_link_escapes_ampersand():
    url = "https://example.test/day-summary?a=1&b=2"
    out = html_link(url, "Сравнение характеристик КТРУ")
    assert 'href="https://example.test/day-summary?a=1&amp;b=2"' in out
    assert "Сравнение характеристик КТРУ</a>" in out


def test_markdown_link():
    out = markdown_link("https://example.test/rts", "Ссылка на ПП РТС")
    assert out == "[Ссылка на ПП РТС](https://example.test/rts)"

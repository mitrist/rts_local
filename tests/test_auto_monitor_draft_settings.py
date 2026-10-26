"""Tests for auto_monitor_draft_settings."""

from __future__ import annotations

from pathlib import Path

from auto_monitor_draft_settings import (
    form_from_settings,
    load_settings,
    parse_form_settings,
    parse_settings,
    save_settings,
    settings_path,
)


def test_defaults_and_roundtrip(tmp_path: Path):
    data = load_settings(Path("/nonexistent/auto_monitor_draft.json"))
    assert data["enabled"] is False
    assert data["rts_profile"] == "IPSokolova"
    assert data["min_pp_amount"] == 200000
    assert data["ktru_codes"] == []

    path = settings_path(tmp_path)
    save_settings(
        path,
        {
            "enabled": True,
            "ktru_codes": ["27.51.11.110-00000009", "08.12.12.140-00000005"],
            "min_pp_amount": 150000,
            "max_per_hour": 3,
            "rts_profile": "IPSokolova",
        },
    )
    loaded = load_settings(path)
    assert loaded["enabled"] is True
    assert loaded["min_pp_amount"] == 150000
    assert loaded["max_per_hour"] == 3
    assert loaded["ktru_codes"] == ["27.51.11.110-00000009", "08.12.12.140-00000005"]


def test_parse_form_settings():
    parsed = parse_form_settings(
        {
            "enabled": "on",
            "ktru_codes_text": "A\nB, C",
            "min_pp_amount": "200 000",
            "max_per_hour": "7",
            "rts_profile": "IPSokolova",
        }
    )
    assert parsed["enabled"] is True
    assert parsed["min_pp_amount"] == 200000
    assert parsed["max_per_hour"] == 7
    assert parsed["ktru_codes"] == ["A", "B", "C"]
    form = form_from_settings(parsed)
    assert form["enabled"] == "1"
    assert "A" in form["ktru_codes_text"]


def test_parse_settings_clamps_max_hour():
    parsed = parse_settings({"max_per_hour": 999, "enabled": 1})
    assert parsed["max_per_hour"] == 100

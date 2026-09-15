from __future__ import annotations

from pathlib import Path

import pytest

from auto_participate_settings import (
    apply_admin_global_enabled,
    count_enabled_profiles,
    effective_min_amount,
    global_enabled,
    load_settings,
    parse_form_profile_rules,
    parse_profile_rules,
    profile_rules,
    save_settings,
    settings_path,
)


def test_default_settings_have_both_profiles():
    data = load_settings(Path("/nonexistent/auto_participate.json"))
    assert "IPSokolova" in data["profiles"]
    assert "Nacpro" in data["profiles"]
    assert data["profiles"]["IPSokolova"]["enabled"] is False
    assert "use_notify_min_amount" not in data["profiles"]["IPSokolova"]


def test_save_and_load_roundtrip(tmp_path):
    path = settings_path(tmp_path)
    payload = load_settings(path)
    payload["profiles"]["IPSokolova"] = parse_profile_rules(
        {
            "enabled": True,
            "min_purchase_amount": 150000,
            "ktru_mode": "whitelist",
            "ktru_codes": ["08.12.12.140-00000005", "26.20.16.120-00000101"],
            "min_match_ratio": 0.5,
            "max_per_hour": 5,
        }
    )
    save_settings(path, payload)
    loaded = load_settings(path)
    rules = profile_rules(loaded, "IPSokolova")
    assert rules["enabled"] is True
    assert rules["min_purchase_amount"] == 150000
    assert rules["ktru_mode"] == "whitelist"
    assert rules["ktru_codes"] == ["08.12.12.140-00000005", "26.20.16.120-00000101"]
    assert rules["min_match_ratio"] == 0.5
    assert rules["max_per_hour"] == 5


def test_effective_min_amount_from_profile_rules():
    rules = parse_profile_rules({"min_purchase_amount": 250000})
    assert effective_min_amount(rules) == 250000


def test_parse_form_profile_rules():
    parsed = parse_form_profile_rules(
        {
            "enabled": "on",
            "min_purchase_amount": "200 000",
            "ktru_mode": "whitelist",
            "ktru_codes_text": "08.12.12.140-00000005\n26.20.16.120-00000101",
            "min_match_ratio": "75",
            "max_per_hour": "3",
        }
    )
    assert parsed["enabled"] is True
    assert parsed["min_purchase_amount"] == 200000
    assert parsed["ktru_mode"] == "whitelist"
    assert parsed["ktru_codes"] == ["08.12.12.140-00000005", "26.20.16.120-00000101"]
    assert parsed["min_match_ratio"] == 0.75
    assert parsed["max_per_hour"] == 3


def test_parse_form_requires_min_amount():
    with pytest.raises(ValueError, match="минимальную сумму"):
        parse_form_profile_rules({"enabled": "on", "min_purchase_amount": ""})


def test_global_enabled_defaults_false():
    data = load_settings(Path("/nonexistent/auto_participate.json"))
    assert global_enabled(data) is False
    assert count_enabled_profiles(data) == 0


def test_apply_admin_global_enabled_toggles_all_profiles(tmp_path):
    path = settings_path(tmp_path)
    save_settings(path, load_settings(path))
    existing = load_settings(path)
    enabled = apply_admin_global_enabled(existing, enabled=True)
    save_settings(path, enabled)
    loaded = load_settings(path)
    assert global_enabled(loaded) is True
    assert count_enabled_profiles(loaded) == len(loaded["profiles"])
    for name in loaded["profiles"]:
        assert profile_rules(loaded, name)["enabled"] is True

    disabled = apply_admin_global_enabled(loaded, enabled=False)
    save_settings(path, disabled)
    loaded2 = load_settings(path)
    assert global_enabled(loaded2) is False
    assert count_enabled_profiles(loaded2) == 0

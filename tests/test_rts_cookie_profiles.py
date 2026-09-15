from __future__ import annotations

from pathlib import Path

from rts_cookie_profiles import (
    cookie_path_for_profile,
    list_known_profiles,
    rts_profile_for_web_user,
    validate_rts_profile,
)


def test_list_known_profiles_default():
    assert list_known_profiles() == ("IPSokolova", "Nacpro")


def test_cookie_path_per_profile(tmp_path):
    meta = tmp_path / "meta"
    root = tmp_path / "app"
    p1 = cookie_path_for_profile("IPSokolova", meta, root)
    p2 = cookie_path_for_profile("Nacpro", meta, root)
    assert p1 != p2
    assert p1.name == "IPSokolova.txt"
    assert p2.name == "Nacpro.txt"


def test_rts_profile_for_web_user():
    assert rts_profile_for_web_user("IPSokolova") == "IPSokolova"
    assert rts_profile_for_web_user(None) == ""


def test_validate_rts_profile():
    assert validate_rts_profile("Nacpro") == "Nacpro"
    try:
        validate_rts_profile("")
    except ValueError as exc:
        assert "profile" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError")

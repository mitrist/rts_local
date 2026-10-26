"""Tests for resolve_offer_creation_data (star probe + 204 retry)."""

from __future__ import annotations

import pytest

from rts_pp_api import RtsPpApiError, resolve_offer_creation_data


def test_resolve_retries_204_then_ok(monkeypatch):
    rounds = {"n": 0}
    sleeps: list[float] = []

    def fake_fetch(cookie, pn, star, *, timeout=30.0):
        # First full probe round (lots 1..8) is empty; after backoff succeed on -1.
        if rounds["n"] == 0:
            if star.endswith("-8"):
                rounds["n"] = 1
            raise RtsPpApiError("empty", reason="creation_data_empty", detail={"star": star})
        return {"ktruCode": "22.22.10.000-00000005", "ktruName": "x"}

    monkeypatch.setattr("rts_pp_api.fetch_offer_creation_data", fake_fetch)
    data, star = resolve_offer_creation_data(
        "cookie",
        "0369200012126000008",
        expected_ktru="22.22.10.000-00000005",
        preferred_lot_index=1,
        empty_backoffs=(0.5, 1.0),
        sleep=sleeps.append,
    )
    assert data["ktruCode"] == "22.22.10.000-00000005"
    assert star == "l0369200012126000008-1"
    assert sleeps == [0.5]


def test_resolve_probes_second_lot_for_ktru(monkeypatch):
    def fake_fetch(cookie, pn, star, *, timeout=30.0):
        if star.endswith("-1"):
            return {"ktruCode": "11.11.11.111-00000001"}
        if star.endswith("-2"):
            return {"ktruCode": "22.22.10.000-00000005"}
        raise RtsPpApiError("empty", reason="creation_data_empty")

    monkeypatch.setattr("rts_pp_api.fetch_offer_creation_data", fake_fetch)
    data, star = resolve_offer_creation_data(
        "cookie",
        "0369200012126000008",
        expected_ktru="22.22.10.000-00000005",
        preferred_lot_index=1,
        empty_backoffs=(),
        sleep=lambda _s: None,
    )
    assert star == "l0369200012126000008-2"
    assert data["ktruCode"] == "22.22.10.000-00000005"


def test_resolve_exhausts_empty(monkeypatch):
    monkeypatch.setattr(
        "rts_pp_api.fetch_offer_creation_data",
        lambda *a, **k: (_ for _ in ()).throw(
            RtsPpApiError("empty", reason="creation_data_empty")
        ),
    )
    with pytest.raises(RtsPpApiError) as ei:
        resolve_offer_creation_data(
            "cookie",
            "0369200012126000008",
            preferred_lot_index=1,
            max_lot_index=2,
            empty_backoffs=(0.1,),
            sleep=lambda _s: None,
        )
    assert ei.value.reason == "creation_data_empty"
    assert "после retry" in str(ei.value)


def test_resolve_allowed_ktru_set(monkeypatch):
    def fake_fetch(cookie, pn, star, *, timeout=30.0):
        if star.endswith("-1"):
            return {"ktruCode": "AA"}
        return {"ktruCode": "BB"}

    monkeypatch.setattr("rts_pp_api.fetch_offer_creation_data", fake_fetch)
    data, star = resolve_offer_creation_data(
        "cookie",
        "PN1",
        allowed_ktru={"BB"},
        empty_backoffs=(),
        sleep=lambda _s: None,
    )
    assert data["ktruCode"] == "BB"
    assert star == "lPN1-2"

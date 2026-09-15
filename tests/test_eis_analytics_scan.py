from __future__ import annotations

from datetime import datetime, timezone

import pytest

import eis_analytics_scan
from eis_analytics_scan import EisAnalyticsScanConfig, parse_region_csv, read_last_scan_report, run_eis_analytics_scan
from eis_ezt_poll import EisPollResult


def test_parse_region_csv_trims_and_deduplicates():
    assert parse_region_csv("35, 72,35,,77") == ["35", "72", "77"]


def test_parse_region_csv_rejects_empty():
    with pytest.raises(ValueError):
        parse_region_csv(" , ")


def test_run_eis_analytics_scan_aggregates_regions(monkeypatch, tmp_path):
    calls: list[tuple[str, str, str]] = []

    def fake_run(cfg):
        calls.append((cfg.org_region, cfg.detail_json_name, cfg.changelog_filename))
        return EisPollResult(
            ok=cfg.org_region != "77",
            errors=[] if cfg.org_region != "77" else ["timeout"],
            zips_downloaded=2,
            new_matches=[],
            state_seen_count=0,
            meta={
                "found_filtered": 3,
                "new_filtered": 1,
                "customer_characteristics_count": 4,
                "analytics_run_id": 10,
            },
        )

    monkeypatch.setattr(eis_analytics_scan, "run_eis_poll", fake_run)
    cfg = EisAnalyticsScanConfig(
        env_file=tmp_path / "env.txt",
        meta_parent=tmp_path,
        regions=["35", "77"],
        exact_date="2026-05-19",
    )

    res = run_eis_analytics_scan(cfg)

    assert [x[0] for x in calls] == ["35", "77"]
    assert all(x[1].startswith("last_eis_analytics_scan_region_") for x in calls)
    assert all(x[2] == "eis_analytics_scan_changelog.json" for x in calls)
    assert not res.ok
    assert res.regions_ok == 1
    assert res.regions_failed == 1
    assert res.zips_downloaded == 4
    assert res.found_filtered == 6
    assert res.customer_characteristics_count == 8
    report = read_last_scan_report(tmp_path)
    assert report is not None
    assert report["regions"] == ["35", "77"]
    assert report["region_results"][1]["errors"] == ["timeout"]


def test_scan_report_timestamps_are_utc(monkeypatch, tmp_path):
    monkeypatch.setattr(
        eis_analytics_scan,
        "run_eis_poll",
        lambda cfg: EisPollResult(ok=True, errors=[], zips_downloaded=0, meta={}),
    )
    res = run_eis_analytics_scan(
        EisAnalyticsScanConfig(env_file=tmp_path / "env.txt", meta_parent=tmp_path, regions=["35"])
    )
    dt = datetime.fromisoformat(res.finished_at_utc.replace("Z", "+00:00"))
    assert dt.tzinfo == timezone.utc

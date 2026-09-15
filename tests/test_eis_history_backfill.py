from __future__ import annotations

from datetime import date, timedelta

from eis_ezt_poll import EisPollResult
from eis_history_config import HISTORY_DOCUMENT_TYPE_44, history_document_types
from eis_history_backfill import HistoryBackfillConfig, effective_date_to, run_history_backfill
from eis_org_regions import all_eis_org_regions, resolve_org_regions
from eis_history_db import create_backfill_job, get_checkpoint, history_db_path, save_checkpoint


def test_history_document_types_default_ezt_only():
    assert history_document_types(None) == [HISTORY_DOCUMENT_TYPE_44]
    assert history_document_types("") == ["epNotificationEZT2020"]


def test_effective_date_to_respects_lag():
    today = date(2026, 5, 29)
    cap = today - timedelta(days=90)
    assert effective_date_to(date(2026, 1, 15), lag_days=90, today=today) == date(2026, 1, 15)
    assert effective_date_to(date(2026, 5, 29), lag_days=90, today=today) == cap


def test_resolve_org_regions_default_all():
    regions = resolve_org_regions(None)
    assert len(regions) == len(all_eis_org_regions())
    assert "72" in regions


def test_resolve_org_regions_csv_subset():
    assert resolve_org_regions("72, 77,72") == ["72", "77"]


def test_run_backfill_skips_checkpoint_ok(monkeypatch, tmp_path):
    db = history_db_path(tmp_path / "eis_history.sqlite3")
    job_id = create_backfill_job(
        db,
        org_regions=["72"],
        ktru_codes=["31.01.12.160-00000005"],
        document_type_44="epNotificationEZT2020",
        document_types=["epNotificationEZT2020"],
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 2),
        date_to_max=date(2026, 1, 2),
        lag_days=0,
        require_ezt_xml=False,
        progress_total=2,
    )
    save_checkpoint(db, job_id, "72", "2026-01-01", status="ok", zips=1, matches=1, lines=1)

    calls: list[str] = []

    def fake_poll(cfg):
        calls.append(cfg.exact_date or "")
        return EisPollResult(
            ok=True,
            errors=[],
            zips_downloaded=1,
            meta={"found_filtered": 1, "lines_in_run": 1},
        )

    monkeypatch.setattr("eis_history_backfill.run_eis_poll", fake_poll)
    monkeypatch.setattr("eis_history_backfill.time.sleep", lambda _x: None)

    cfg = HistoryBackfillConfig(
        env_file=tmp_path / "env.txt",
        db_path=db,
        state_file=tmp_path / "eis_history_state.json",
        meta_parent=tmp_path,
        org_regions=["72"],
        document_types=["epNotificationEZT2020"],
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 2),
        lag_days=0,
        day_delay_sec=0,
        resume_job_id=job_id,
    )
    (tmp_path / "env.txt").write_text("eruz_token=00000000-0000-0000-0000-000000000001\n", encoding="utf-8")

    code = run_history_backfill(cfg)
    assert code == 0
    assert len(calls) == 1
    assert get_checkpoint(db, job_id, "72", "2026-01-01")["status"] == "ok"


def test_run_backfill_empty_period_returns_2(tmp_path):
    today = date.today()
    cfg = HistoryBackfillConfig(
        env_file=tmp_path / "env.txt",
        db_path=tmp_path / "eis_history.sqlite3",
        state_file=tmp_path / "state.json",
        meta_parent=tmp_path,
        org_regions=["72"],
        document_types=["epNotificationEF2020"],
        date_from=today,
        date_to=today + timedelta(days=1),
        lag_days=3650,
    )
    assert run_history_backfill(cfg) == 2

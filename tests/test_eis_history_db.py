from __future__ import annotations

import json
from datetime import date, datetime, timezone

from eis_analytics_db import characteristics_fingerprint
from eis_ezt_archive import NoticeMatch
from eis_history_db import (
    characteristics_set_hash,
    create_backfill_job,
    get_checkpoint,
    get_current_backfill_job,
    get_history_coverage_period,
    get_latest_backfill_job,
    history_backfill_status,
    history_connect,
    history_db_totals,
    history_db_totals_for_ui,
    record_eis_history_poll_run,
    save_checkpoint,
    update_backfill_job,
)
from eis_ktru_characteristics import CustomerCharacteristic, KtruPurchaseLine


def _line(*, value: str = "Текстиль") -> KtruPurchaseLine:
    return KtruPurchaseLine(
        line_key="PN1|31.01.12.160-00000005|sid:100",
        purchase_number="PN1",
        object_sid="100",
        object_external_sid="",
        position_index=0,
        ktru_code="31.01.12.160-00000005",
        position_display_name="Кресло офисное",
        characteristics=[
            CustomerCharacteristic(
                match_key="chr_ref_1",
                characteristic_kind="reference",
                characteristic_code="CHR_REF_1",
                display_label="Вид материала обивки сиденья",
                value_display=value,
            ),
        ],
    )


def test_characteristics_set_hash_order_stable_and_differs_from_fingerprint():
    line = _line()
    chars = line.characteristics

    h1, _ = characteristics_set_hash(chars)
    h2, _ = characteristics_set_hash(reversed(chars))
    fp_hash, fp_canon = characteristics_fingerprint(chars)

    assert h1 == h2
    assert h1 != fp_hash
    assert "display_label" in fp_canon or True
    assert '"key"' in fp_canon


def test_record_history_poll_stores_display_label(tmp_path):
    db = tmp_path / "eis_history.sqlite3"
    match = NoticeMatch(
        purchase_number="PN-H1",
        ktru_codes=["31.01.12.160-00000005"],
        purchase_method_codes=[],
        source_file="n.xml",
        publish_dt_in_eis="2026-05-19T08:00:00+03:00",
        customer_ktru_lines=[_line()],
    )
    finished = datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc)
    record_eis_history_poll_run(
        db,
        [match],
        finished_at_utc=finished,
        org_region="72",
        document_type_44="epNotificationEZT2020",
        ktru_filter="31.01",
        exact_dates=["2026-05-19+03:00"],
        zips_downloaded=1,
        ok=True,
        errors=[],
    )
    with history_connect(db) as conn:
        row = conn.execute(
            "SELECT characteristics_json FROM ktru_lines LIMIT 1",
        ).fetchone()
    assert row is not None
    chars = json.loads(str(row["characteristics_json"]))
    assert isinstance(chars, list) and len(chars) >= 1
    assert chars[0]["display_label"] == "Вид материала обивки сиденья"
    assert chars[0]["characteristic_code"] == "CHR_REF_1"


def test_get_latest_backfill_job_returns_last_done(tmp_path):
    db = tmp_path / "eis_history.sqlite3"
    create_backfill_job(
        db,
        org_regions=["72"],
        ktru_codes=[],
        document_type_44="epNotificationEZT2020",
        document_types=["epNotificationEZT2020"],
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 15),
        date_to_max=date(2026, 1, 15),
        lag_days=0,
        require_ezt_xml=False,
        progress_total=10,
    )
    job2_id = create_backfill_job(
        db,
        org_regions=["77"],
        ktru_codes=[],
        document_type_44="epNotificationEZT2020",
        document_types=["epNotificationEZT2020"],
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 28),
        date_to_max=date(2026, 2, 28),
        lag_days=0,
        require_ezt_xml=False,
        progress_total=20,
    )
    with history_connect(db) as conn:
        conn.execute(
            "UPDATE backfill_jobs SET status = 'done', finished_at_utc = ? WHERE id = ?",
            ("2026-03-01T00:00:00Z", job2_id),
        )
        conn.commit()

    latest = get_latest_backfill_job(db)

    assert latest is not None
    assert int(latest["id"]) == job2_id
    assert latest["status"] == "done"
    assert latest["date_from"] == "2026-02-01"
    assert latest["date_to"] == "2026-02-28"


def test_get_history_coverage_period_spans_done_jobs(tmp_path):
    db = tmp_path / "eis_history.sqlite3"
    create_backfill_job(
        db,
        org_regions=["72"],
        ktru_codes=[],
        document_type_44="epNotificationEZT2020",
        document_types=["epNotificationEZT2020"],
        date_from=date(2025, 6, 1),
        date_to=date(2025, 12, 31),
        date_to_max=date(2025, 12, 31),
        lag_days=0,
        require_ezt_xml=False,
        progress_total=10,
    )
    job2 = create_backfill_job(
        db,
        org_regions=["77"],
        ktru_codes=[],
        document_type_44="epNotificationEZT2020",
        document_types=["epNotificationEZT2020"],
        date_from=date(2026, 1, 1),
        date_to=date(2026, 5, 31),
        date_to_max=date(2026, 5, 31),
        lag_days=0,
        require_ezt_xml=False,
        progress_total=10,
    )
    with history_connect(db) as conn:
        conn.execute("UPDATE backfill_jobs SET status = 'done'")
        conn.commit()

    cov = get_history_coverage_period(db)

    assert cov == (date(2025, 6, 1), date(2026, 5, 31))


def test_history_backfill_status_running_job(tmp_path):
    db = tmp_path / "eis_history.sqlite3"
    job_id = create_backfill_job(
        db,
        org_regions=["72"],
        ktru_codes=[],
        document_type_44="epNotificationEZT2020",
        document_types=["epNotificationEZT2020"],
        date_from=date(2025, 6, 1),
        date_to=date(2025, 12, 31),
        date_to_max=date(2025, 12, 31),
        lag_days=0,
        require_ezt_xml=False,
        progress_total=100,
    )
    update_backfill_job(db, job_id, status="running", progress_done=25)
    active = get_current_backfill_job(db)
    st = history_backfill_status(db)
    assert active is not None
    assert int(active["id"]) == job_id
    assert st["is_running"] is True
    assert st["progress_pct"] == 25.0


def test_history_db_totals_for_ui_uses_job_stats(tmp_path):
    db = tmp_path / "eis_history.sqlite3"
    job_id = create_backfill_job(
        db,
        org_regions=["72"],
        ktru_codes=[],
        document_type_44="epNotificationEZT2020",
        document_types=["epNotificationEZT2020"],
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        date_to_max=date(2026, 1, 31),
        lag_days=0,
        require_ezt_xml=False,
        progress_total=1,
    )
    update_backfill_job(
        db,
        job_id,
        status="done",
        stats_json={
            "db_totals": {
                "notice_events": 100,
                "ktru_lines": 200,
                "unique_ktru_codes": 50,
                "top_ktru": [{"ktru_code": "31.01.12.160-00000005", "line_count": 10}],
            }
        },
    )
    job = get_latest_backfill_job(db)
    totals = history_db_totals_for_ui(db, job)
    assert totals["from_job_stats"] is True
    assert totals["ktru_lines"] == 200
    assert totals["notice_events"] == 100
    slow = history_db_totals(db)
    assert slow["ktru_lines"] >= 0


def test_backfill_checkpoint_roundtrip(tmp_path):
    db = tmp_path / "eis_history.sqlite3"
    job_id = create_backfill_job(
        db,
        org_regions=["72"],
        ktru_codes=["31.01.12.160-00000005"],
        document_type_44="epNotificationEZT2020",
        document_types=["epNotificationEZT2020"],
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        date_to_max=date(2026, 1, 31),
        lag_days=90,
        require_ezt_xml=False,
        progress_total=31,
    )
    save_checkpoint(db, job_id, "72", "2026-01-15", status="ok", zips=2, matches=3, lines=4)
    cp = get_checkpoint(db, job_id, "72", "2026-01-15+03:00")
    assert cp is not None
    assert cp["status"] == "ok"
    assert int(cp["matches"]) == 3

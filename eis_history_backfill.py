"""Local CLI: historical EIS XML backfill into eis_history.sqlite3."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eis_ezt_poll import (
    EisPollConfig,
    build_exact_dates_list,
    daterange_inclusive,
    parse_date_iso,
    run_eis_poll,
)
from eis_history_config import (
    HISTORY_PERIOD_FROM,
    HISTORY_PERIOD_TO,
    history_document_types,
)
from eis_org_regions import resolve_org_regions
from eis_history_db import (
    HISTORY_DB_DEFAULT,
    checkpoint_summary_by_region,
    create_backfill_job,
    history_db_totals,
    exact_date_key,
    get_backfill_job,
    get_checkpoint,
    history_db_path,
    job_cancel_requested,
    label_quality_stats,
    save_checkpoint,
    update_backfill_job,
)

STATE_FILENAME = "eis_history_state.json"
DEFAULT_LAG_DAYS = 0
DEFAULT_DAY_DELAY_SEC = 1.0


@dataclass
class HistoryBackfillConfig:
    env_file: Path
    db_path: Path
    state_file: Path
    meta_parent: Path
    org_regions: list[str]
    date_from: date
    date_to: date
    lag_days: int
    document_types: list[str]
    require_ezt_xml: bool = False
    timeout: float = 120.0
    poll_interval: float = 5.0
    max_prepared_polls: int = 60
    date_suffix_timezone: str = "+03:00"
    day_delay_sec: float = DEFAULT_DAY_DELAY_SEC
    no_verify: bool = False
    ssl_legacy: bool = False
    tls_max_12: bool = False
    legacy_int44: bool = False
    service_url: str | None = None
    save_zips_dir: Path | None = None
    print_raw_on_error: bool = False
    resume_job_id: int | None = None


def effective_date_to(user_to: date, *, lag_days: int, today: date | None = None) -> date:
    ref = today or datetime.now().astimezone().date()
    cap = ref - timedelta(days=max(0, int(lag_days)))
    return min(user_to, cap)


def _job_list_json(job: dict[str, Any], key: str) -> list[str]:
    raw = job.get(key)
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    try:
        data = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(x).strip() for x in data if str(x).strip()] if isinstance(data, list) else []


def run_history_backfill(cfg: HistoryBackfillConfig) -> int:
    db = cfg.db_path.resolve()
    job_id = cfg.resume_job_id

    saved_stats: dict[str, Any] = {}
    if job_id is not None:
        job = get_backfill_job(db, job_id)
        if not job:
            print(f"Job #{job_id} не найден.", file=sys.stderr)
            return 2
        cfg.org_regions = _job_list_json(job, "org_regions_json")
        types_json = _job_list_json(job, "document_types_json")
        if types_json:
            cfg.document_types = types_json
        elif job.get("document_type_44"):
            cfg.document_types = [str(job["document_type_44"])]
        cfg.date_from = date.fromisoformat(str(job["date_from"]))
        cfg.date_to = date.fromisoformat(str(job["date_to"]))
        cfg.lag_days = int(job.get("lag_days") or cfg.lag_days)
        cfg.require_ezt_xml = bool(int(job.get("require_ezt_xml") or 0))
        progress_done = int(job.get("progress_done") or 0)
        try:
            loaded = json.loads(str(job.get("stats_json") or "{}"))
            saved_stats = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            saved_stats = {}
        print(f"Продолжение job #{job_id} (progress {progress_done})")
    else:
        progress_done = 0
        if not cfg.document_types:
            cfg.document_types = history_document_types(None)

    eff_to = effective_date_to(cfg.date_to, lag_days=cfg.lag_days)
    if eff_to < cfg.date_from:
        print(
            f"Ошибка: период пуст после cutoff (date_from={cfg.date_from}, "
            f"effective_date_to={eff_to}, lag_days={cfg.lag_days}).",
            file=sys.stderr,
        )
        return 2

    dates = daterange_inclusive(cfg.date_from, eff_to)
    progress_total = len(cfg.org_regions) * len(dates)

    if job_id is None:
        job_id = create_backfill_job(
            db,
            org_regions=cfg.org_regions,
            ktru_codes=[],
            document_type_44=",".join(cfg.document_types),
            document_types=cfg.document_types,
            date_from=cfg.date_from,
            date_to=eff_to,
            date_to_max=eff_to,
            lag_days=cfg.lag_days,
            require_ezt_xml=cfg.require_ezt_xml,
            progress_total=progress_total,
        )
        progress_done = 0
        print(
            f"Создан job #{job_id}, период {cfg.date_from}..{eff_to}, "
            f"регионов: {len(cfg.org_regions)}, ячеек: {progress_total}, "
            f"типы документов: {', '.join(cfg.document_types)}"
        )

    update_backfill_job(
        db,
        job_id,
        status="running",
        started_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )

    stats: dict[str, Any] = saved_stats or {
        "zips_total": 0,
        "matches_total": 0,
        "lines_total": 0,
        "cells_ok": 0,
        "cells_no_data": 0,
        "cells_error": 0,
        "cells_skipped": 0,
    }
    errors: list[str] = []
    final_status = "done"

    try:
        for region in cfg.org_regions:
            for day in dates:
                if job_cancel_requested(db, job_id):
                    final_status = "cancelled"
                    print("Отмена по запросу.")
                    break

                exact_list = build_exact_dates_list(
                    None,
                    day,
                    day,
                    cfg.date_suffix_timezone,
                )
                exact_one = exact_list[0] if exact_list else day.isoformat()
                day_key = exact_date_key(exact_one)

                cp = get_checkpoint(db, job_id, region, day_key)
                if cp and str(cp.get("status")) == "ok" and int(cp.get("zips") or 0) > 0:
                    progress_done += 1
                    stats["cells_skipped"] = int(stats.get("cells_skipped", 0)) + 1
                    update_backfill_job(db, job_id, progress_done=progress_done)
                    print(f"[skip] {region} {day_key} (checkpoint ok, zips={cp.get('zips')})")
                    continue

                print(
                    f"[run] job={job_id} region={region} date={day_key} "
                    f"({progress_done + 1}/{progress_total})"
                )

                zips = 0
                found = 0
                lines_n = 0
                cell_errors: list[str] = []

                for doc_type in cfg.document_types:
                    poll_cfg = EisPollConfig(
                        env_file=cfg.env_file,
                        state_file=cfg.state_file,
                        meta_parent=cfg.meta_parent,
                        org_region=region,
                        detail_json_name=f"history_job_{job_id}_{region}_{day_key}_{doc_type}.json",
                        document_type_44=doc_type,
                        ktru_csv=None,
                        ktru_file=None,
                        exact_date=exact_one,
                        date_from=None,
                        date_to=None,
                        date_suffix_timezone=cfg.date_suffix_timezone,
                        timeout=cfg.timeout,
                        poll_interval=cfg.poll_interval,
                        max_prepared_polls=cfg.max_prepared_polls,
                        require_ezt_xml=cfg.require_ezt_xml,
                        dry_run=False,
                        notify_telegram=False,
                        env_telegram_ktru_digest=False,
                        no_verify=cfg.no_verify,
                        ssl_legacy=cfg.ssl_legacy,
                        tls_max_12=cfg.tls_max_12,
                        legacy_int44=cfg.legacy_int44,
                        service_url=cfg.service_url,
                        print_raw_on_error=cfg.print_raw_on_error,
                        save_zips_dir=cfg.save_zips_dir,
                        changelog_filename="eis_history_changelog.json",
                        analytics_db_name=db.name,
                        history_db_path=db,
                    )
                    res = run_eis_poll(poll_cfg)
                    for line in res.log_lines:
                        print(line)
                    if res.errors:
                        cell_errors.extend(res.errors)
                    meta = res.meta if isinstance(res.meta, dict) else {}
                    found += int(meta.get("found_filtered") or 0)
                    zips += int(res.zips_downloaded or 0)
                    lines_n += int(meta.get("lines_in_run") or 0)

                if cell_errors:
                    cell_status = "error"
                    err_text = "; ".join(cell_errors)[:2000]
                    errors.extend([f"{region}/{day_key}: {e}" for e in cell_errors[:3]])
                    stats["cells_error"] = int(stats.get("cells_error", 0)) + 1
                elif zips == 0 and found == 0:
                    cell_status = "no_data"
                    err_text = None
                    stats["cells_no_data"] = int(stats.get("cells_no_data", 0)) + 1
                else:
                    cell_status = "ok"
                    err_text = None
                    stats["cells_ok"] = int(stats.get("cells_ok", 0)) + 1

                stats["zips_total"] = int(stats.get("zips_total", 0)) + zips
                stats["matches_total"] = int(stats.get("matches_total", 0)) + found
                stats["lines_total"] = int(stats.get("lines_total", 0)) + lines_n

                save_checkpoint(
                    db,
                    job_id,
                    region,
                    day_key,
                    status=cell_status,
                    zips=zips,
                    matches=found,
                    lines=lines_n,
                    error_text=err_text,
                )

                progress_done += 1
                update_backfill_job(
                    db,
                    job_id,
                    progress_done=progress_done,
                    stats_json=stats,
                    last_error=errors[-1] if errors else None,
                )

                if cfg.day_delay_sec > 0:
                    time.sleep(cfg.day_delay_sec)

            if final_status == "cancelled":
                break
    except Exception as exc:  # noqa: BLE001
        final_status = "failed"
        errors.append(str(exc))
        update_backfill_job(db, job_id, last_error=str(exc))
        raise

    stats["db_totals"] = history_db_totals(db)
    stats["label_quality"] = label_quality_stats(db)

    update_backfill_job(
        db,
        job_id,
        status=final_status,
        finished_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        progress_done=progress_done,
        stats_json=stats,
        last_error=errors[-1] if errors and final_status != "done" else None,
    )

    print(f"\nJob #{job_id} завершён: {final_status}")
    print(f"  прогресс: {progress_done}/{progress_total}")
    print(f"  архивов: {stats.get('zips_total')}, совпадений: {stats.get('matches_total')}")
    print(f"  строк КТРУ: {stats.get('lines_total')}")
    db_tot = stats.get("db_totals") if isinstance(stats.get("db_totals"), dict) else {}
    if db_tot:
        print(
            f"  в БД: закупок {db_tot.get('notice_events')}, "
            f"строк КТРУ {db_tot.get('ktru_lines')}, "
            f"уникальных КТРУ {db_tot.get('unique_ktru_codes')}"
        )
        top = db_tot.get("top_ktru")
        if isinstance(top, list) and top:
            print("  топ КТРУ:", ", ".join(f"{x['ktru_code']}({x['line_count']})" for x in top[:5]))
    by_reg = checkpoint_summary_by_region(db, job_id)
    if by_reg:
        print("  checkpoint по регионам (первые 10):")
        for row in by_reg[:10]:
            print(
                f"    {row['org_region']}: zips={row.get('zips')}, "
                f"ok={row.get('cells_ok')}, no_data={row.get('cells_no_data')}, "
                f"cells={row.get('cells')}"
            )
    if stats.get("label_quality"):
        lq = stats["label_quality"]
        print(
            f"  качество имён: weak={lq.get('weak_label_equals_code')}/"
            f"{lq.get('characteristics_total')} (label==code)"
        )
    if errors:
        print("  ошибки (последние):", "; ".join(errors[:5]), file=sys.stderr)

    return 0 if final_status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(
        description="Исторический backfill ЕИС (XML) в локальную eis_history.sqlite3.",
    )
    p.add_argument("--env-file", type=Path, default=root / "env.txt")
    p.add_argument("--db", type=Path, default=None, help=f"SQLite (default {HISTORY_DB_DEFAULT})")
    p.add_argument(
        "--regions",
        "--org-region",
        dest="regions",
        default=None,
        help="Ограничить регионами (CSV); по умолчанию — все субъекты РФ",
    )
    p.add_argument(
        "--date-from",
        type=parse_date_iso,
        default=HISTORY_PERIOD_FROM,
        help=f"Начало периода (по умолчанию {HISTORY_PERIOD_FROM.isoformat()})",
    )
    p.add_argument(
        "--date-to",
        type=parse_date_iso,
        default=HISTORY_PERIOD_TO,
        help=f"Конец периода (по умолчанию {HISTORY_PERIOD_TO.isoformat()})",
    )
    p.add_argument(
        "--lag-days",
        type=int,
        default=int(os.environ.get("EIS_HISTORY_LAG_DAYS", str(DEFAULT_LAG_DAYS))),
        help="Обрезать date_to до today-lag (0 = не обрезать закреплённый период)",
    )
    p.add_argument(
        "--document-types44",
        default=None,
        help="CSV типов documentType44; по умолчанию EF+EOK+EZK+EZT",
    )
    p.add_argument("--require-ezt-xml", action="store_true")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--poll-interval", type=float, default=5.0)
    p.add_argument("--max-prepared-polls", type=int, default=60)
    p.add_argument("--date-suffix-timezone", default="+03:00")
    p.add_argument(
        "--day-delay",
        type=float,
        default=float(os.environ.get("EIS_HISTORY_DAY_DELAY_SEC", str(DEFAULT_DAY_DELAY_SEC))),
    )
    p.add_argument("--resume-job", type=int, default=None, metavar="ID")
    p.add_argument("--save-zips-dir", type=Path, default=None)
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--ssl-legacy", action="store_true")
    p.add_argument("--tls-max-12", action="store_true", dest="tls_max_12")
    p.add_argument("--legacy-int44", action="store_true")
    p.add_argument("--service-url", default=os.environ.get("EIS_GET_DOCS_IP_URL", "").strip() or None)
    p.add_argument("--print-raw-on-error", action="store_true")
    p.add_argument("--cancel-job", type=int, default=None, metavar="ID", help="Запросить отмену job")
    args = p.parse_args(argv)

    db = history_db_path(args.db or os.environ.get("EIS_HISTORY_DB", HISTORY_DB_DEFAULT))

    if args.cancel_job is not None:
        update_backfill_job(db, args.cancel_job, cancel_requested=True)
        print(f"Job #{args.cancel_job}: cancel_requested=1")
        return 0

    doc_types = history_document_types(args.document_types44)
    if args.resume_job is None:
        try:
            regions = resolve_org_regions(args.regions)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        if not args.regions:
            print(f"Регионы: все субъекты РФ ({len(regions)} кодов orgRegion)")
        print(f"Период: {args.date_from} .. {args.date_to}, lag_days={args.lag_days}")
        print(f"Типы документов: {', '.join(doc_types)}")
    else:
        regions = []
        doc_types = []

    cfg = HistoryBackfillConfig(
        env_file=args.env_file,
        db_path=db,
        state_file=root / STATE_FILENAME,
        meta_parent=root,
        org_regions=regions,
        date_from=args.date_from,
        date_to=args.date_to,
        lag_days=args.lag_days,
        document_types=doc_types,
        require_ezt_xml=args.require_ezt_xml,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        max_prepared_polls=args.max_prepared_polls,
        date_suffix_timezone=args.date_suffix_timezone,
        day_delay_sec=args.day_delay,
        no_verify=args.no_verify,
        ssl_legacy=args.ssl_legacy,
        tls_max_12=args.tls_max_12,
        legacy_int44=args.legacy_int44,
        service_url=args.service_url,
        save_zips_dir=args.save_zips_dir,
        print_raw_on_error=args.print_raw_on_error,
        resume_job_id=args.resume_job,
    )
    return run_history_backfill(cfg)


if __name__ == "__main__":
    raise SystemExit(main())

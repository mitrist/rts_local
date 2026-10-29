"""Schedule and job status for the weekly competitor-offer monitor."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rts_pp_monitor_settings import parse_ktru_codes

SETTINGS_VERSION = 1
MSK = timezone(timedelta(hours=3))
STALE_JOB_SEC = 6 * 3600
SEED_KTRU_CODES = (
    "31.01.11.150-00000003",
    "31.01.12.139-00000001",
    "31.01.11.150-00000021",
    "31.01.12.122-00000004",
)
WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

_LOCK = threading.Lock()
_JOB_STATUSES = frozenset({"idle", "running", "ok", "error"})


def settings_path(meta_dir: Path) -> Path:
    return meta_dir / "competitor_monitor.json"


def default_settings() -> dict[str, Any]:
    return {
        "version": SETTINGS_VERSION,
        "enabled": True,
        "interval_days": 14,
        "weekday": 0,
        "hour_msk": 6,
        "ktru_codes": list(SEED_KTRU_CODES),
        "next_run_at_utc": "",
        "job_status": "idle",
        "job_progress": "",
        "job_error": "",
        "last_started_at_utc": "",
        "last_finished_at_utc": "",
        "last_trigger": "",
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    return _iso(utc_now())


def parse_utc(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def format_msk(value: str) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return ""
    return parsed.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clamp_int(raw: object, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


def clean_ktru_codes(raw: object) -> list[str]:
    if isinstance(raw, str):
        source = [part.strip() for part in raw.replace(",", "\n").splitlines() if part.strip()]
    elif isinstance(raw, list):
        source = list(raw)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in source:
        code = str(item or "").strip()
        if not code or code in seen:
            continue
        try:
            parsed = parse_ktru_codes([code])
        except ValueError:
            continue
        if parsed and parsed[0] not in seen:
            seen.add(parsed[0])
            out.append(parsed[0])
    return out


def compute_next_run(settings: dict[str, Any], *, now: datetime | None = None) -> datetime:
    """Soonest future weekday/hour in MSK. The 14-day step is applied after a full poll."""
    moment = now or utc_now()
    local = moment.astimezone(MSK)
    weekday = _clamp_int(settings.get("weekday"), 0, 0, 6)
    hour = _clamp_int(settings.get("hour_msk"), 6, 0, 23)
    days_ahead = (weekday - local.weekday()) % 7
    candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc).replace(microsecond=0)


def advance_next_run(settings: dict[str, Any], *, now: datetime | None = None) -> datetime:
    moment = now or utc_now()
    base = parse_utc(str(settings.get("next_run_at_utc") or "")) or moment
    step_days = 14 if _clamp_int(settings.get("interval_days"), 14, 7, 14) == 14 else 7
    nxt = base + timedelta(days=step_days)
    while nxt <= moment:
        nxt += timedelta(days=step_days)
    return nxt


def schedule_due(settings: dict[str, Any], *, now: datetime | None = None) -> bool:
    if not settings.get("enabled"):
        return False
    if not settings.get("ktru_codes"):
        return False
    if str(settings.get("job_status") or "") == "running":
        return False
    nxt = parse_utc(str(settings.get("next_run_at_utc") or ""))
    if nxt is None:
        return False
    return (now or utc_now()) >= nxt


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    base = default_settings()
    interval = _clamp_int(raw.get("interval_days"), 14, 7, 14)
    if interval != 14:
        interval = 7
    status = str(raw.get("job_status") or "idle").strip().lower()
    if status not in _JOB_STATUSES:
        status = "idle"
    return {
        "version": SETTINGS_VERSION,
        "enabled": bool(raw.get("enabled", base["enabled"])),
        "interval_days": interval,
        "weekday": _clamp_int(raw.get("weekday"), 0, 0, 6),
        "hour_msk": _clamp_int(raw.get("hour_msk"), 6, 0, 23),
        "ktru_codes": clean_ktru_codes(raw.get("ktru_codes", base["ktru_codes"])),
        "next_run_at_utc": str(raw.get("next_run_at_utc") or "").strip(),
        "job_status": status,
        "job_progress": str(raw.get("job_progress") or "").strip(),
        "job_error": str(raw.get("job_error") or "").strip(),
        "last_started_at_utc": str(raw.get("last_started_at_utc") or "").strip(),
        "last_finished_at_utc": str(raw.get("last_finished_at_utc") or "").strip(),
        "last_trigger": str(raw.get("last_trigger") or "").strip(),
    }


def _read_unlocked(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return default_settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_settings()
    if not isinstance(data, dict):
        return default_settings()
    return _normalize(data)


def _write_unlocked(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = _normalize(payload)
    out["saved_at_utc"] = utc_now_iso()
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_settings(path: Path) -> dict[str, Any]:
    with _LOCK:
        data = _read_unlocked(path)
        if not data.get("next_run_at_utc"):
            data["next_run_at_utc"] = _iso(compute_next_run(data))
            _write_unlocked(path, data)
        return data


def save_schedule(
    path: Path,
    *,
    enabled: bool,
    interval_days: int,
    weekday: int,
    hour_msk: int,
) -> dict[str, Any]:
    with _LOCK:
        data = _read_unlocked(path)
        data["enabled"] = bool(enabled)
        data["interval_days"] = 14 if int(interval_days) == 14 else 7
        data["weekday"] = _clamp_int(weekday, 0, 0, 6)
        data["hour_msk"] = _clamp_int(hour_msk, 6, 0, 23)
        data["next_run_at_utc"] = _iso(compute_next_run(data))
        _write_unlocked(path, data)
        return data


def add_ktru_code(path: Path, code: str) -> dict[str, Any]:
    parsed = parse_ktru_codes([code])
    with _LOCK:
        data = _read_unlocked(path)
        if parsed[0] not in data["ktru_codes"]:
            data["ktru_codes"].append(parsed[0])
        if not data.get("next_run_at_utc"):
            data["next_run_at_utc"] = _iso(compute_next_run(data))
        _write_unlocked(path, data)
        return data


def remove_ktru_code(path: Path, code: str) -> dict[str, Any]:
    target = str(code or "").strip()
    with _LOCK:
        data = _read_unlocked(path)
        data["ktru_codes"] = [item for item in data["ktru_codes"] if item != target]
        if not data.get("next_run_at_utc"):
            data["next_run_at_utc"] = _iso(compute_next_run(data))
        _write_unlocked(path, data)
        return data


def update_job(path: Path, **changes: Any) -> dict[str, Any]:
    allowed = {
        "job_status",
        "job_progress",
        "job_error",
        "last_started_at_utc",
        "last_finished_at_utc",
        "last_trigger",
        "next_run_at_utc",
    }
    with _LOCK:
        data = _read_unlocked(path)
        for key, value in changes.items():
            if key in allowed:
                data[key] = value
        if not data.get("next_run_at_utc"):
            data["next_run_at_utc"] = _iso(compute_next_run(data))
        _write_unlocked(path, data)
        return data


def finish_job(
    path: Path,
    *,
    status: str,
    error: str = "",
    progress: str = "",
    advance: bool = False,
) -> dict[str, Any]:
    with _LOCK:
        data = _read_unlocked(path)
        data["job_status"] = status
        data["job_error"] = error
        data["job_progress"] = progress
        data["last_finished_at_utc"] = _iso(utc_now())
        if advance:
            data["next_run_at_utc"] = _iso(advance_next_run(data))
        elif not data.get("next_run_at_utc"):
            data["next_run_at_utc"] = _iso(compute_next_run(data))
        _write_unlocked(path, data)
        return data


def clear_stale_job(path: Path, *, now: datetime | None = None) -> None:
    moment = now or utc_now()
    with _LOCK:
        data = _read_unlocked(path)
        if data.get("job_status") != "running":
            return
        started = parse_utc(str(data.get("last_started_at_utc") or ""))
        if started is not None and (moment - started).total_seconds() < STALE_JOB_SEC:
            return
        data["job_status"] = "error"
        data["job_error"] = "Опрос прерван."
        data["job_progress"] = ""
        data["last_finished_at_utc"] = _iso(moment)
        if not data.get("next_run_at_utc"):
            data["next_run_at_utc"] = _iso(compute_next_run(data))
        _write_unlocked(path, data)

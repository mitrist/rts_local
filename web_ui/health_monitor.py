"""Process health: event-loop lag, request tracking, unauthenticated /health/* endpoints."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class HealthMonitor:
    started_at: float = field(default_factory=time.monotonic)
    started_at_utc: str = field(default_factory=_utc_iso)
    last_request_at: float = field(default_factory=time.monotonic)
    request_count: int = 0
    event_loop_lag_sec: float = 0.0
    db_check_ok: bool | None = None
    db_check_error: str = ""
    db_check_at_utc: str = ""
    html_supervisor_errors: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_request(self) -> None:
        now = time.monotonic()
        with self._lock:
            self.last_request_at = now
            self.request_count += 1

    def record_loop_lag(self, lag_sec: float) -> None:
        with self._lock:
            self.event_loop_lag_sec = max(0.0, float(lag_sec))

    def record_db_check(self, ok: bool, error: str = "") -> None:
        with self._lock:
            self.db_check_ok = ok
            self.db_check_error = error[:500]
            self.db_check_at_utc = _utc_iso()

    def record_html_supervisor_error(self) -> None:
        with self._lock:
            self.html_supervisor_errors += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            max_lag = _env_float("RTS_HEALTH_MAX_LOOP_LAG_SEC", 15.0)
            idle_sec = max(0.0, now - self.last_request_at)
            lag = self.event_loop_lag_sec
            degraded = lag >= max_lag
            return {
                "ok": True,
                "degraded": degraded,
                "started_at_utc": self.started_at_utc,
                "uptime_sec": round(now - self.started_at, 1),
                "request_count": self.request_count,
                "idle_sec": round(idle_sec, 1),
                "event_loop_lag_sec": round(lag, 3),
                "max_loop_lag_sec": max_lag,
                "db_check_ok": self.db_check_ok,
                "db_check_error": self.db_check_error,
                "db_check_at_utc": self.db_check_at_utc,
                "html_supervisor_errors": self.html_supervisor_errors,
            }


monitor = HealthMonitor()
router = APIRouter()


async def heartbeat_loop(stop: asyncio.Event) -> None:
    """Detect event-loop stalls (sync DB/HTTP on the main thread)."""
    interval = max(0.5, _env_float("RTS_HEALTH_HEARTBEAT_SEC", 1.0))
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            lag = time.monotonic() - t0 - interval
            monitor.record_loop_lag(lag)


def _ping_db(db_path: Path) -> tuple[bool, str]:
    if not db_path.is_file():
        return False, "database file missing"
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, str(exc)
    return True, ""


@router.get("/health/live")
async def health_live() -> JSONResponse:
    body = monitor.snapshot()
    status = 200 if body.get("ok") and not body.get("degraded") else 503
    return JSONResponse(body, status_code=status)


@router.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    db_path = Path(str(getattr(request.app.state, "analytics_db_path", "") or ""))
    if not db_path:
        body = monitor.snapshot()
        body["ready"] = False
        body["ready_error"] = "analytics_db_path not configured"
        return JSONResponse(body, status_code=503)

    ok, err = await asyncio.to_thread(_ping_db, db_path)
    monitor.record_db_check(ok, err)
    body = monitor.snapshot()
    body["ready"] = ok
    if not ok:
        body["ready_error"] = err
    status = 200 if ok and not body.get("degraded") else 503
    return JSONResponse(body, status_code=status)


async def request_timing_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    monitor.record_request()
    return await call_next(request)

#!/usr/bin/env python3
"""External watchdog: probe /health/live and restart rts-tender after repeated failures.

Install via deploy/rts-tender-watchdog.{service,timer}.example on the server.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOG = logging.getLogger("rts_tender_watchdog")

DEFAULT_URL = "http://127.0.0.1:8765/health/live"
DEFAULT_STATE = "/run/rts-tender/watchdog-failures.txt"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_failures(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip() or "0"))
    except ValueError:
        return 0


def _write_failures(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(max(0, int(value))), encoding="utf-8")


def _fetch_health(url: str, timeout_sec: float) -> tuple[bool, dict | None, str]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        status = int(exc.code or 0)
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        return False, data, f"HTTP {status}: {raw[:200]}"
    except Exception as exc:
        return False, None, str(exc)

    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return False, None, f"invalid JSON: {raw[:200]}"

    if status != 200:
        return False, data if isinstance(data, dict) else None, f"HTTP {status}"
    if not isinstance(data, dict):
        return False, None, "empty health payload"
    if not data.get("ok"):
        return False, data, "ok=false"
    if data.get("degraded"):
        lag = data.get("event_loop_lag_sec")
        return False, data, f"degraded event_loop_lag_sec={lag}"
    return True, data, ""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    url = os.environ.get("RTS_WATCHDOG_URL", DEFAULT_URL).strip() or DEFAULT_URL
    timeout_sec = _env_float("RTS_WATCHDOG_TIMEOUT_SEC", 5.0)
    max_failures = _env_int("RTS_WATCHDOG_MAX_FAILURES", 3)
    state_path = Path(os.environ.get("RTS_WATCHDOG_STATE", DEFAULT_STATE))
    service = os.environ.get("RTS_WATCHDOG_SERVICE", "rts-tender").strip() or "rts-tender"

    ok, data, err = _fetch_health(url, timeout_sec)
    if ok:
        _write_failures(state_path, 0)
        lag = (data or {}).get("event_loop_lag_sec")
        LOG.info("health OK url=%s lag=%s", url, lag)
        return 0

    streak = _read_failures(state_path) + 1
    _write_failures(state_path, streak)
    LOG.warning("health FAIL url=%s streak=%s/%s err=%s", url, streak, max_failures, err)

    if streak < max_failures:
        return 1

    LOG.error("restarting %s after %s consecutive health failures", service, streak)
    try:
        subprocess.run(["systemctl", "restart", service], check=True, timeout=60)
    except Exception as exc:
        LOG.error("systemctl restart failed: %s", exc)
        return 2
    _write_failures(state_path, 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

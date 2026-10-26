"""Standalone cron entry for RTS PP registry monitor (systemd timer / cron)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows — cron runs on Linux; lock is best-effort locally
    fcntl = None  # type: ignore[assignment]

from eis_analytics_db import analytics_db_path, DEFAULT_DB_NAME
from notify import (
    max_creds_from_env_file,
    resolve_max_chat_id,
    resolve_telegram_chat_id,
    send_max_markdown_long,
    send_telegram_html_long,
    telegram_creds_from_env_file,
)
from rts_pp_monitor import (
    MAX_PP_MONITOR_CHANNEL,
    TELEGRAM_PP_MONITOR_CHANNEL,
    run_rts_pp_monitor_poll,
)
from rts_pp_monitor_settings import load_settings, settings_path, poll_interval_sec_from_settings

_LOG = logging.getLogger("rts_pp_monitor_cron")
_STATE_NAME = "rts_pp_monitor_cron_state.json"
_LOCK_NAME = "rts_pp_monitor_poll.lock"
_DEFAULT_INTERVAL_SEC = 420  # 7 minutes


def project_root() -> Path:
    return Path(os.environ.get("RTS_PROJECT_ROOT", Path(__file__).resolve().parent)).resolve()


def meta_dir() -> Path:
    state = Path(os.environ.get("RTS_STATE_FILE", str(project_root() / "rts_invites_state.json"))).resolve()
    return Path(os.environ.get("RTS_META_DIR", str(state.parent))).resolve()


def eis_env_path() -> Path:
    return Path(os.environ.get("EIS_ENV_FILE", str(project_root() / "env.txt"))).resolve()


def analytics_db() -> Path:
    return analytics_db_path(meta_dir(), os.environ.get("EIS_ANALYTICS_DB", DEFAULT_DB_NAME))


def cron_state_path() -> Path:
    return meta_dir() / _STATE_NAME


def cron_lock_path() -> Path:
    return meta_dir() / _LOCK_NAME


def poll_interval_sec() -> int:
    settings = load_settings(settings_path(meta_dir()))
    from_settings = poll_interval_sec_from_settings(settings)
    if from_settings is not None:
        return from_settings
    raw = os.environ.get("RTS_PP_MONITOR_INTERVAL_SEC", "").strip()
    if raw:
        try:
            return max(60, min(int(raw), 7200))
        except ValueError:
            pass
    raw_min = os.environ.get("RTS_PP_MONITOR_INTERVAL_MIN", "").strip()
    if raw_min:
        try:
            return max(60, min(int(raw_min) * 60, 7200))
        except ValueError:
            pass
    return _DEFAULT_INTERVAL_SEC


def cron_mode_enabled() -> bool:
    raw = os.environ.get("RTS_PP_MONITOR_CRON", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def load_cron_state() -> dict[str, Any]:
    path = cron_state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cron_state(payload: dict[str, Any]) -> None:
    path = cron_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _interval_skip_payload(*, force: bool) -> dict[str, Any] | None:
    if force:
        return None
    settings = load_settings(settings_path(meta_dir()))
    if not settings.get("enabled"):
        return {"ok": True, "skipped": True, "reason": "disabled"}
    state = load_cron_state()
    finished_raw = str(state.get("finished_at_utc") or "").strip()
    if not finished_raw:
        return None
    try:
        finished_dt = datetime.fromisoformat(finished_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    interval = poll_interval_sec()
    if datetime.now(timezone.utc) < finished_dt + timedelta(seconds=interval):
        return {"ok": True, "skipped": True, "reason": "interval_wait"}
    return None


def build_pp_monitor_senders(timeout: float) -> list[tuple[str, str, Any, str, str, float, str]]:
    try:
        tg_token, _ = telegram_creds_from_env_file(eis_env_path())
        max_token, _ = max_creds_from_env_file(eis_env_path())
        monitor = load_settings(settings_path(meta_dir()))
        tg_chat = resolve_telegram_chat_id(eis_env_path(), str(monitor.get("telegram_chat_id") or ""))
        max_chat = resolve_max_chat_id(eis_env_path(), str(monitor.get("max_chat_id") or ""))
    except Exception:
        _LOG.exception("Cannot load messenger credentials")
        return []
    senders = [
        (
            TELEGRAM_PP_MONITOR_CHANNEL,
            "Telegram PP monitor",
            send_telegram_html_long,
            tg_token,
            tg_chat,
            timeout,
            "html",
        ),
        (
            MAX_PP_MONITOR_CHANNEL,
            "MAX PP monitor",
            send_max_markdown_long,
            max_token,
            max_chat,
            timeout,
            "markdown",
        ),
    ]
    tg_on = bool(monitor.get("notify_telegram", True))
    max_on = bool(monitor.get("notify_max", True))
    if not tg_on and not max_on:
        return []
    if not tg_on:
        return [s for s in senders if s[0] != TELEGRAM_PP_MONITOR_CHANNEL]
    if not max_on:
        return [s for s in senders if s[0] != MAX_PP_MONITOR_CHANNEL]
    return senders


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_lock_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw.isdigit() else None
    except OSError:
        return None


class PollLockError(Exception):
    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        self.message = message
        super().__init__(message or reason)


class PollFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fp = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                self._fp = open(self.path, "a+", encoding="utf-8")
            except PermissionError as exc:
                raise PollLockError(
                    "lock_permission_denied",
                    f"Нет доступа к lock-файлу {self.path}: {exc}",
                ) from exc
            if fcntl is None:
                return True
            try:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._fp.close()
                self._fp = None
                stale_pid = _read_lock_pid(self.path)
                if attempt == 0 and stale_pid is not None and not _pid_alive(stale_pid):
                    try:
                        self.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                return False
            self._fp.seek(0)
            self._fp.truncate(0)
            self._fp.write(str(os.getpid()))
            self._fp.flush()
            return True
        return False

    def release(self) -> None:
        if self._fp is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        finally:
            self._fp.close()
            self._fp = None


def _stale_poll_sec() -> int:
    raw = os.environ.get("RTS_PP_MONITOR_STALE_POLL_SEC", "").strip()
    if raw:
        try:
            return max(60, min(int(raw), 86_400))
        except ValueError:
            pass
    return 20 * 60


def poll_lock_is_held() -> bool:
    """True when the poll lock file is held by a live process."""
    lock = PollFileLock(cron_lock_path())
    try:
        acquired = lock.acquire()
    except PollLockError:
        return True
    if acquired:
        lock.release()
        return False
    return True


def resolve_poll_running(
    state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """True when a poll is in progress; false for dead or stale PIDs."""
    if not bool(state.get("running")):
        return False
    if not poll_lock_is_held():
        return False
    pid_raw = state.get("pid")
    if isinstance(pid_raw, int) and not _pid_alive(pid_raw):
        return False
    started_raw = str(state.get("started_at_utc") or "").strip()
    if not started_raw:
        return True
    try:
        started_dt = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    ts = now or datetime.now(timezone.utc)
    return (ts - started_dt).total_seconds() <= float(_stale_poll_sec())


def clear_stale_running_state(state: dict[str, Any]) -> dict[str, Any] | None:
    """Drop running=true when no poll lock is held (e.g. web restart mid-poll)."""
    if not bool(state.get("running")) or poll_lock_is_held():
        return None
    repaired = {**state, "running": False}
    if not str(state.get("finished_at_utc") or "").strip():
        repaired["reason"] = "stale_lock_cleared"
    return repaired


def stale_running_state(state: dict[str, Any], *, now: datetime | None = None) -> bool:
    """True when state claims running but the poll process is gone or timed out."""
    return bool(state.get("running")) and not resolve_poll_running(state, now=now)


def _poll_state_from_result(base_state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        **base_state,
        "running": False,
        "finished_at_utc": _utc_now_iso(),
        "ok": bool(result.get("ok")),
        "skipped": bool(result.get("skipped")),
        "reason": str(result.get("reason") or ""),
        "pages_fetched": int(result.get("pages_fetched") or 0),
        "items_new": int(result.get("items_new") or 0),
        "notified": int(result.get("notified") or 0),
        "notify_failed": int(result.get("notify_failed") or 0),
        "ktru_polled": int(result.get("ktru_polled") or 0),
        "errors": result.get("errors") or [],
        "error": str(result.get("error") or "")[:500],
    }


def run_poll_once(*, force: bool = False) -> dict[str, Any]:
    wait = _interval_skip_payload(force=force)
    if wait is not None:
        return wait
    lock = PollFileLock(cron_lock_path())
    try:
        if not lock.acquire():
            return {"ok": True, "skipped": True, "reason": "busy"}
    except PollLockError as exc:
        _LOG.error("PP monitor poll lock: %s", exc.message)
        return {
            "ok": False,
            "skipped": False,
            "reason": exc.reason,
            "error": exc.message,
        }

    started = _utc_now_iso()
    base_state = {
        "running": True,
        "started_at_utc": started,
        "interval_sec": poll_interval_sec(),
        "pid": os.getpid(),
    }
    save_cron_state(base_state)
    result: dict[str, Any] = {"ok": False, "reason": "internal_error", "error": "poll did not start"}
    try:
        try:
            timeout = max(5.0, min(float(os.environ.get("TELEGRAM_TIMEOUT_SEC", "20")), 120.0))
        except ValueError:
            timeout = 20.0
        senders = build_pp_monitor_senders(timeout)
        result = run_rts_pp_monitor_poll(
            meta_dir(),
            analytics_db(),
            force=force,
            senders=senders or None,
        )
        try:
            from auto_monitor_draft import drain_summary_as_dict, process_auto_monitor_draft_queue

            drain = process_auto_monitor_draft_queue(
                meta_dir(),
                analytics_db(),
                project_root=project_root(),
                limit=5,
            )
            result = {**result, "auto_monitor_draft": drain_summary_as_dict(drain)}
        except Exception as exc:
            _LOG.warning("auto_monitor_draft queue drain failed: %s", exc)
            result = {**result, "auto_monitor_draft_error": str(exc)[:300]}
    except Exception as exc:
        _LOG.exception("PP monitor poll crashed")
        result = {"ok": False, "reason": "internal_error", "error": str(exc)[:500]}
    finally:
        save_cron_state(_poll_state_from_result(base_state, result))
        lock.release()
    return result


def cron_status_payload(*, now: float | None = None) -> dict[str, Any]:
    import time

    ts = time.time() if now is None else now
    settings = load_settings(settings_path(meta_dir()))
    env_on = os.environ.get("RTS_PP_MONITOR", "").strip().lower() in ("1", "true", "yes", "on")
    cron_on = cron_mode_enabled()
    enabled = cron_on and env_on and bool(settings.get("enabled"))
    interval_sec = poll_interval_sec()
    now_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    state = load_cron_state()
    repaired = clear_stale_running_state(state)
    if repaired is not None:
        save_cron_state(repaired)
        state = repaired
    running = resolve_poll_running(state, now=now_dt)
    stale_running = stale_running_state(state, now=now_dt)
    finished_raw = str(state.get("finished_at_utc") or "").strip()
    next_poll_unix: float | None = None
    seconds_left: int | None = None
    if running:
        seconds_left = None
    elif finished_raw:
        try:
            finished_dt = datetime.fromisoformat(finished_raw.replace("Z", "+00:00"))
            next_poll_unix = finished_dt.timestamp() + interval_sec
            seconds_left = max(0, int(next_poll_unix - ts))
        except ValueError:
            pass
    elif stale_running or enabled:
        seconds_left = 0
    poll_due = (
        enabled
        and not running
        and (stale_running or not finished_raw or seconds_left == 0)
    )
    return {
        "enabled": enabled,
        "env_monitor": env_on,
        "settings_enabled": bool(settings.get("enabled")),
        "cron_mode": cron_on,
        "polling": running,
        "poll_due": poll_due,
        "interval_min": max(1, interval_sec // 60),
        "interval_sec": interval_sec,
        "settings_poll_interval_min": int(settings.get("poll_interval_min") or 0),
        "next_poll_unix": next_poll_unix,
        "seconds_until_next": seconds_left,
        "last_run": state if state else None,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Poll RTS PP competitor registry (cron/systemd).")
    parser.add_argument("--force", action="store_true", help="Run even if monitor disabled in settings.")
    args = parser.parse_args(argv)

    result = run_poll_once(force=bool(args.force))
    if result.get("skipped") and result.get("reason") == "busy":
        _LOG.info("PP monitor poll skipped: another run in progress")
        return 0
    if result.get("skipped"):
        _LOG.info("PP monitor poll skipped: %s", result.get("reason") or "unknown")
        return 0
    if not result.get("ok"):
        reason = str(result.get("reason") or "")
        _LOG.error(
            "PP monitor poll failed: %s",
            result.get("error") or reason or "unknown",
        )
        return 0 if reason in ("session_expired", "no_cookie") else 1
    _LOG.info(
        "PP monitor poll ok: pages=%s new=%s notified=%s",
        result.get("pages_fetched"),
        result.get("items_new"),
        result.get("notified"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

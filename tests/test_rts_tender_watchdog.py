from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tools import rts_tender_watchdog as wd


def test_watchdog_resets_on_ok(tmp_path, monkeypatch):
    state = tmp_path / "fail.txt"
    monkeypatch.setenv("RTS_WATCHDOG_STATE", str(state))
    state.write_text("2", encoding="utf-8")

    payload = {"ok": True, "degraded": False, "event_loop_lag_sec": 0.01}
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    setattr(resp, "status", 200)

    with patch.object(wd.urllib.request, "urlopen", return_value=resp):
        assert wd.main() == 0
    assert state.read_text(encoding="utf-8") == "0"


def test_watchdog_restarts_after_streak(tmp_path, monkeypatch):
    state = tmp_path / "fail.txt"
    monkeypatch.setenv("RTS_WATCHDOG_STATE", str(state))
    monkeypatch.setenv("RTS_WATCHDOG_MAX_FAILURES", "2")

    with patch.object(wd, "_fetch_health", return_value=(False, None, "timeout")):
        with patch.object(wd.subprocess, "run") as run_mock:
            assert wd.main() == 1
            assert state.read_text(encoding="utf-8") == "1"
            assert wd.main() == 0
            run_mock.assert_called_once()

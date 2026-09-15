from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_web_auth(tmp_path):
    """Тесты без web_users.txt в корне — вход не требуется."""
    from web_ui import auth as web_auth

    empty = tmp_path / "web_users.txt"
    empty.write_text("# tests: no accounts\n", encoding="utf-8")
    web_auth.configure_web_auth(empty)

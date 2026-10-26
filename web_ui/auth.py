"""Веб-авторизация: несколько login:password в файле, сессия Starlette."""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette import status

_LOG = logging.getLogger("web_ui.auth")

SESSION_USER_KEY = "web_user"
_USERS: dict[str, str] = {}
_USERS_FILE: Path | None = None
_USERS_MTIME: float | None = None


def web_users_file_path(project_root: Path) -> Path:
    raw = os.environ.get("RTS_WEB_USERS_FILE", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (project_root / raw).resolve()
    return (project_root / "web_users.txt").resolve()


def parse_web_users_file(path: Path) -> dict[str, str]:
    """Строки login:password; # и пустые — пропуск. Дубликат логина — последняя строка."""
    users: dict[str, str] = {}
    if not path.is_file():
        return users
    if not os.access(path, os.R_OK):
        _LOG.warning("Нет доступа на чтение %s — только RTS_WEB_USER/PASSWORD", path)
        return users
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _LOG.warning("Не удалось прочитать %s: %s", path, exc)
        return users
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            _LOG.warning("web_users: пропуск строки без ':'")
            continue
        login, _, password = s.partition(":")
        login = login.strip()
        if not login:
            continue
        if login in users:
            _LOG.warning("web_users: дубликат логина %r — перезапись", login)
        users[login] = password
    return users


def _env_fallback_user() -> dict[str, str]:
    user = os.environ.get("RTS_WEB_USER", "").strip()
    pw = os.environ.get("RTS_WEB_PASSWORD", "").strip()
    if user and pw:
        return {user: pw}
    return {}


def _reload_users_if_needed() -> None:
    global _USERS, _USERS_MTIME
    path = _USERS_FILE
    if path is None:
        _USERS = _env_fallback_user()
        _USERS_MTIME = None
        return
    if path.is_file():
        mtime = path.stat().st_mtime
        if _USERS_MTIME != mtime:
            _USERS = parse_web_users_file(path)
            _USERS_MTIME = mtime
            if not _USERS:
                _USERS = _env_fallback_user()
    else:
        _USERS = _env_fallback_user()
        _USERS_MTIME = None


def configure_web_auth(users_file: Path) -> None:
    global _USERS_FILE, _USERS_MTIME
    _USERS_FILE = users_file
    _USERS_MTIME = None
    _reload_users_if_needed()


def get_web_users() -> dict[str, str]:
    _reload_users_if_needed()
    return dict(_USERS)


def write_web_users_file(path: Path, users: dict[str, str]) -> None:
    """Перезаписать web_users.txt (login:password на строку)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for login in sorted(users):
        name = login.strip()
        if not name:
            continue
        password = users[login]
        if ":" in name:
            raise ValueError(f"Логин не может содержать «:»: {name!r}")
        lines.append(f"{name}:{password}")
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(path)
    global _USERS_MTIME
    if _USERS_FILE == path:
        _USERS_MTIME = None
        _reload_users_if_needed()


def upsert_web_user(path: Path, login: str, password: str, *, allow_empty_password: bool = False) -> None:
    name = login.strip()
    if not name:
        raise ValueError("Укажите логин.")
    users = parse_web_users_file(path) if path.is_file() else {}
    pw = password
    if not pw and allow_empty_password and name in users:
        pw = users[name]
    if not pw:
        raise ValueError("Укажите пароль.")
    users[name] = pw
    write_web_users_file(path, users)


def delete_web_user(path: Path, login: str) -> bool:
    name = login.strip()
    users = parse_web_users_file(path) if path.is_file() else {}
    if name not in users:
        return False
    del users[name]
    write_web_users_file(path, users)
    return True


def list_web_users_for_admin(path: Path) -> list[str]:
    return sorted(parse_web_users_file(path).keys()) if path.is_file() else []


def auth_enabled() -> bool:
    return bool(get_web_users())


def verify_credentials(username: str, password: str) -> bool:
    users = get_web_users()
    expected = users.get(username.strip())
    if expected is None:
        return False
    return secrets.compare_digest(password, expected)


def session_username(request: Request) -> str | None:
    raw = request.session.get(SESSION_USER_KEY)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def login_user(request: Request, username: str) -> None:
    request.session[SESSION_USER_KEY] = username.strip()


def logout_user(request: Request) -> None:
    request.session.pop(SESSION_USER_KEY, None)


def safe_next_path(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s.startswith("/") or s.startswith("//"):
        return "/analytics"
    if s == "/":
        return "/analytics"
    return s


def login_redirect_url(next_path: str | None) -> str:
    n = safe_next_path(next_path)
    if n == "/":
        return "/login"
    return f"/login?next={quote(n, safe='')}"


def ui_context(request: Request) -> dict[str, Any]:
    user = session_username(request)
    try:
        from rts_pp_publish_service import publish_enabled as _pp_pub_on

        pub_on = bool(_pp_pub_on())
    except Exception:
        pub_on = False
    return {
        "web_auth_enabled": auth_enabled(),
        "web_user": user,
        "web_users_file": str(_USERS_FILE) if _USERS_FILE else "",
        "settings_admin": is_settings_admin(user),
        "pp_publish_enabled": pub_on,
    }


def settings_admin_users() -> set[str]:
    raw = os.environ.get("RTS_SETTINGS_ADMIN_USERS", "admin").strip()
    if not raw:
        return {"admin"}
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_settings_admin(username: str | None) -> bool:
    user = (username or "").strip()
    if not user:
        return False
    if not auth_enabled():
        return True
    return user in settings_admin_users()


def require_settings_admin(request: Request) -> str:
    user = require_session(request)
    if not is_settings_admin(user):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/analytics"},
        )
    return user


def require_session(request: Request) -> str:
    if not auth_enabled():
        return ""
    user = session_username(request)
    if user and user in get_web_users():
        return user
    if user and user not in get_web_users():
        logout_user(request)
    path = request.url.path
    if path.startswith("/api/"):
        raise HTTPException(status_code=401, detail="Требуется вход")
    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": login_redirect_url(next_url)},
    )

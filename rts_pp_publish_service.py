"""Orchestrate PP publish jobs: prepare → await operator signature → TSA → publish."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eis_analytics_db import (
    create_pp_publish_job,
    find_pp_publish_job_for_draft,
    get_pp_publish_job,
    update_pp_publish_job,
)
from rts_pp_api import RtsPpApiError, load_offers_cookie, validate_cookie_session
from rts_pp_publish import (
    append_signed_data,
    build_sign_requests,
    normalize_submitted_signatures,
    prepare_for_publish,
    publish_offer,
)

_LOG = logging.getLogger("rts_pp_publish")

ACTIVE_STATUSES = ("preparing", "awaiting_signature", "timestamping", "publishing")
REUSE_STATUSES = ("published", "awaiting_signature", "preparing", "timestamping", "publishing")


def publish_enabled() -> bool:
    return os.environ.get("RTS_PP_PUBLISH_ENABLED", "").strip().lower() in ("1", "true", "yes")


@dataclass
class PublishJobResult:
    ok: bool
    job_id: int | None = None
    status: str = ""
    rts_draft_id: int | None = None
    sign_requests: list[dict[str, Any]] | None = None
    error: str = ""
    reason: str = ""
    http_status: int = 200
    reused: bool = False

    def as_api_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "job_id": self.job_id,
            "status": self.status,
            "rts_draft_id": self.rts_draft_id,
            "sign_requests": self.sign_requests or [],
            "error": self.error,
            "reason": self.reason,
            "reused": self.reused,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _job_to_result(job: dict[str, Any], *, reused: bool = False, ok: bool | None = None) -> PublishJobResult:
    status = str(job.get("status") or "")
    err = str(job.get("error_text") or "")
    sign_reqs = job.get("sign_requests")
    if not isinstance(sign_reqs, list):
        sign_reqs = []
    if ok is None:
        ok = status == "published" or status == "awaiting_signature"
    return PublishJobResult(
        ok=ok and status != "failed",
        job_id=int(job["id"]) if job.get("id") is not None else None,
        status=status,
        rts_draft_id=int(job["rts_draft_id"]) if job.get("rts_draft_id") is not None else None,
        sign_requests=sign_reqs,
        error=err,
        reason="failed" if status == "failed" else "",
        reused=reused,
        http_status=200 if status != "failed" else 400,
    )


def start_publish_job(
    *,
    analytics_db: Path,
    cookie_path: Path,
    rts_draft_id: int,
    rts_profile: str = "",
    source: str = "",
    source_key: str = "",
    force_new: bool = False,
) -> PublishJobResult:
    if not publish_enabled():
        return PublishJobResult(
            ok=False,
            error="Публикация ПП отключена (RTS_PP_PUBLISH_ENABLED).",
            reason="disabled",
            http_status=404,
        )
    draft_id = int(rts_draft_id)
    if draft_id <= 0:
        return PublishJobResult(
            ok=False,
            error="Укажите rts_draft_id.",
            reason="bad_request",
            http_status=400,
        )
    profile = str(rts_profile or "").strip()

    if not force_new:
        existing = find_pp_publish_job_for_draft(
            analytics_db,
            rts_draft_id=draft_id,
            rts_profile=profile,
            statuses=REUSE_STATUSES,
        )
        if existing:
            st = str(existing.get("status") or "")
            if st == "published":
                return _job_to_result(existing, reused=True, ok=True)
            if st in ACTIVE_STATUSES:
                return _job_to_result(existing, reused=True, ok=True)

    job_id = create_pp_publish_job(
        analytics_db,
        rts_draft_id=draft_id,
        rts_profile=profile,
        source=source,
        source_key=source_key,
        status="preparing",
    )
    try:
        cookie = load_offers_cookie(cookie_path)
        validate_cookie_session(cookie)
        prepare = prepare_for_publish(cookie, draft_id)
        sign_requests = build_sign_requests(prepare)
        update_pp_publish_job(
            analytics_db,
            job_id,
            status="awaiting_signature",
            prepare_json=json.dumps(prepare, ensure_ascii=False),
            sign_requests_json=json.dumps(sign_requests, ensure_ascii=False),
            error_text="",
        )
    except RtsPpApiError as exc:
        update_pp_publish_job(
            analytics_db,
            job_id,
            status="failed",
            error_text=str(exc),
        )
        _LOG.warning("publish prepare failed draft=%s: %s", draft_id, exc)
        http = 401 if exc.reason == "session_expired" else 400
        if exc.reason == "no_cookie":
            http = 400
        return PublishJobResult(
            ok=False,
            job_id=job_id,
            status="failed",
            rts_draft_id=draft_id,
            error=str(exc),
            reason=exc.reason,
            http_status=http,
        )
    except Exception as exc:  # noqa: BLE001
        update_pp_publish_job(
            analytics_db,
            job_id,
            status="failed",
            error_text=str(exc)[:2000],
        )
        _LOG.exception("publish prepare unexpected draft=%s", draft_id)
        return PublishJobResult(
            ok=False,
            job_id=job_id,
            status="failed",
            rts_draft_id=draft_id,
            error=str(exc),
            reason="error",
            http_status=500,
        )

    job = get_pp_publish_job(analytics_db, job_id)
    assert job is not None
    return _job_to_result(job, reused=False, ok=True)


def submit_publish_signatures(
    *,
    analytics_db: Path,
    cookie_path: Path,
    job_id: int,
    signatures: list[dict[str, Any]],
    rts_profile: str = "",
) -> PublishJobResult:
    if not publish_enabled():
        return PublishJobResult(
            ok=False,
            error="Публикация ПП отключена (RTS_PP_PUBLISH_ENABLED).",
            reason="disabled",
            http_status=404,
        )
    job = get_pp_publish_job(analytics_db, int(job_id))
    if job is None:
        return PublishJobResult(
            ok=False,
            error="Задание публикации не найдено.",
            reason="not_found",
            http_status=404,
        )
    profile = str(rts_profile or "").strip()
    job_profile = str(job.get("rts_profile") or "").strip()
    if profile and job_profile and profile != job_profile:
        return PublishJobResult(
            ok=False,
            job_id=int(job["id"]),
            error="Профиль не совпадает с заданием.",
            reason="forbidden",
            http_status=403,
        )
    status = str(job.get("status") or "")
    if status == "published":
        return _job_to_result(job, reused=True, ok=True)
    if status != "awaiting_signature":
        return PublishJobResult(
            ok=False,
            job_id=int(job["id"]),
            status=status,
            rts_draft_id=int(job["rts_draft_id"]) if job.get("rts_draft_id") else None,
            error=f"Задание в статусе «{status}», подпись не принимается.",
            reason="bad_state",
            http_status=409,
        )

    sign_requests = job.get("sign_requests")
    prepare = job.get("prepare")
    if not isinstance(sign_requests, list) or not isinstance(prepare, dict):
        update_pp_publish_job(
            analytics_db,
            int(job["id"]),
            status="failed",
            error_text="В задании нет данных prepare/sign_requests.",
        )
        return PublishJobResult(
            ok=False,
            job_id=int(job["id"]),
            status="failed",
            error="В задании нет данных prepare.",
            reason="bad_state",
            http_status=500,
        )

    try:
        normalized = normalize_submitted_signatures(sign_requests, signatures)
    except RtsPpApiError as exc:
        return PublishJobResult(
            ok=False,
            job_id=int(job["id"]),
            status=status,
            rts_draft_id=int(job["rts_draft_id"]),
            sign_requests=sign_requests,
            error=str(exc),
            reason=exc.reason,
            http_status=400,
        )

    update_pp_publish_job(
        analytics_db,
        int(job["id"]),
        status="timestamping",
        signatures_json=json.dumps(normalized, ensure_ascii=False),
        error_text="",
    )
    try:
        cookie = load_offers_cookie(cookie_path)
        validate_cookie_session(cookie)
        tsa_result = append_signed_data(
            cookie,
            normalized,
            sign_requests=sign_requests,
        )
        stamped = tsa_result if isinstance(tsa_result, list) else normalized
        update_pp_publish_job(
            analytics_db,
            int(job["id"]),
            status="publishing",
            signatures_json=json.dumps(stamped, ensure_ascii=False),
        )
        publish_offer(
            cookie,
            prepare,
            stamped if isinstance(stamped, list) else normalized,
            draft_id=int(job["rts_draft_id"]),
        )
        update_pp_publish_job(
            analytics_db,
            int(job["id"]),
            status="published",
            published_at_utc=_utc_now(),
            error_text="",
        )
    except RtsPpApiError as exc:
        update_pp_publish_job(
            analytics_db,
            int(job["id"]),
            status="failed",
            error_text=str(exc),
        )
        _LOG.warning("publish submit failed job=%s: %s", job_id, exc)
        http = 401 if exc.reason == "session_expired" else 400
        return PublishJobResult(
            ok=False,
            job_id=int(job["id"]),
            status="failed",
            rts_draft_id=int(job["rts_draft_id"]),
            error=str(exc),
            reason=exc.reason,
            http_status=http,
        )
    except Exception as exc:  # noqa: BLE001
        update_pp_publish_job(
            analytics_db,
            int(job["id"]),
            status="failed",
            error_text=str(exc)[:2000],
        )
        _LOG.exception("publish submit unexpected job=%s", job_id)
        return PublishJobResult(
            ok=False,
            job_id=int(job["id"]),
            status="failed",
            rts_draft_id=int(job["rts_draft_id"]),
            error=str(exc),
            reason="error",
            http_status=500,
        )

    final = get_pp_publish_job(analytics_db, int(job["id"]))
    assert final is not None
    if str(final.get("source") or "") == "auto_monitor":
        offer_key = str(final.get("source_key") or "").strip()
        if offer_key:
            try:
                from eis_analytics_db import update_auto_monitor_draft_queue

                update_auto_monitor_draft_queue(
                    analytics_db,
                    offer_key,
                    rts_profile=str(final.get("rts_profile") or "").strip(),
                    status="published",
                    rts_draft_id=int(final["rts_draft_id"]) if final.get("rts_draft_id") else None,
                    error_text="",
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("auto_monitor queue publish sync failed: %s", exc)
    elif str(final.get("source") or "") == "notice":
        source_key = str(final.get("source_key") or "").strip()
        if "|" in source_key:
            pn, lk = source_key.split("|", 1)
            pn, lk = pn.strip(), lk.strip()
            if pn and lk:
                try:
                    from eis_analytics_db import record_auto_participate_attempt

                    record_auto_participate_attempt(
                        analytics_db,
                        purchase_number=pn,
                        line_key=lk,
                        rts_profile=str(final.get("rts_profile") or "").strip(),
                        status="published",
                        rts_draft_id=int(final["rts_draft_id"]) if final.get("rts_draft_id") else None,
                        error_text="",
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning("auto_participate publish sync failed: %s", exc)
    return _job_to_result(final, ok=True)


def cancel_publish_job(
    *,
    analytics_db: Path,
    job_id: int,
    rts_profile: str = "",
) -> PublishJobResult:
    if not publish_enabled():
        return PublishJobResult(
            ok=False,
            error="Публикация ПП отключена (RTS_PP_PUBLISH_ENABLED).",
            reason="disabled",
            http_status=404,
        )
    job = get_pp_publish_job(analytics_db, int(job_id))
    if job is None:
        return PublishJobResult(
            ok=False,
            error="Задание публикации не найдено.",
            reason="not_found",
            http_status=404,
        )
    profile = str(rts_profile or "").strip()
    job_profile = str(job.get("rts_profile") or "").strip()
    if profile and job_profile and profile != job_profile:
        return PublishJobResult(
            ok=False,
            job_id=int(job["id"]),
            error="Профиль не совпадает с заданием.",
            reason="forbidden",
            http_status=403,
        )
    status = str(job.get("status") or "")
    if status == "published":
        return _job_to_result(job, reused=True, ok=True)
    if status not in ("awaiting_signature", "preparing", "failed"):
        return PublishJobResult(
            ok=False,
            job_id=int(job["id"]),
            status=status,
            error=f"Нельзя отменить задание в статусе «{status}».",
            reason="bad_state",
            http_status=409,
        )
    update_pp_publish_job(
        analytics_db,
        int(job["id"]),
        status="cancelled",
        error_text="Отменено оператором.",
    )
    final = get_pp_publish_job(analytics_db, int(job["id"]))
    assert final is not None
    return PublishJobResult(
        ok=True,
        job_id=int(final["id"]),
        status="cancelled",
        rts_draft_id=int(final["rts_draft_id"]) if final.get("rts_draft_id") else None,
    )


def get_publish_job_status(
    *,
    analytics_db: Path,
    job_id: int,
    rts_profile: str = "",
) -> PublishJobResult:
    if not publish_enabled():
        return PublishJobResult(
            ok=False,
            error="Публикация ПП отключена (RTS_PP_PUBLISH_ENABLED).",
            reason="disabled",
            http_status=404,
        )
    job = get_pp_publish_job(analytics_db, int(job_id))
    if job is None:
        return PublishJobResult(
            ok=False,
            error="Задание публикации не найдено.",
            reason="not_found",
            http_status=404,
        )
    profile = str(rts_profile or "").strip()
    job_profile = str(job.get("rts_profile") or "").strip()
    if profile and job_profile and profile != job_profile:
        return PublishJobResult(
            ok=False,
            job_id=int(job["id"]),
            error="Профиль не совпадает с заданием.",
            reason="forbidden",
            http_status=403,
        )
    return _job_to_result(job, ok=True)

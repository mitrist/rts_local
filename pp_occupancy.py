"""Shared RTS fingerprint occupancy for core and virtual PP queues."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eis_analytics_db import (
    get_pp_core_variant,
    has_rts_pp_draft_id,
    list_pp_core_queue,
    list_pp_virtual_queue,
)
from pp_core_catalog import variant_selection_key
from rts_cookie_profiles import normalize_rts_profile
from virtual_pp_store import get_virtual_pp_variant

CORE_QUEUED_STATUSES = frozenset({"pending", "running", "drafted", "publish_prepared", "done"})
CORE_CREATED_STATUSES = frozenset({"drafted", "publish_prepared", "done"})
VIRTUAL_QUEUED_STATUSES = frozenset({"pending", "running", "drafted"})
VIRTUAL_CREATED_STATUSES = frozenset({"drafted"})


def queue_job_blocks_selection(
    analytics_db: Path,
    job: dict[str, Any],
    created_statuses: frozenset[str],
) -> bool:
    """Old queue rows after deleting «Черновики ПП» must not block a new run."""
    status = str(job.get("status") or "")
    if status in ("pending", "running"):
        return True
    if status not in created_statuses:
        return False
    draft_id = job.get("rts_draft_id")
    if draft_id is None:
        return False
    profile = normalize_rts_profile(job.get("rts_profile"))
    if not profile:
        return False
    return has_rts_pp_draft_id(analytics_db, rts_draft_id=int(draft_id), rts_profile=profile)


def occupied_selection_keys(
    analytics_db: Path,
    *,
    ktru_code: str,
    rts_profile: str,
    queued: bool = True,
    include_core: bool = True,
    include_virtual: bool = True,
) -> dict[frozenset[tuple[str, str]], int]:
    profile = normalize_rts_profile(rts_profile)
    occupied: dict[frozenset[tuple[str, str]], int] = {}
    core_statuses = CORE_QUEUED_STATUSES if queued else CORE_CREATED_STATUSES
    virtual_statuses = VIRTUAL_QUEUED_STATUSES if queued else VIRTUAL_CREATED_STATUSES
    if include_core:
        for job in list_pp_core_queue(analytics_db, ktru_code=ktru_code, limit=500):
            if normalize_rts_profile(job.get("rts_profile")) != profile:
                continue
            if str(job.get("status") or "") not in core_statuses:
                continue
            if not queue_job_blocks_selection(analytics_db, job, CORE_CREATED_STATUSES):
                continue
            num = int(job.get("variant_no") or 0)
            variant = get_pp_core_variant(analytics_db, ktru_code, num)
            key = variant_selection_key(variant)
            if key:
                occupied.setdefault(key, num)
    if include_virtual:
        for job in list_pp_virtual_queue(analytics_db, ktru_code=ktru_code, limit=500):
            if normalize_rts_profile(job.get("rts_profile")) != profile:
                continue
            if str(job.get("status") or "") not in virtual_statuses:
                continue
            if not queue_job_blocks_selection(analytics_db, job, VIRTUAL_CREATED_STATUSES):
                continue
            num = int(job.get("variant_no") or 0)
            variant = get_virtual_pp_variant(analytics_db, ktru_code, num)
            key = variant_selection_key(variant)
            if key:
                occupied.setdefault(key, num)
    return occupied

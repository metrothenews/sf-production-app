"""Best-effort leasing and status transitions for Render_Jobs.

v1 assumes a single active agent (no atomic CAS semantics).
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any, Dict, Optional

from .gateway import SheetsGateway
from .render_contract import (
    RENDER_STATUS_FAILED,
    RENDER_STATUS_LEASED,
    RENDER_STATUS_QUEUED,
    RENDER_STATUS_RETRY,
    RENDER_STATUS_RUNNING,
    RENDER_STATUS_SUCCESS,
)
from .render_repo import find_render_job_by_id, list_render_jobs, upsert_render_job


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_utc_iso(raw: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(raw))
    except Exception as exc:
        raise ValueError(f"Invalid now_utc timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _priority_value(job: Dict[str, Any]) -> int:
    raw = job.get("priority")
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        return int(float(str(raw)))
    except Exception:
        return 0


def _created_value(job: Dict[str, Any]) -> str:
    return str(job.get("created_utc") or "")


def _int_or_default(raw: Any, default: int) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw)))
    except Exception:
        return default


def lease_one_render_job(
    gateway: SheetsGateway,
    sheet_id: str,
    now_utc: str,
    lease_seconds: int,
    owner_id: str,
) -> Optional[Dict[str, Any]]:
    current = _parse_utc_iso(now_utc)
    requested_job_id = os.environ.get("JOB_ID", "").strip()
    candidates = list_render_jobs(
        gateway,
        sheet_id,
        enabled=True,
        statuses={RENDER_STATUS_QUEUED, RENDER_STATUS_RETRY},
    )
    if requested_job_id:
        candidates = [j for j in candidates if str(j.get("render_job_id") or "").strip() == requested_job_id]
    if not candidates:
        return None

    ranked = sorted(
        candidates,
        key=lambda j: (-_priority_value(j), _created_value(j)),
    )
    selected = dict(ranked[0])
    selected["status"] = RENDER_STATUS_LEASED
    selected["lease_owner"] = owner_id
    selected["lease_until_utc"] = (current + dt.timedelta(seconds=int(lease_seconds))).isoformat()
    selected["updated_utc"] = current.isoformat()
    selected["error_msg"] = ""

    upsert_render_job(gateway, sheet_id, selected)
    return find_render_job_by_id(gateway, sheet_id, str(selected["render_job_id"]))


def mark_running(
    gateway: SheetsGateway,
    sheet_id: str,
    render_job_id: str,
    run_id: str,
    now_utc: str | None = None,
) -> Dict[str, Any]:
    job = find_render_job_by_id(gateway, sheet_id, render_job_id)
    if not job:
        raise ValueError(f"Render job not found: {render_job_id}")
    job["status"] = RENDER_STATUS_RUNNING
    job["run_id"] = run_id
    job["updated_utc"] = now_utc or _utc_now_iso()
    # Clear stale error text as soon as the job enters running state.
    job["error_msg"] = ""
    upsert_render_job(gateway, sheet_id, job)
    return job


def mark_success(
    gateway: SheetsGateway,
    sheet_id: str,
    render_job_id: str,
    run_id: str,
    now_utc: str | None = None,
) -> Dict[str, Any]:
    job = find_render_job_by_id(gateway, sheet_id, render_job_id)
    if not job:
        raise ValueError(f"Render job not found: {render_job_id}")
    job["status"] = RENDER_STATUS_SUCCESS
    job["run_id"] = run_id
    job["updated_utc"] = now_utc or _utc_now_iso()
    job["lease_owner"] = ""
    job["lease_until_utc"] = ""
    job["error_msg"] = ""
    upsert_render_job(gateway, sheet_id, job)
    return job


def mark_failed(
    gateway: SheetsGateway,
    sheet_id: str,
    render_job_id: str,
    run_id: str,
    error_msg: str,
    now_utc: str | None = None,
    force_fail: bool = False,
) -> Dict[str, Any]:
    job = find_render_job_by_id(gateway, sheet_id, render_job_id)
    if not job:
        raise ValueError(f"Render job not found: {render_job_id}")
    force_fail_env = os.environ.get("FORCE_FAIL", "0").strip() == "1"
    force_terminal = bool(force_fail or force_fail_env)
    current_attempts = _int_or_default(job.get("attempts"), 0)
    max_attempts = max(1, _int_or_default(job.get("max_attempts"), 3))
    next_attempts = current_attempts + 1

    if force_terminal or next_attempts >= max_attempts:
        job["status"] = RENDER_STATUS_FAILED
    else:
        job["status"] = RENDER_STATUS_RETRY
    job["run_id"] = run_id
    stamp = now_utc or _utc_now_iso()
    job["updated_utc"] = stamp
    job["lease_owner"] = ""
    job["lease_until_utc"] = ""
    job["attempts"] = next_attempts
    job["max_attempts"] = max_attempts
    job["last_error_utc"] = stamp
    job["error_msg"] = str(error_msg)
    upsert_render_job(gateway, sheet_id, job)
    return job

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

from .gateway import SheetsGateway
from .render_contract import RENDER_STATUS_LEASED, RENDER_STATUS_RUNNING
from .render_repo import read_render_jobs_all
from .repositories import normalize_enabled_value


class BatchTriggerError(RuntimeError):
    pass


@dataclass(frozen=True)
class BatchTriggerConfig:
    project_id: str
    region: str
    image_uri: str
    machine_type: str
    max_retries: int
    timeout_seconds: int
    task_cpu_milli: int
    task_memory_mib: int
    job_queue: str
    batch_service_account_email: str
    batch_service_account_scopes: tuple[str, ...]
    drive_sa_key_json: str
    drive_sa_key_file: str
    control_sheet_id: str
    sheets_json_base_url: str
    sheets_json_api_key: str
    require_render_binaries: str

    @classmethod
    def from_env(
        cls,
        *,
        control_sheet_id: str,
    ) -> "BatchTriggerConfig":
        project_id = str(os.environ.get("GCP_PROJECT_ID", "")).strip()
        if not project_id:
            raise BatchTriggerError("GCP_PROJECT_ID is required for RENDER_EXEC_BACKEND=batch")
        image_uri = str(os.environ.get("BATCH_IMAGE_URI", "")).strip()
        if not image_uri:
            raise BatchTriggerError("BATCH_IMAGE_URI is required for RENDER_EXEC_BACKEND=batch")

        return cls(
            project_id=project_id,
            region=str(os.environ.get("GCP_REGION", "us-east1")).strip() or "us-east1",
            image_uri=image_uri,
            machine_type=str(os.environ.get("BATCH_MACHINE_TYPE", "n2d-standard-8")).strip() or "n2d-standard-8",
            max_retries=_to_int(str(os.environ.get("BATCH_MAX_RETRIES", "1")), fallback=1),
            timeout_seconds=_to_int(str(os.environ.get("BATCH_TIMEOUT_SECONDS", "14400")), fallback=14400),
            task_cpu_milli=_to_int(str(os.environ.get("BATCH_TASK_CPU_MILLI", "8000")), fallback=8000),
            task_memory_mib=_to_int(str(os.environ.get("BATCH_TASK_MEMORY_MIB", "32768")), fallback=32768),
            job_queue=str(os.environ.get("BATCH_JOB_QUEUE", "render-default")).strip() or "render-default",
            batch_service_account_email=str(os.environ.get("BATCH_SERVICE_ACCOUNT_EMAIL", "")).strip(),
            batch_service_account_scopes=_parse_scopes(
                str(
                    os.environ.get(
                        "BATCH_SERVICE_ACCOUNT_SCOPES",
                        "https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive",
                    )
                ).strip()
            ),
            drive_sa_key_json=str(os.environ.get("DRIVE_SA_KEY_JSON", "")),
            drive_sa_key_file=str(os.environ.get("DRIVE_SA_KEY_FILE", "/secrets/drive-sa.json")).strip()
            or "/secrets/drive-sa.json",
            control_sheet_id=str(control_sheet_id or "").strip(),
            sheets_json_base_url=str(os.environ.get("SHEETS_JSON_BASE_URL", "")).strip(),
            sheets_json_api_key=str(os.environ.get("SHEETS_JSON_API_KEY", "")).strip(),
            require_render_binaries=str(os.environ.get("REQUIRE_RENDER_BINARIES", "1")).strip() or "1",
        )


def _to_int(raw: str, *, fallback: int) -> int:
    try:
        return int(str(raw).strip())
    except Exception:
        return fallback


def _parse_scopes(raw: str) -> tuple[str, ...]:
    values = []
    for scope in str(raw or "").split(","):
        text = scope.strip()
        if text and text not in values:
            values.append(text)
    return tuple(values)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sanitize_label(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = text.strip("-_")
    return text[:63]


def _build_job_id(trace_id: str) -> str:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = _sanitize_label(trace_id) or uuid.uuid4().hex[:8]
    return f"render-{ts}-{suffix[:20]}"


def summarize_render_queue_state(gateway: SheetsGateway, control_sheet_id: str) -> dict[str, Any]:
    rows = read_render_jobs_all(gateway, control_sheet_id)
    active_ids: list[str] = []
    queued_ids: list[str] = []
    for row in rows:
        if not normalize_enabled_value(row.get("enabled")):
            continue
        status = str(row.get("status") or "").strip().lower()
        job_id = str(row.get("render_job_id") or "").strip()
        if status in {RENDER_STATUS_LEASED, RENDER_STATUS_RUNNING} and job_id:
            active_ids.append(job_id)
        if status in {"queued", "retry"} and job_id:
            queued_ids.append(job_id)
    return {
        "active_job_ids": active_ids,
        "active_count": len(active_ids),
        "queued_job_ids": queued_ids,
        "queued_count": len(queued_ids),
    }


def build_batch_create_request(
    cfg: BatchTriggerConfig,
    *,
    poll_seconds: float,
    max_rounds: int,
    requested_by: str,
    trace_id: str,
) -> tuple[str, dict[str, Any]]:
    trace = trace_id.strip() or uuid.uuid4().hex[:10]
    job_id = _build_job_id(trace)
    labels = {
        "control_sheet": hashlib.sha1(cfg.control_sheet_id.encode("utf-8")).hexdigest()[:12],
        "job_queue": _sanitize_label(cfg.job_queue) or "render-default",
        "trace_id": _sanitize_label(trace) or "unknown",
        "requested_by": _sanitize_label(requested_by) or "api",
    }

    command = (
        "cd /workspace/script-orchestrator-sync && "
        "scripts/batch_entrypoint.sh "
        f"python scripts/trigger_render_batch.py --poll-seconds {poll_seconds} --max-rounds {max_rounds}"
    )
    env_vars = {
        "CONTROL_SHEET_ID": cfg.control_sheet_id,
        "SHEETS_JSON_BASE_URL": cfg.sheets_json_base_url,
        "SHEETS_JSON_API_KEY": cfg.sheets_json_api_key,
        "REQUIRE_RENDER_BINARIES": cfg.require_render_binaries,
    }
    key_file = str(cfg.drive_sa_key_file or "").strip()
    if key_file:
        env_vars["DRIVE_SA_KEY_FILE"] = key_file
    # Only materialize a key file when key JSON is explicitly configured.
    # Preferred production mode is attached service account credentials.
    if str(cfg.drive_sa_key_json or "").strip():
        env_vars["DRIVE_SA_KEY_JSON"] = cfg.drive_sa_key_json
        if key_file:
            env_vars["GOOGLE_APPLICATION_CREDENTIALS"] = key_file

    allocation_policy: dict[str, Any] = {
        "instances": [
            {
                "policy": {
                    "machine_type": cfg.machine_type,
                }
            }
        ]
    }
    service_account_policy: dict[str, Any] = {}
    if cfg.batch_service_account_email:
        service_account_policy["email"] = cfg.batch_service_account_email
    if cfg.batch_service_account_scopes:
        service_account_policy["scopes"] = list(cfg.batch_service_account_scopes)
    if service_account_policy:
        allocation_policy["service_account"] = service_account_policy

    request: dict[str, Any] = {
        "parent": f"projects/{cfg.project_id}/locations/{cfg.region}",
        "job_id": job_id,
        "job": {
            "labels": labels,
            "task_groups": [
                {
                    "task_count": 1,
                    "parallelism": 1,
                    "task_spec": {
                        "runnables": [
                            {
                                "container": {
                                    "image_uri": cfg.image_uri,
                                    "entrypoint": "/bin/bash",
                                    "commands": ["-lc", command],
                                }
                            }
                        ],
                        "environment": {"variables": env_vars},
                        "compute_resource": {
                            "cpu_milli": cfg.task_cpu_milli,
                            "memory_mib": cfg.task_memory_mib,
                        },
                        "max_retry_count": cfg.max_retries,
                        "max_run_duration": f"{cfg.timeout_seconds}s",
                    },
                }
            ],
            "allocation_policy": allocation_policy,
            "logs_policy": {"destination": "CLOUD_LOGGING"},
        },
    }
    return job_id, request


def _batch_client():
    try:
        from google.cloud import batch_v1
    except Exception as exc:  # pragma: no cover - import path depends on runtime deps
        raise BatchTriggerError(
            "google-cloud-batch is required for batch backend. Install dependencies and redeploy."
        ) from exc
    return batch_v1.BatchServiceClient()


def _state_name(raw_state: Any) -> str:
    if hasattr(raw_state, "name"):
        name = str(getattr(raw_state, "name") or "")
    else:
        name = str(raw_state or "")
    name = name.strip()
    if "." in name:
        name = name.split(".")[-1]
    return name.lower().strip()


def normalize_batch_job_status(job: Any, *, region: str) -> dict[str, Any]:
    status_obj = getattr(job, "status", None)
    state_raw = getattr(status_obj, "state", "")
    state = _state_name(state_raw)
    if state in {"succeeded"}:
        lifecycle = "success"
    elif state in {"failed", "deletion_in_progress"}:
        lifecycle = "failed"
    else:
        lifecycle = "running"
    return {
        "status": lifecycle,
        "batch_job_name": str(getattr(job, "name", "")),
        "batch_region": region,
        "batch_state": state or "unknown",
        "submitted_utc": str(getattr(job, "create_time", "")) or None,
        "updated_utc": str(getattr(job, "update_time", "")) or None,
    }


def start_render_batch_via_batch(
    *,
    gateway: SheetsGateway,
    control_sheet_id: str,
    poll_seconds: float | None = None,
    max_rounds: int | None = None,
    requested_by: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    control_id = str(control_sheet_id or "").strip()
    if not control_id:
        raise BatchTriggerError("control_sheet_id is required")

    queue_state = summarize_render_queue_state(gateway, control_id)
    if queue_state["active_count"] > 0:
        return {
            "status": "busy",
            "return_code": 2,
            "busy_reason": "render_jobs_running",
            "noop_reason": None,
            "error": None,
            "queued_job_ids": queue_state["queued_job_ids"],
            "final_progress": None,
            "batch_job_name": None,
            "batch_region": None,
            "submitted_utc": None,
        }

    cfg = BatchTriggerConfig.from_env(control_sheet_id=control_id)
    p = 1.0 if poll_seconds is None else float(poll_seconds)
    rounds = 40 if max_rounds is None else int(max_rounds)
    _, create_request = build_batch_create_request(
        cfg,
        poll_seconds=p,
        max_rounds=rounds,
        requested_by=requested_by,
        trace_id=trace_id,
    )
    try:
        client = _batch_client()
        created = client.create_job(request=create_request)
    except Exception as exc:
        raise BatchTriggerError(f"batch create_job failed: {exc}") from exc

    return {
        "status": "started",
        "return_code": None,
        "busy_reason": None,
        "noop_reason": None,
        "error": None,
        "queued_job_ids": queue_state["queued_job_ids"],
        "final_progress": None,
        "batch_job_name": str(getattr(created, "name", "")),
        "batch_region": cfg.region,
        "submitted_utc": _utc_now(),
    }


def get_batch_job_status(batch_job_name: str, *, project_id: str = "", region: str = "") -> dict[str, Any]:
    raw_name = str(batch_job_name or "").strip()
    if not raw_name:
        raise BatchTriggerError("batch_job_name is required")

    project = project_id.strip() or str(os.environ.get("GCP_PROJECT_ID", "")).strip()
    reg = region.strip() or str(os.environ.get("GCP_REGION", "us-east1")).strip() or "us-east1"
    if raw_name.startswith("projects/"):
        name = raw_name
    else:
        if not project:
            raise BatchTriggerError("GCP_PROJECT_ID is required when batch_job_name is not a full resource path")
        name = f"projects/{project}/locations/{reg}/jobs/{raw_name}"
    try:
        client = _batch_client()
        job = client.get_job(request={"name": name})
    except Exception as exc:
        raise BatchTriggerError(f"batch get_job failed: {exc}") from exc
    return normalize_batch_job_status(job, region=reg)

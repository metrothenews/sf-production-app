# src/so_sync/api.py
"""FastAPI HTTP interface for the sync agent.
Provides the minimal endpoints required by the contract:
- POST /api/v1/jobs/{job_id}/run
- GET /api/v1/runs/{run_id}
- POST /api/v1/render/run-batch
- GET /healthz
- GET /version
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import AppConfig
from .sync_engine import execute_job
from .repositories import find_run_by_id
from .render_batch_trigger import start_render_batch_process

app = FastAPI(title="script-orchestrator-sync", version="v1.0.0")

config = AppConfig()

def _gateway():
    return config.gateway

class RunRequest(BaseModel):
    trigger: str
    dedupe_key: str | None = None

class RunResponse(BaseModel):
    run_id: str
    status: str
    rows_processed: int | None = None
    error_msg: str | None = None


class RenderBatchRunRequest(BaseModel):
    poll_seconds: float | None = None
    max_rounds: int | None = None
    startup_wait_seconds: float | None = None


class RenderBatchRunResponse(BaseModel):
    status: str
    pid: int | None = None
    return_code: int | None = None
    busy_reason: str | None = None
    noop_reason: str | None = None
    error: str | None = None
    queued_job_ids: list[str] = Field(default_factory=list)
    final_progress: dict[str, Any] | None = None
    log_path: str | None = None

@app.post("/api/v1/jobs/{job_id}/run", response_model=RunResponse)
def run_job(job_id: str, req: RunRequest):
    try:
        run_entry = execute_job(
            gateway=_gateway(),
            control_sheet_id=config.control_sheet_id,
            target_sheet_id=config.default_target_sheet_id or "",
            job_id=job_id,
            trigger=req.trigger,
            dedupe_key=req.dedupe_key,
        )
        return RunResponse(
            run_id=run_entry["run_id"],
            status=run_entry["status"],
            rows_processed=run_entry.get("rows_processed"),
            error_msg=run_entry.get("error_msg"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str):
    entry = find_run_by_id(_gateway(), config.control_sheet_id, run_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(
        run_id=entry["run_id"],
        status=entry["status"],
        rows_processed=entry.get("rows_processed"),
        error_msg=entry.get("error_msg"),
    )


@app.post("/api/v1/render/run-batch", response_model=RenderBatchRunResponse)
def run_render_batch(req: RenderBatchRunRequest | None = None):
    payload = req or RenderBatchRunRequest()
    startup_wait_seconds = 0.75 if payload.startup_wait_seconds is None else payload.startup_wait_seconds
    result = start_render_batch_process(
        control_sheet_id=config.control_sheet_id,
        poll_seconds=payload.poll_seconds,
        max_rounds=payload.max_rounds,
        startup_wait_seconds=startup_wait_seconds,
        extra_env={
            "SHEETS_JSON_BASE_URL": config.sheets_json_base_url,
            "SHEETS_JSON_API_KEY": config.sheets_json_api_key or "",
        },
    )
    status = str(result.get("status") or "")
    if status == "busy":
        raise HTTPException(status_code=409, detail=result)
    if status == "error":
        raise HTTPException(status_code=500, detail=result)
    return RenderBatchRunResponse(**result)

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/version")
def version():
    return {"version": app.version}

# End of api.py

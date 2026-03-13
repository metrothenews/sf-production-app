from __future__ import annotations

import importlib
import sys

from fastapi.testclient import TestClient


def _load_api_module():
    if "so_sync.api" in sys.modules:
        del sys.modules["so_sync.api"]
    return importlib.import_module("so_sync.api")


def test_render_run_batch_started_returns_200(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_SHEET_ID", "sheet_1")
    monkeypatch.setenv("SHEETS_JSON_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("RENDER_EXEC_BACKEND", "local")
    api = _load_api_module()
    monkeypatch.setattr(
        api,
        "start_render_batch_process",
        lambda **kwargs: {
            "status": "started",
            "pid": 123,
            "return_code": None,
            "busy_reason": None,
            "noop_reason": None,
            "error": None,
            "queued_job_ids": [],
            "final_progress": None,
            "log_path": "/tmp/render_batch_test.log",
        },
    )
    client = TestClient(api.app)
    resp = client.post("/api/v1/render/run-batch", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "started"
    assert body["pid"] == 123
    assert body["execution_backend"] == "local"


def test_render_run_batch_busy_returns_409(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_SHEET_ID", "sheet_1")
    monkeypatch.setenv("SHEETS_JSON_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("RENDER_EXEC_BACKEND", "local")
    api = _load_api_module()
    monkeypatch.setattr(
        api,
        "start_render_batch_process",
        lambda **kwargs: {
            "status": "busy",
            "pid": 222,
            "return_code": 2,
            "busy_reason": "local_lock",
            "noop_reason": None,
            "error": None,
            "queued_job_ids": [],
            "final_progress": None,
            "log_path": "/tmp/render_batch_busy.log",
        },
    )
    client = TestClient(api.app)
    resp = client.post("/api/v1/render/run-batch", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "busy"


def test_render_run_batch_error_returns_500(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_SHEET_ID", "sheet_1")
    monkeypatch.setenv("SHEETS_JSON_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("RENDER_EXEC_BACKEND", "local")
    api = _load_api_module()
    monkeypatch.setattr(
        api,
        "start_render_batch_process",
        lambda **kwargs: {
            "status": "error",
            "pid": 333,
            "return_code": 1,
            "busy_reason": None,
            "noop_reason": None,
            "error": "batch_error=compile_failed code=1",
            "queued_job_ids": [],
            "final_progress": None,
            "log_path": "/tmp/render_batch_error.log",
        },
    )
    client = TestClient(api.app)
    resp = client.post("/api/v1/render/run-batch", json={})
    assert resp.status_code == 500
    assert resp.json()["detail"]["status"] == "error"


def test_render_run_batch_batch_backend_started_returns_200(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_SHEET_ID", "sheet_1")
    monkeypatch.setenv("SHEETS_JSON_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("RENDER_EXEC_BACKEND", "batch")
    api = _load_api_module()
    monkeypatch.setattr(
        api,
        "start_render_batch_via_batch",
        lambda **kwargs: {
            "status": "started",
            "return_code": None,
            "busy_reason": None,
            "noop_reason": None,
            "error": None,
            "queued_job_ids": [],
            "final_progress": None,
            "batch_job_name": "projects/p/locations/us-east1/jobs/render-abc",
            "batch_region": "us-east1",
            "submitted_utc": "2026-01-01T00:00:00+00:00",
        },
    )
    client = TestClient(api.app)
    resp = client.post("/api/v1/render/run-batch", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "started"
    assert body["execution_backend"] == "batch"
    assert body["batch_region"] == "us-east1"


def test_render_batch_job_status_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_SHEET_ID", "sheet_1")
    monkeypatch.setenv("SHEETS_JSON_BASE_URL", "https://example.invalid")
    api = _load_api_module()
    monkeypatch.setattr(
        api,
        "get_batch_job_status",
        lambda batch_job_name: {
            "status": "running",
            "batch_job_name": batch_job_name,
            "batch_region": "us-east1",
            "batch_state": "running",
            "submitted_utc": None,
            "updated_utc": None,
        },
    )
    client = TestClient(api.app)
    resp = client.get("/api/v1/render/batch-jobs/render-abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_job_name"] == "render-abc"
    assert body["status"] == "running"

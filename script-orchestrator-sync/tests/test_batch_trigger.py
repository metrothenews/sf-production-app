from __future__ import annotations

from types import SimpleNamespace

from so_sync.batch_trigger import (
    BatchTriggerConfig,
    build_batch_create_request,
    get_batch_job_status,
    summarize_render_queue_state,
)


def test_summarize_render_queue_state_counts_active_and_queued(monkeypatch) -> None:
    monkeypatch.setattr(
        "so_sync.batch_trigger.read_render_jobs_all",
        lambda gateway, control_sheet_id: [
            {"enabled": "TRUE", "render_job_id": "job_running", "status": "running"},
            {"enabled": "TRUE", "render_job_id": "job_queued", "status": "queued"},
            {"enabled": "TRUE", "render_job_id": "job_retry", "status": "retry"},
            {"enabled": "FALSE", "render_job_id": "job_disabled", "status": "running"},
        ],
    )
    state = summarize_render_queue_state(gateway=object(), control_sheet_id="sheet_1")
    assert state["active_count"] == 1
    assert state["active_job_ids"] == ["job_running"]
    assert state["queued_count"] == 2
    assert state["queued_job_ids"] == ["job_queued", "job_retry"]


def test_build_batch_create_request_has_expected_command_and_env() -> None:
    cfg = BatchTriggerConfig(
        project_id="proj_1",
        region="us-east1",
        image_uri="us-east1-docker.pkg.dev/proj/repo/app:abc",
        machine_type="n2d-standard-8",
        max_retries=1,
        timeout_seconds=14400,
        task_cpu_milli=8000,
        task_memory_mib=32768,
        job_queue="render-default",
        drive_sa_key_json="{\"type\":\"service_account\"}",
        drive_sa_key_file="/secrets/drive-sa.json",
        control_sheet_id="sheet_1",
        sheets_json_base_url="https://example.invalid",
        sheets_json_api_key="k",
        require_render_binaries="1",
    )
    job_id, req = build_batch_create_request(
        cfg,
        poll_seconds=1.25,
        max_rounds=40,
        requested_by="metrothenews@gmail.com",
        trace_id="trace123",
    )
    assert job_id.startswith("render-")
    assert req["parent"] == "projects/proj_1/locations/us-east1"
    command = req["job"]["task_groups"][0]["task_spec"]["runnables"][0]["container"]["commands"][1]
    assert "scripts/batch_entrypoint.sh" in command
    assert "--poll-seconds 1.25" in command
    assert "--max-rounds 40" in command
    env_vars = req["job"]["task_groups"][0]["task_spec"]["environment"]["variables"]
    assert env_vars["CONTROL_SHEET_ID"] == "sheet_1"
    assert env_vars["DRIVE_SA_KEY_FILE"] == "/secrets/drive-sa.json"
    assert req["job"]["allocation_policy"]["instances"][0]["policy"]["machine_type"] == "n2d-standard-8"


def test_get_batch_job_status_with_short_id(monkeypatch) -> None:
    class _State:
        name = "RUNNING"

    job = SimpleNamespace(
        name="projects/proj_1/locations/us-east1/jobs/render-abc",
        status=SimpleNamespace(state=_State()),
        create_time="2026-01-01T00:00:00Z",
        update_time="2026-01-01T00:10:00Z",
    )

    class _Client:
        def get_job(self, request):
            assert request["name"] == "projects/proj_1/locations/us-east1/jobs/render-abc"
            return job

    monkeypatch.setenv("GCP_PROJECT_ID", "proj_1")
    monkeypatch.setenv("GCP_REGION", "us-east1")
    monkeypatch.setattr("so_sync.batch_trigger._batch_client", lambda: _Client())
    out = get_batch_job_status("render-abc")
    assert out["status"] == "running"
    assert out["batch_job_name"].endswith("/jobs/render-abc")
    assert out["batch_state"] == "running"

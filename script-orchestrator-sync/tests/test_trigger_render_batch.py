from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "trigger_render_batch.py"
_SPEC = importlib.util.spec_from_file_location("trigger_render_batch", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import script from {_SCRIPT_PATH}")
trigger = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(trigger)


def test_is_process_running_only_when_enabled_and_active() -> None:
    rows = [
        {"enabled": "TRUE", "status": "queued"},
        {"enabled": "FALSE", "status": "running"},
        {"enabled": "TRUE", "status": "running"},
    ]
    assert trigger.is_process_running(rows) is True


def test_is_process_running_false_when_no_enabled_active_rows() -> None:
    rows = [
        {"enabled": "TRUE", "status": "queued"},
        {"enabled": "TRUE", "status": "retry"},
        {"enabled": "FALSE", "status": "running"},
    ]
    assert trigger.is_process_running(rows) is False


def test_parse_queued_job_ids_dedupes_and_preserves_order() -> None:
    stdout = """
queued_render_job_id=cfg_abc_r01 manifest_drive_file_id=1
queued_render_job_id=cfg_abc_r02 manifest_drive_file_id=2
queued_render_job_id=cfg_abc_r01 manifest_drive_file_id=1
ignored=line
""".strip()
    assert trigger.parse_queued_job_ids(stdout) == ["cfg_abc_r01", "cfg_abc_r02"]


def test_compute_queue_progress_counts_statuses_and_percent() -> None:
    rows = [
        {"render_job_id": "cfg_abc_r01", "status": "success"},
        {"render_job_id": "cfg_abc_r02", "status": "failed"},
        {"render_job_id": "cfg_abc_r03", "status": "running"},
        {"render_job_id": "cfg_abc_r04", "status": "queued"},
        {"render_job_id": "cfg_abc_r05", "status": "retry"},
        {"render_job_id": "cfg_abc_r06", "status": "leased"},
    ]
    snapshot = trigger.compute_queue_progress(
        rows,
        ["cfg_abc_r01", "cfg_abc_r02", "cfg_abc_r03", "cfg_abc_r04", "cfg_abc_r05", "cfg_abc_r06"],
    )
    assert snapshot["total_jobs"] == 6
    assert snapshot["done_jobs"] == 2
    assert snapshot["success_jobs"] == 1
    assert snapshot["failed_jobs"] == 1
    assert snapshot["running_jobs"] == 2
    assert snapshot["queued_jobs"] == 2
    assert snapshot["percent"] == 33.33


def test_compute_queue_progress_empty_targets_is_complete() -> None:
    snapshot = trigger.compute_queue_progress([], [])
    assert snapshot["total_jobs"] == 0
    assert snapshot["done_jobs"] == 0
    assert snapshot["percent"] == 100.0

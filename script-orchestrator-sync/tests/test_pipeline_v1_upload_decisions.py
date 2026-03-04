from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_should_upload_artifact_when_no_existing_uploaded_row() -> None:
    rows = [
        {
            "run_id": "r1",
            "artifact_type": "pipeline_final_mp4",
            "filename": "final.mp4",
            "drive_file_id": "",
        }
    ]
    assert render_agent.should_upload_artifact(
        rows,
        run_id="r1",
        artifact_type="pipeline_final_mp4",
        filename="final.mp4",
    )


def test_should_upload_artifact_false_when_existing_uploaded_row_present() -> None:
    rows = [
        {
            "run_id": "r1",
            "artifact_type": "pipeline_final_mp4",
            "filename": "final.mp4",
            "drive_file_id": "drive_file_123",
        }
    ]
    assert not render_agent.should_upload_artifact(
        rows,
        run_id="r1",
        artifact_type="pipeline_final_mp4",
        filename="final.mp4",
    )


def test_build_pipeline_subfolder_name_defaults_to_campaign(monkeypatch) -> None:
    monkeypatch.delenv("PIPELINE_OUTPUT_SUBFOLDER_MODE", raising=False)
    assert render_agent.build_pipeline_subfolder_name("demo campaign", "run_abc") == "demo_campaign"


def test_build_pipeline_subfolder_name_run_mode(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_OUTPUT_SUBFOLDER_MODE", "run")
    assert render_agent.build_pipeline_subfolder_name("demo campaign", "run_abc") == "demo_campaign__run_abc"


def test_is_pipeline_final_video_artifact_keeps_shorts_and_conditional_final() -> None:
    assert render_agent._is_pipeline_final_video_artifact(
        artifact_type="pipeline_short_01",
        run_id="r1",
        run_ids_with_shorts={"r1"},
    )
    assert not render_agent._is_pipeline_final_video_artifact(
        artifact_type="pipeline_final_mp4",
        run_id="r1",
        run_ids_with_shorts={"r1"},
    )
    assert render_agent._is_pipeline_final_video_artifact(
        artifact_type="pipeline_final_mp4",
        run_id="r2",
        run_ids_with_shorts={"r1"},
    )


def test_collect_pipeline_cleanup_drive_file_ids_only_returns_nonfinal_pipeline_artifacts() -> None:
    rows = [
        {
            "render_job_id": "job1",
            "run_id": "r1",
            "artifact_type": "pipeline_short_01",
            "drive_file_id": "keep_short",
        },
        {
            "render_job_id": "job1",
            "run_id": "r1",
            "artifact_type": "pipeline_final_mp4",
            "drive_file_id": "delete_final_when_shorts_exist",
        },
        {
            "render_job_id": "job1",
            "run_id": "r1",
            "artifact_type": "pipeline_stitch_log",
            "drive_file_id": "delete_log",
        },
        {
            "render_job_id": "job1",
            "run_id": "r2",
            "artifact_type": "pipeline_final_mp4",
            "drive_file_id": "keep_final_without_shorts",
        },
        {
            "render_job_id": "job2",
            "run_id": "r1",
            "artifact_type": "pipeline_stitch_log",
            "drive_file_id": "other_job_ignored",
        },
    ]
    out = render_agent._collect_pipeline_cleanup_drive_file_ids(
        rows,
        render_job_id="job1",
        run_ids_with_shorts={"r1"},
    )
    assert out == ["delete_final_when_shorts_exist", "delete_log"]

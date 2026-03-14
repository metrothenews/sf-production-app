"""Render control-plane sheet contracts."""

from __future__ import annotations

from typing import List, Mapping

RENDER_JOBS_TAB = "Render_Jobs"
OUTPUTS_INDEX_TAB = "Outputs_Index"
RENDER_RUNS_TAB = "Render_Runs"

RENDER_STATUS_QUEUED = "queued"
RENDER_STATUS_RETRY = "retry"
RENDER_STATUS_LEASED = "leased"
RENDER_STATUS_RUNNING = "running"
RENDER_STATUS_SUCCESS = "success"
RENDER_STATUS_FAILED = "failed"

RENDER_CANONICAL_HEADERS: Mapping[str, List[str]] = {
    RENDER_JOBS_TAB: [
        "render_job_id",
        "enabled",
        "priority",
        "status",
        "campaign_id",
        "input_mode",
        "input_drive_folder_id",
        "input_manifest_drive_file_id",
        "audio_drive_file_id",
        "srt_drive_file_id",
        "renderer",
        "preset",
        "fps",
        "aspect",
        "output_mode",
        "output_drive_folder_id",
        "output_filename",
        "lease_owner",
        "lease_until_utc",
        "run_id",
        "attempts",
        "max_attempts",
        "last_error_utc",
        "created_utc",
        "updated_utc",
        "error_msg",
        "logs_drive_file_id",
        "input_video_path",
        "input_audio_path",
        "input_srt_path",
        "bg_mode",
        "output_local_path",
        "stitch_preset",
        "seed",
        "copy_srt_drive_file_id",
        "bpm",
    ],
    OUTPUTS_INDEX_TAB: [
        "output_id",
        "run_id",
        "render_job_id",
        "campaign_id",
        "artifact_type",
        "drive_file_id",
        "drive_folder_id",
        "filename",
        "bytes",
        "sha256",
        "created_utc",
        "notes",
    ],
    RENDER_RUNS_TAB: [
        "run_id",
        "run_type",
        "render_job_id",
        "status",
        "started_utc",
        "finished_utc",
        "dedupe_key",
        "error_msg",
        "artifacts_written",
        "created_utc",
        "updated_utc",
    ],
}

RENDER_HEADER_ALIASES: Mapping[str, List[str]] = {
    "render_job_id": ["render job id"],
    "input_drive_folder_id": ["input drive folder id"],
    "input_manifest_drive_file_id": ["input manifest drive file id"],
    "audio_drive_file_id": ["audio drive file id"],
    "srt_drive_file_id": ["srt drive file id"],
    "output_drive_folder_id": ["output drive folder id"],
    "output_filename": ["output filename"],
    "lease_until_utc": ["lease until utc"],
    "last_error_utc": ["last error utc"],
    "logs_drive_file_id": ["logs drive file id"],
    "drive_file_id": ["drive file id"],
    "drive_folder_id": ["drive folder id"],
    "created_utc": ["created utc"],
    "updated_utc": ["updated utc"],
    "run_type": ["run type"],
    "artifacts_written": ["artifacts written"],
    "target_count": ["n", "target count", "clip count", "number of outputs", "number_of_outputs"],
    "source_folder_ids": ["source folder ids", "folders", "drive folder ids"],
    "source_file_ids": ["source file ids", "files", "drive file ids"],
    "select_mode": ["select mode"],
    "select_seed": ["select seed"],
    "output_folder_id": ["output folder id"],
}

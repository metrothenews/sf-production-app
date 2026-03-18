from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_pipeline_workdir_paths_and_clip_filename_are_deterministic() -> None:
    paths = render_agent.build_pipeline_workdir_paths("run123", base_tmp_dir="/tmp/base")
    assert paths["root"] == "/tmp/base/so_work/run123"
    assert paths["inputs"] == "/tmp/base/so_work/run123/inputs"
    assert paths["clips"] == "/tmp/base/so_work/run123/inputs/clips"
    assert paths["outputs"] == "/tmp/base/so_work/run123/outputs"
    assert paths["logs"] == "/tmp/base/so_work/run123/logs"
    assert render_agent.build_clip_filename("A 1", "abc123") == "A_1__abc123.mp4"


def test_resolve_pipeline_v1_input_ids_manifest_wins_and_job_is_fallback() -> None:
    manifest = {
        "inputs": {
            "audio_drive_file_id": "audio_manifest",
            "lyrics_srt_drive_file_id": "lyrics_manifest",
            "copy_srt_drive_file_id": "copy_manifest",
            "clips": [{"drive_file_id": "clip1", "label": "c1", "category": "people"}],
        }
    }
    job = {
        "audio_drive_file_id": "audio_job",
        "srt_drive_file_id": "lyrics_job",
        "copy_srt_drive_file_id": "copy_job",
    }
    resolved = render_agent.resolve_pipeline_v1_input_ids(manifest, job)
    assert resolved["audio_drive_file_id"] == "audio_manifest"
    assert resolved["lyrics_srt_drive_file_id"] == "lyrics_manifest"
    assert resolved["copy_srt_drive_file_id"] == "copy_manifest"
    assert resolved["clips"] == [{"drive_file_id": "clip1", "label": "c1", "category": "P"}]


def test_resolve_pipeline_v1_input_ids_preserves_collection_num() -> None:
    resolved = render_agent.resolve_pipeline_v1_input_ids(
        {"inputs": {"clips": [{"drive_file_id": "clip3", "collection_num": "collection-8"}]}},
        {},
    )
    assert resolved["clips"] == [{"drive_file_id": "clip3", "label": "clip_001", "collection_num": "8"}]


def test_resolve_pipeline_v1_input_ids_uses_job_fallback_when_manifest_missing() -> None:
    resolved = render_agent.resolve_pipeline_v1_input_ids(
        {"inputs": {"clips": [{"drive_file_id": "clip2"}]}},
        {
            "audio_drive_file_id": "audio_job",
            "srt_drive_file_id": "lyrics_job",
            "copy_srt_drive_file_id": "copy_job",
        },
    )
    assert resolved["audio_drive_file_id"] == "audio_job"
    assert resolved["lyrics_srt_drive_file_id"] == "lyrics_job"
    assert resolved["copy_srt_drive_file_id"] == "copy_job"


def test_resolve_song_title_overlay_prefers_manifest() -> None:
    manifest = {"text": {"song_title": {"enabled": True, "text": "Manifest Song"}}}
    job = {"display_song_name": "FALSE", "song_name": "Job Song"}
    out = render_agent.resolve_song_title_overlay(manifest, job)
    assert out == {"enabled": True, "text": "Manifest Song"}


def test_resolve_song_title_overlay_can_fallback_to_job_fields() -> None:
    out = render_agent.resolve_song_title_overlay(
        {"text": {}},
        {"display_song_name": "TRUE", "song_name": "Job Song"},
    )
    assert out == {"enabled": True, "text": "Job Song"}

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


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


def test_resolve_pipeline_v1_input_ids_preserves_gcs_fields() -> None:
    manifest = {
        "inputs": {
            "clips": [
                {
                    "drive_file_id": "clip_gcs_1",
                    "gcs_uri": "gs://bucket/library-mirror/clips/collection-1/P/clip_gcs_1__a.mp4",
                    "gcs_object": "library-mirror/clips/collection-1/P/clip_gcs_1__a.mp4",
                }
            ]
        }
    }
    resolved = render_agent.resolve_pipeline_v1_input_ids(manifest, {})
    assert resolved["clips"] == [
        {
            "drive_file_id": "clip_gcs_1",
            "label": "clip_001",
            "gcs_uri": "gs://bucket/library-mirror/clips/collection-1/P/clip_gcs_1__a.mp4",
            "gcs_object": "library-mirror/clips/collection-1/P/clip_gcs_1__a.mp4",
        }
    ]


def test_resolve_pipeline_v1_input_ids_accepts_gcs_only_clip() -> None:
    manifest = {
        "inputs": {
            "clips": [{"gcs_uri": "gs://bucket/library-mirror/clips/collection-2/U/abc__b.mov"}]
        }
    }
    resolved = render_agent.resolve_pipeline_v1_input_ids(manifest, {})
    assert len(resolved["clips"]) == 1
    assert resolved["clips"][0]["label"] == "clip_001"
    assert resolved["clips"][0]["gcs_uri"] == "gs://bucket/library-mirror/clips/collection-2/U/abc__b.mov"
    assert resolved["clips"][0]["drive_file_id"].startswith("gcs_")


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


def test_resolve_and_download_pipeline_v1_inputs_uses_gcs_for_clip_media(tmp_path, monkeypatch) -> None:
    paths = {
        "root": str(tmp_path / "run"),
        "inputs": str(tmp_path / "run" / "inputs"),
        "clips": str(tmp_path / "run" / "inputs" / "clips"),
        "outputs": str(tmp_path / "run" / "outputs"),
        "logs": str(tmp_path / "run" / "logs"),
    }
    monkeypatch.setattr(render_agent, "build_pipeline_workdir_paths", lambda run_id, base_tmp_dir=None: paths)

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "inputs": {
                    "clips": [
                        {
                            "drive_file_id": "clip_drive_id_1",
                            "gcs_uri": "gs://bucket/library-mirror/clips/collection-1/P/clip_drive_id_1__a.mp4",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    audio_path = tmp_path / "audio.wav"
    lyrics_path = tmp_path / "lyrics.srt"
    audio_path.write_bytes(b"audio")
    lyrics_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nline\n", encoding="utf-8")

    calls: list[tuple[str, str, str]] = []

    def _fake_download_gcs(gcs_uri: str, dst_path: str) -> str:
        calls.append(("gcs", gcs_uri, dst_path))
        Path(dst_path).write_bytes(b"clip")
        return dst_path

    def _fake_download_drive(drive_id: str, dst_path: str, *, proxy_client=None, upload_client=None) -> str:
        _ = proxy_client
        _ = upload_client
        calls.append(("drive", drive_id, dst_path))
        Path(dst_path).write_bytes(b"clip")
        return dst_path

    monkeypatch.setattr(render_agent, "_download_gcs_uri", _fake_download_gcs)
    monkeypatch.setattr(render_agent, "_download_with_fallback", _fake_download_drive)

    out = render_agent.resolve_and_download_pipeline_v1_inputs(
        {
            "input_manifest_path": str(manifest_path),
            "input_audio_path": str(audio_path),
            "input_srt_path": str(lyrics_path),
        },
        "run_gcs_1",
    )
    assert len(out["clip_paths"]) == 1
    assert calls
    assert calls[0][0] == "gcs"
    assert not any(call[0] == "drive" for call in calls)


def test_resolve_and_download_pipeline_v1_inputs_fails_fast_on_gcs_error(tmp_path, monkeypatch) -> None:
    paths = {
        "root": str(tmp_path / "run"),
        "inputs": str(tmp_path / "run" / "inputs"),
        "clips": str(tmp_path / "run" / "inputs" / "clips"),
        "outputs": str(tmp_path / "run" / "outputs"),
        "logs": str(tmp_path / "run" / "logs"),
    }
    monkeypatch.setattr(render_agent, "build_pipeline_workdir_paths", lambda run_id, base_tmp_dir=None: paths)

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "inputs": {
                    "clips": [
                        {
                            "gcs_uri": "gs://bucket/library-mirror/clips/collection-2/U/clip_2__b.mp4",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    audio_path = tmp_path / "audio.wav"
    lyrics_path = tmp_path / "lyrics.srt"
    audio_path.write_bytes(b"audio")
    lyrics_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nline\n", encoding="utf-8")

    drive_calls = {"count": 0}

    def _fake_download_drive(drive_id: str, dst_path: str, *, proxy_client=None, upload_client=None) -> str:
        _ = drive_id
        _ = dst_path
        _ = proxy_client
        _ = upload_client
        drive_calls["count"] += 1
        return dst_path

    monkeypatch.setattr(render_agent, "_download_with_fallback", _fake_download_drive)
    monkeypatch.setattr(render_agent, "_download_gcs_uri", lambda gcs_uri, dst_path: (_ for _ in ()).throw(RuntimeError("missing mirror object")))

    with pytest.raises(RuntimeError, match=r"missing mirror object"):
        render_agent.resolve_and_download_pipeline_v1_inputs(
            {
                "input_manifest_path": str(manifest_path),
                "input_audio_path": str(audio_path),
                "input_srt_path": str(lyrics_path),
            },
            "run_gcs_fail",
        )
    assert drive_calls["count"] == 0

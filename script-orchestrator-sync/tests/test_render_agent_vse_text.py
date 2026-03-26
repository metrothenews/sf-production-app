from __future__ import annotations

import hashlib
import importlib.util
import tempfile
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_build_vse_text_command_uses_blender_bin_and_arg_order() -> None:
    job = {
        "input_video_path": "/tmp/in/video.mp4",
        "input_srt_path": "/tmp/in/lyrics.srt",
        "input_audio_path": "/tmp/in/master.wav",
        "fps": "30",
        "aspect": "9:16",
        "preset": "brat_words_manual_lightgrey_narrow_pixelate_v007",
        "bg_mode": "none",
        "output_local_path": "/tmp/out/final.mp4",
    }
    blender_bin = "/opt/blender/blender"
    script_path = "/abs/vse_bpm_preset_deterministic_bl456.py"

    cmd = render_agent.build_vse_text_command(job, blender_bin=blender_bin, script_path=script_path)

    assert cmd[0] == blender_bin
    assert cmd[1:5] == ["-b", "--python", script_path, "--"]
    assert cmd[5:] == [
        "--video",
        "/tmp/in/video.mp4",
        "--srt",
        "/tmp/in/lyrics.srt",
        "--audio",
        "/tmp/in/master.wav",
        "--fps",
        "30",
        "--aspect",
        "9:16",
        "--preset",
        "brat_words_manual_lightgrey_narrow_pixelate_v007",
        "--bg-mode",
        "none",
        "--output",
        "/tmp/out/final.mp4",
    ]


def test_build_outputs_rows_for_vse_text_contains_run_and_artifact_metadata() -> None:
    run_id = "run_123"
    render_job_id = "render_456"
    expected_keys = {
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
    }
    with tempfile.TemporaryDirectory() as td:
        output_mp4_path = str(Path(td) / "final.mp4")
        log_path = str(Path(td) / "run.log")
        mp4_bytes = b"\x00\x01mp4-bytes"
        log_bytes = b"log line one\nlog line two\n"
        Path(output_mp4_path).write_bytes(mp4_bytes)
        Path(log_path).write_bytes(log_bytes)

        rows = render_agent.build_outputs_rows_for_vse_text(
            run_id=run_id,
            render_job_id=render_job_id,
            output_mp4_path=output_mp4_path,
            log_path=log_path,
            campaign_id="camp_001",
            created_utc="2026-03-02T00:00:00+00:00",
        )

        assert len(rows) == 2
        assert all(set(r.keys()) == expected_keys for r in rows)
        assert all(r["run_id"] == run_id for r in rows)
        assert all(r["render_job_id"] == render_job_id for r in rows)

        mp4_row = next(r for r in rows if r["artifact_type"] == "vse_text_mp4")
        log_row = next(r for r in rows if r["artifact_type"] == "vse_text_log")

        assert mp4_row["bytes"] == len(mp4_bytes)
        assert mp4_row["sha256"] == hashlib.sha256(mp4_bytes).hexdigest()
        assert output_mp4_path in str(mp4_row["notes"])

        assert log_row["bytes"] == len(log_bytes)
        assert log_row["sha256"] == hashlib.sha256(log_bytes).hexdigest()
        assert log_path in str(log_row["notes"])


def test_strict_lyrics_overlay_enabled_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIPELINE_STRICT_LYRICS_OVERLAY", raising=False)
    assert render_agent._strict_lyrics_overlay_enabled() is True


def test_strict_lyrics_overlay_enabled_supports_disable_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINE_STRICT_LYRICS_OVERLAY", "0")
    assert render_agent._strict_lyrics_overlay_enabled() is False
    monkeypatch.setenv("PIPELINE_STRICT_LYRICS_OVERLAY", "off")
    assert render_agent._strict_lyrics_overlay_enabled() is False


def test_handle_lyrics_stage_fallback_raises_when_strict_and_nonempty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINE_STRICT_LYRICS_OVERLAY", "1")
    with pytest.raises(RuntimeError, match="non-empty lyric cues"):
        render_agent._handle_lyrics_stage_fallback(
            stage="lyrics:test.mp4",
            cut_idx=1,
            filename="test.mp4",
            reason="missing_or_empty_lyrics_output",
            lyrics_cues=3,
            source_path="/tmp/base.mp4",
            output_path="/tmp/lyrics.mp4",
            log_path="/tmp/lyrics.log",
            strict_on_nonempty=True,
        )


def test_handle_lyrics_stage_fallback_no_raise_for_empty_or_non_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINE_STRICT_LYRICS_OVERLAY", "1")
    render_agent._handle_lyrics_stage_fallback(
        stage="lyrics:test.mp4",
        cut_idx=1,
        filename="test.mp4",
        reason="empty_lyrics_srt",
        lyrics_cues=0,
        source_path="/tmp/base.mp4",
        output_path="/tmp/lyrics.mp4",
        log_path="/tmp/lyrics.log",
        strict_on_nonempty=False,
    )
    monkeypatch.setenv("PIPELINE_STRICT_LYRICS_OVERLAY", "0")
    render_agent._handle_lyrics_stage_fallback(
        stage="lyrics:test.mp4",
        cut_idx=1,
        filename="test.mp4",
        reason="missing_or_empty_lyrics_output",
        lyrics_cues=5,
        source_path="/tmp/base.mp4",
        output_path="/tmp/lyrics.mp4",
        log_path="/tmp/lyrics.log",
        strict_on_nonempty=True,
    )

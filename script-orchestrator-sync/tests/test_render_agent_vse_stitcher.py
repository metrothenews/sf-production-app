from __future__ import annotations

import hashlib
import importlib.util
import tempfile
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_build_vse_stitcher_command_arg_ordering() -> None:
    job = {
        "media_root_path": "/tmp/in/media_root",
        "input_audio_path": "/tmp/in/master.wav",
        "bpm": "120",
        "fps": "30",
        "aspect": "9:16",
        "stitch_preset": "4a4b4c",
        "seed": "42",
        "output_local_path": "/tmp/out/base.mp4",
    }
    blender_bin = "/opt/blender/blender"
    script_path = "/abs/vse_stitcher.py"

    cmd = render_agent.build_vse_stitcher_command(job, blender_bin=blender_bin, script_path=script_path)

    assert cmd[0] == blender_bin
    assert cmd[1:5] == ["-b", "--python", script_path, "--"]
    assert cmd[5:] == [
        "--media-root",
        "/tmp/in/media_root",
        "--audio",
        "/tmp/in/master.wav",
        "--bpm",
        "120",
        "--fps",
        "30",
        "--aspect",
        "9:16",
        "--preset",
        "4a4b4c",
        "--seed",
        "42",
        "--output",
        "/tmp/out/base.mp4",
    ]


def test_build_vse_stitcher_command_includes_effects_pass_flags() -> None:
    cmd = render_agent.build_vse_stitcher_command(
        {
            "media_root_path": "/tmp/in/media_root",
            "input_audio_path": "/tmp/in/master.wav",
            "bpm": "136",
            "fps": "30",
            "aspect": "9:16",
            "stitch_preset": "4p4sbc4f4t2sp2u2c2wm",
            "seed": "99",
            "output_local_path": "/tmp/out/base.mp4",
            "effects_pass1_mode": "base_only",
            "effects_plan_out": "/tmp/out/cut_01_effects_plan.json",
            "run_id": "run_123",
            "cut_idx": 1,
        },
        blender_bin="/opt/blender/blender",
        script_path="/abs/vse_stitcher.py",
    )
    assert "--effects-pass1-mode" in cmd
    assert "--effects-plan-out" in cmd
    assert "--run-id" in cmd
    assert "--cut-idx" in cmd
    assert cmd[cmd.index("--effects-pass1-mode") + 1] == "base_only"
    assert cmd[cmd.index("--effects-plan-out") + 1] == "/tmp/out/cut_01_effects_plan.json"
    assert cmd[cmd.index("--run-id") + 1] == "run_123"
    assert cmd[cmd.index("--cut-idx") + 1] == "1"


def test_build_vse_effects_command_arg_ordering() -> None:
    cmd = render_agent.build_vse_effects_command(
        {
            "input_video_path": "/tmp/out/base.mp4",
            "input_audio_path": "/tmp/in/master.wav",
            "effects_plan_path": "/tmp/out/cut_01_effects_plan.json",
            "fps": "30",
            "aspect": "9:16",
            "output_local_path": "/tmp/out/effects.mp4",
            "render_end_seconds": 12.34,
        },
        blender_bin="/opt/blender/blender",
        script_path="/abs/vse_effects_pass.py",
    )
    assert cmd[0] == "/opt/blender/blender"
    assert cmd[1:5] == ["-b", "--python", "/abs/vse_effects_pass.py", "--"]
    assert cmd[5:17] == [
        "--input-video",
        "/tmp/out/base.mp4",
        "--audio",
        "/tmp/in/master.wav",
        "--effects-plan",
        "/tmp/out/cut_01_effects_plan.json",
        "--fps",
        "30",
        "--aspect",
        "9:16",
        "--output",
        "/tmp/out/effects.mp4",
    ]
    assert "--render-end-seconds" in cmd


def test_build_pipeline_subfolder_name_defaults_to_campaign(monkeypatch) -> None:
    monkeypatch.delenv("PIPELINE_OUTPUT_SUBFOLDER_MODE", raising=False)
    out = render_agent.build_pipeline_subfolder_name("Test Video 001", "run123")
    assert out == "Test_Video_001"


def test_build_outputs_rows_for_vse_stitcher_full_schema_and_hashes() -> None:
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
        mp4_path = str(Path(td) / "base.mp4")
        log_path = str(Path(td) / "stitch.log")
        mp4_bytes = b"\x00\x02stitched"
        log_bytes = b"stitch log\n"
        Path(mp4_path).write_bytes(mp4_bytes)
        Path(log_path).write_bytes(log_bytes)

        rows = render_agent.build_outputs_rows_for_vse_stitcher(
            run_id="run_1",
            render_job_id="job_1",
            output_mp4_path=mp4_path,
            log_path=log_path,
            campaign_id="camp_1",
            created_utc="2026-03-02T00:00:00+00:00",
        )
        assert len(rows) == 2
        assert all(set(r.keys()) == expected_keys for r in rows)

        mp4_row = next(r for r in rows if r["artifact_type"] == "vse_stitcher_mp4")
        log_row = next(r for r in rows if r["artifact_type"] == "vse_stitcher_log")
        assert mp4_row["bytes"] == len(mp4_bytes)
        assert mp4_row["sha256"] == hashlib.sha256(mp4_bytes).hexdigest()
        assert mp4_path in str(mp4_row["notes"])
        assert log_row["bytes"] == len(log_bytes)
        assert log_row["sha256"] == hashlib.sha256(log_bytes).hexdigest()
        assert log_path in str(log_row["notes"])

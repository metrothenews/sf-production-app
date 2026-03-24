from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_pipeline_v1_stage_plan_without_copy() -> None:
    manifest = {"copy_pass": {"enabled": False}}
    resolved_inputs = {"copy_srt_path": ""}
    assert render_agent.plan_pipeline_v1_stages(manifest, resolved_inputs) == ["stitch", "lyrics"]


def test_pipeline_v1_stage_plan_with_copy() -> None:
    manifest = {"copy_pass": {"enabled": True}}
    resolved_inputs = {"copy_srt_path": "/tmp/copy.srt"}
    assert render_agent.plan_pipeline_v1_stages(manifest, resolved_inputs) == ["stitch", "lyrics", "copy"]


def test_resolve_pipeline_edit_mode_precedence() -> None:
    manifest = {"edit_mode": "performance"}
    assert render_agent.resolve_pipeline_edit_mode(manifest, {"edit_mode": "found_footage"}) == "found_footage"
    assert render_agent.resolve_pipeline_edit_mode(manifest, {}) == "performance"
    assert render_agent.resolve_pipeline_edit_mode({}, {}) == "found_footage"


def test_parse_performance_preset_tokens() -> None:
    out = render_agent.parse_performance_preset_tokens("4a2br2cw")
    assert out[0] == {"duration": 4, "clip": "a", "modifiers": ""}
    assert out[1] == {"duration": 2, "clip": "b", "modifiers": "r"}
    assert out[2] == {"duration": 2, "clip": "c", "modifiers": "w"}


def test_build_vse_performance_command_contains_clip_args(tmp_path: Path) -> None:
    out_mp4 = tmp_path / "out.mp4"
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    cmd = render_agent.build_vse_performance_command(
        {
            "input_audio_path": "/tmp/audio.wav",
            "fps": "30",
            "aspect": "9:16",
            "performance_preset": "4a2br2cw",
            "seed": "42",
            "timeline_offset_seconds": "7.23",
            "sync_mode": "timeline_locked",
            "short_clip_policy": "wrap_loop",
            "output_local_path": str(out_mp4),
            "clip_map": {"a": str(clip_a), "b": str(clip_b)},
            "render_end_seconds": 12.4,
        },
        blender_bin="blender",
        script_path="/tmp/script.py",
    )
    assert "--clip-a" in cmd
    assert "--clip-b" in cmd
    assert "--preset" in cmd
    assert "4a2br2cw" in cmd
    assert "--render-end-seconds" in cmd
    assert "--timeline-offset-seconds" in cmd
    assert "--sync-mode" in cmd
    assert "--short-clip-policy" in cmd

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "vse-stitcher"
    / "scripts"
    / "vse_performance_preset_deterministic_bl456.py"
)
_SPEC = importlib.util.spec_from_file_location("vse_performance_preset_deterministic_bl456", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import script from {_SCRIPT_PATH}")
perf = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name or "vse_performance_preset_deterministic_bl456"] = perf
_SPEC.loader.exec_module(perf)


def test_extract_user_args_from_blender_argv() -> None:
    argv = ["blender", "-b", "--python", "script.py", "--", "--audio", "a.wav", "--fps", "30"]
    out = perf._extract_user_args_from_blender_argv(argv)
    assert out == ["--audio", "a.wav", "--fps", "30"]


def test_allocate_token_frames_sums_exact() -> None:
    tokens = [
        {"duration": 4, "clip": "a", "modifiers": ""},
        {"duration": 2, "clip": "b", "modifiers": "r"},
        {"duration": 2, "clip": "c", "modifiers": "w"},
    ]
    out = perf._allocate_token_frames(tokens, 477)
    assert sum(out) == 477
    assert all(v >= 1 for v in out)


def test_allocate_token_frames_rejects_impossible() -> None:
    tokens = [
        {"duration": 1, "clip": "a", "modifiers": ""},
        {"duration": 1, "clip": "b", "modifiers": ""},
        {"duration": 1, "clip": "c", "modifiers": ""},
    ]
    with pytest.raises(RuntimeError, match=r"Token allocation impossible"):
        perf._allocate_token_frames(tokens, 2)


def test_token_start_frames_from_timeline_offset() -> None:
    frames = [238, 120, 119]
    starts = perf._token_start_frames(217, frames)
    assert starts == [217, 455, 575]


def test_plan_source_extraction_wrap_loop_crosses_clip_end() -> None:
    plan = perf._plan_source_extraction(
        clip_total_frames=100,
        absolute_start_frame=95,
        frame_count=20,
        short_clip_policy="wrap_loop",
    )
    assert [(s.start_frame, s.frame_count) for s in plan.spans] == [(95, 5), (0, 15)]
    assert plan.wraps_before == 0
    assert plan.wraps_in_token == 1
    assert plan.pad_frames == 0


def test_plan_source_extraction_wrap_loop_uses_absolute_offset() -> None:
    plan = perf._plan_source_extraction(
        clip_total_frames=100,
        absolute_start_frame=250,
        frame_count=20,
        short_clip_policy="wrap_loop",
    )
    assert [(s.start_frame, s.frame_count) for s in plan.spans] == [(50, 20)]
    assert plan.wraps_before == 2
    assert plan.wraps_in_token == 0


def test_flash_replacement_preserves_token_duration_in_frames() -> None:
    token_frames = 120
    flash_frames = perf._flash_frame_count("b", token_frames, 30)
    clip_frames = token_frames - flash_frames
    assert flash_frames > 0
    assert flash_frames < token_frames
    assert clip_frames + flash_frames == token_frames


def test_reverse_wrap_plan_preserves_requested_frame_count() -> None:
    token_frames = 40
    flash_frames = perf._flash_frame_count("r", token_frames, 30)
    plan = perf._plan_source_extraction(
        clip_total_frames=30,
        absolute_start_frame=28 + flash_frames,
        frame_count=token_frames - flash_frames,
        short_clip_policy="wrap_loop",
    )
    assert sum(s.frame_count for s in plan.spans) == token_frames - flash_frames

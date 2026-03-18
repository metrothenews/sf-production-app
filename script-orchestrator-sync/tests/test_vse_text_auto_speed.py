from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load_vse_text_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "vse-text"
        / "scripts"
        / "vse_bpm_preset_deterministic_bl456.py"
    )
    if "bpy" not in sys.modules:
        bpy_stub = types.ModuleType("bpy")
        bpy_stub.types = types.SimpleNamespace(Scene=object, Sequence=object, SequenceEditor=object)
        bpy_stub.app = types.SimpleNamespace(
            build_options=types.SimpleNamespace(codec_ffmpeg=True),
        )
        sys.modules["bpy"] = bpy_stub
    spec = importlib.util.spec_from_file_location("vse_text_script_auto_speed", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import vse text script from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vse_text = _load_vse_text_module()


def test_auto_speed_baseline_quantile_from_cues() -> None:
    cues = [
        vse_text.Cue(idx=1, start_ms=0, end_ms=2000, text="one two three four"),  # 2.0 wps
        vse_text.Cue(idx=2, start_ms=0, end_ms=3000, text="one two three four five six"),  # 2.0 wps
        vse_text.Cue(idx=3, start_ms=0, end_ms=2000, text="one two three four five six seven eight"),  # 4.0 wps
    ]
    baseline = vse_text._compute_auto_speed_baseline_wps(
        cues,
        baseline_quantile=0.70,
        min_words_for_baseline=3,
        fallback_baseline_wps=2.5,
    )
    assert baseline == pytest.approx(2.8, rel=1e-3)


def test_auto_speed_baseline_uses_fallback_when_insufficient_words() -> None:
    cues = [
        vse_text.Cue(idx=1, start_ms=0, end_ms=2000, text="one two"),
        vse_text.Cue(idx=2, start_ms=0, end_ms=2500, text="three four"),
    ]
    baseline = vse_text._compute_auto_speed_baseline_wps(
        cues,
        baseline_quantile=0.70,
        min_words_for_baseline=3,
        fallback_baseline_wps=2.5,
    )
    assert baseline == pytest.approx(2.5, rel=1e-6)


def test_auto_speed_multiplier_behavior() -> None:
    at_baseline = vse_text._compute_auto_reveal_speed_multiplier(
        words=6,
        duration_ms=3000,  # 2.0 wps
        baseline_wps=2.0,
        max_speedup=2.0,
        gamma=1.0,
    )
    sparse_half_density = vse_text._compute_auto_reveal_speed_multiplier(
        words=2,
        duration_ms=2000,  # 1.0 wps
        baseline_wps=2.0,
        max_speedup=2.0,
        gamma=1.0,
    )
    dense = vse_text._compute_auto_reveal_speed_multiplier(
        words=6,
        duration_ms=2000,  # 3.0 wps
        baseline_wps=2.0,
        max_speedup=2.0,
        gamma=1.0,
    )
    assert at_baseline == pytest.approx(1.0, rel=1e-6)
    assert sparse_half_density == pytest.approx(2.0, rel=1e-6)
    assert dense == pytest.approx(1.0, rel=1e-6)


def test_reveal_window_and_word_start_bounds() -> None:
    total_frames = 100
    reveal_frames = vse_text._compute_reveal_window_frames(total_frames=total_frames, speed=2.0)
    assert reveal_frames == 50

    starts = [
        vse_text._compute_word_reveal_start_frame(
            start_f=10,
            end_f_excl=110,
            reveal_frames=reveal_frames,
            n_words=10,
            word_index=i,
        )
        for i in range(10)
    ]
    assert starts[0] == 10
    assert starts[-1] == 55
    assert all(starts[i] <= starts[i + 1] for i in range(len(starts) - 1))
    assert all(10 <= s < 110 for s in starts)

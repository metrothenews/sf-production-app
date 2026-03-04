from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compile_config_to_render_job.py"
_SPEC = importlib.util.spec_from_file_location("compile_config_to_render_job", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import compile script from {_SCRIPT_PATH}")
compiler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compiler)


def test_build_cuts_from_clip_slots() -> None:
    cfg = {
        "Cut Clips?": "TRUE",
        "Clip 1": "TRUE",
        "Clip 1 Start": "00:00:12:44",
        "Clip 1 End": "00:00:24:48",
        "Clip 2": "TRUE",
        "Clip 2 Start": "00:00:26:12",
        "Clip 2 End": "00:00:32:24",
        "Clip 3": "FALSE",
    }
    cuts = compiler.build_cuts_from_config(cfg, naming_convention="Test_Video_v1_000")
    assert len(cuts) == 2
    assert cuts[0]["start"] == "00:00:12.440"
    assert cuts[0]["end"] == "00:00:24.480"
    assert cuts[0]["filename"] == "Test_Video_v1_001.mp4"
    assert cuts[1]["filename"] == "Test_Video_v1_002.mp4"


def test_build_cuts_respects_start_index_offset() -> None:
    cfg = {
        "Cut Clips?": "TRUE",
        "Clip 1": "TRUE",
        "Clip 1 Start": "00:00:12:44",
        "Clip 1 End": "00:00:24:48",
        "Clip 2": "TRUE",
        "Clip 2 Start": "00:00:26:12",
        "Clip 2 End": "00:00:32:24",
    }
    cuts = compiler.build_cuts_from_config(cfg, naming_convention="Test_Video_v1_000", start_index=4)
    assert cuts[0]["filename"] == "Test_Video_v1_004.mp4"
    assert cuts[1]["filename"] == "Test_Video_v1_005.mp4"


def test_build_cuts_allows_template_index_zero_when_requested() -> None:
    cfg = {
        "Cut Clips?": "TRUE",
        "Clip 1": "TRUE",
        "Clip 1 Start": "00:00:12:44",
        "Clip 1 End": "00:00:24:48",
    }
    cuts = compiler.build_cuts_from_config(cfg, naming_convention="Test_Video_v1_000", start_index=0)
    assert cuts[0]["filename"] == "Test_Video_v1_000.mp4"


def test_build_cuts_rejects_end_before_start() -> None:
    cfg = {
        "Cut Clips?": "TRUE",
        "Clip 1": "TRUE",
        "Clip 1 Start": "00:00:24:48",
        "Clip 1 End": "00:00:12:44",
    }
    with pytest.raises(ValueError):
        compiler.build_cuts_from_config(cfg, naming_convention="Test_Video_v1_000")

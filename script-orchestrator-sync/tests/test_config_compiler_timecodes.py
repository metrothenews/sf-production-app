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


def test_hhmmssff_to_ffmpeg_ts_examples() -> None:
    assert compiler.parse_hhmmssff_to_ffmpeg_ts("00:00:12:44") == "00:00:12.440"
    assert compiler.parse_hhmmssff_to_ffmpeg_ts("00:00:24:48") == "00:00:24.480"


def test_hhmmssff_rejects_invalid_ff() -> None:
    with pytest.raises(ValueError):
        compiler.parse_hhmmssff_to_ffmpeg_ts("00:00:12:100")

from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_build_ffmpeg_cut_command() -> None:
    cmd = render_agent.build_ffmpeg_cut_command(
        ffmpeg_bin="ffmpeg",
        input_path="/tmp/final.mp4",
        start="00:00:05.000",
        end="00:00:12.000",
        output_path="/tmp/short_01.mp4",
    )
    assert cmd == [
        "ffmpeg",
        "-y",
        "-ss",
        "00:00:05.000",
        "-to",
        "00:00:12.000",
        "-i",
        "/tmp/final.mp4",
        "-c",
        "copy",
        "/tmp/short_01.mp4",
    ]

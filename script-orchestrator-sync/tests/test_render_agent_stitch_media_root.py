from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_extract_stitch_categories_from_exec_preset() -> None:
    out = render_agent._extract_stitch_categories("1p1u1f1c1m1t")
    assert out == ["P", "U", "F", "C", "M", "T"]


def test_build_stitcher_media_root_mirrors_clips_into_preset_categories(tmp_path: Path) -> None:
    clip = tmp_path / "inputs" / "clip1.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"x")
    root = render_agent._build_stitcher_media_root(
        [str(clip)],
        str(tmp_path / "media_root"),
        stitch_preset="2p4u2c",
    )
    media_root = Path(root)
    assert (media_root / "P" / f"001_{clip.name}").exists()
    assert (media_root / "U" / f"001_{clip.name}").exists()
    assert (media_root / "C" / f"001_{clip.name}").exists()
    # Legacy fallback buckets are still present.
    assert (media_root / "A" / f"001_{clip.name}").exists()
    assert (media_root / "B" / f"001_{clip.name}").exists()


def test_parse_ffmpeg_timestamp_to_seconds() -> None:
    assert render_agent._parse_ffmpeg_timestamp_to_seconds("00:00:12.440") == 12.44
    assert render_agent._parse_ffmpeg_timestamp_to_seconds("01:02:03.004") == 3723.004

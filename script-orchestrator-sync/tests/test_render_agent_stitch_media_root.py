from __future__ import annotations

import importlib.util
from pathlib import Path
import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_extract_stitch_categories_from_exec_preset() -> None:
    out = render_agent._extract_stitch_categories("1p1u1f1c1m1t")
    assert out == ["P", "U", "F", "C", "M", "T"]


def test_build_stitcher_media_root_routes_by_clip_category(tmp_path: Path) -> None:
    clip_p = tmp_path / "inputs" / "people.mp4"
    clip_u = tmp_path / "inputs" / "urban.mp4"
    clip_p.parent.mkdir(parents=True, exist_ok=True)
    clip_p.write_bytes(b"x")
    clip_u.write_bytes(b"y")
    root = render_agent._build_stitcher_media_root(
        [
            {"path": str(clip_p), "category": "P"},
            {"path": str(clip_u), "category": "U"},
        ],
        str(tmp_path / "media_root"),
        stitch_preset="2p4u",
    )
    media_root = Path(root)
    assert (media_root / "P" / f"001_{clip_p.name}").exists()
    assert (media_root / "U" / f"001_{clip_u.name}").exists()


def test_build_stitcher_media_root_uses_uncategorized_fallback(tmp_path: Path) -> None:
    clip = tmp_path / "inputs" / "clip1.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"x")
    root = render_agent._build_stitcher_media_root(
        [str(clip)],
        str(tmp_path / "media_root"),
        stitch_preset="2p4u",
    )
    media_root = Path(root)
    assert (media_root / "P" / f"001_{clip.name}").exists()
    assert (media_root / "U" / f"001_{clip.name}").exists()


def test_build_stitcher_media_root_errors_when_required_category_missing(tmp_path: Path) -> None:
    clip = tmp_path / "inputs" / "people.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"x")
    with pytest.raises(RuntimeError, match=r"No clips found for stitch preset category 'U'"):
        render_agent._build_stitcher_media_root(
            [{"path": str(clip), "category": "P"}],
            str(tmp_path / "media_root"),
            stitch_preset="2p4u",
        )


def test_parse_ffmpeg_timestamp_to_seconds() -> None:
    assert render_agent._parse_ffmpeg_timestamp_to_seconds("00:00:12.440") == 12.44
    assert render_agent._parse_ffmpeg_timestamp_to_seconds("01:02:03.004") == 3723.004

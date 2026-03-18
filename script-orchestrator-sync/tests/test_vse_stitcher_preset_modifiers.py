from __future__ import annotations

import importlib.util
import os
import random
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "vse-stitcher"
    / "scripts"
    / "vse_bpm_preset_deterministic_bl456.py"
)
_EFFECTS_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "vse-stitcher"
    / "scripts"
    / "vse_effects_pass_deterministic_bl456.py"
)
if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace(
        types=types.SimpleNamespace(Scene=object, Sequence=object, SequenceEditor=object)
    )
_SPEC = importlib.util.spec_from_file_location("vse_stitcher_script", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import stitcher script from {_SCRIPT_PATH}")
stitcher = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = stitcher
_SPEC.loader.exec_module(stitcher)

_FX_SPEC = importlib.util.spec_from_file_location("vse_effects_script", _EFFECTS_SCRIPT_PATH)
if _FX_SPEC is None or _FX_SPEC.loader is None:
    raise RuntimeError(f"Unable to import effects script from {_EFFECTS_SCRIPT_PATH}")
effects = importlib.util.module_from_spec(_FX_SPEC)
sys.modules[_FX_SPEC.name] = effects
_FX_SPEC.loader.exec_module(effects)


def test_parse_preset_supports_new_modifiers() -> None:
    steps = stitcher.parse_preset("4p4sbc4f4t2sp2u2c2wm")
    got = [(s.rate, s.category, s.modifier) for s in steps]
    assert got == [
        (4, "p", ""),
        (4, "c", "sb"),
        (4, "f", ""),
        (4, "t", ""),
        (2, "p", "s"),
        (2, "u", ""),
        (2, "c", ""),
        (2, "m", "w"),
    ]


def test_parse_preset_keeps_legacy_tokens_valid() -> None:
    steps = stitcher.parse_preset("4a4b4c")
    assert [(s.rate, s.category, s.modifier) for s in steps] == [
        (4, "a", ""),
        (4, "b", ""),
        (4, "c", ""),
    ]


@pytest.mark.parametrize("bad_preset", ["4sxp", "4sbwp", "4swbp", "4bsm", "4wsm"])
def test_parse_preset_rejects_unknown_modifiers(bad_preset: str) -> None:
    with pytest.raises(ValueError, match=r"Unsupported modifier"):
        stitcher.parse_preset(bad_preset)


def test_step_mode_mapping() -> None:
    assert stitcher._step_mode_from_modifier("") == "normal"
    assert stitcher._step_mode_from_modifier("b") == "flash_black"
    assert stitcher._step_mode_from_modifier("w") == "flash_white"
    assert stitcher._step_mode_from_modifier("s") == "stutter_two"
    assert stitcher._step_mode_from_modifier("sb") == "stutter_black"
    assert stitcher._step_mode_from_modifier("sw") == "stutter_white"


def test_build_frame_ops_for_stutter_black_alternates_color_then_source() -> None:
    ops = stitcher._build_frame_ops_for_segment(
        segment_index=0,
        mode="stutter_black",
        start=10,
        end_excl=16,
        primary_clip_path="/tmp/p.mov",
        secondary_clip_path="",
        primary_source_frames=4,
        secondary_source_frames=0,
        primary_in_start=1,
        secondary_in_start=0,
    )
    assert len(ops) == 6
    assert [op["timeline_frame"] for op in ops] == [10, 11, 12, 13, 14, 15]
    assert [op["op_type"] for op in ops] == [
        "replace_color",
        "replace_source",
        "replace_color",
        "replace_source",
        "replace_color",
        "replace_source",
    ]
    source_frames = [op["source_frame"] for op in ops if op["op_type"] == "replace_source"]
    assert source_frames == [1, 2, 3]


def test_build_frame_ops_for_stutter_two_uses_primary_then_secondary() -> None:
    ops = stitcher._build_frame_ops_for_segment(
        segment_index=1,
        mode="stutter_two",
        start=1,
        end_excl=7,
        primary_clip_path="/tmp/a.mov",
        secondary_clip_path="/tmp/b.mov",
        primary_source_frames=3,
        secondary_source_frames=2,
        primary_in_start=0,
        secondary_in_start=1,
    )
    assert len(ops) == 6
    assert [op["source_role"] for op in ops] == [
        "primary",
        "secondary",
        "primary",
        "secondary",
        "primary",
        "secondary",
    ]
    assert [op["source_frame"] for op in ops] == [0, 1, 1, 0, 2, 1]


def test_validate_unique_frame_ops_rejects_duplicates() -> None:
    with pytest.raises(RuntimeError, match=r"Duplicate frame op"):
        stitcher._validate_unique_frame_ops(
            [
                {"timeline_frame": 5, "op_type": "replace_color"},
                {"timeline_frame": 5, "op_type": "replace_source"},
            ]
        )


def test_compute_timeline_anchored_window_in_start_zero() -> None:
    frame_start, off_start, off_end, visible = stitcher._compute_timeline_anchored_window(
        start=17,
        end_excl=18,
        in_start=0,
        source_total=29,
    )
    assert (frame_start, off_start, off_end, visible) == (17, 0, 28, 1)


def test_compute_timeline_anchored_window_in_start_nonzero() -> None:
    frame_start, off_start, off_end, visible = stitcher._compute_timeline_anchored_window(
        start=17,
        end_excl=18,
        in_start=1,
        source_total=29,
    )
    assert (frame_start, off_start, off_end, visible) == (16, 1, 27, 1)
    # Timeline placement remains anchored at frame 17 despite source offset.
    assert frame_start + off_start == 17


def test_compute_timeline_anchored_window_is_idempotent() -> None:
    first = stitcher._compute_timeline_anchored_window(
        start=123,
        end_excl=124,
        in_start=7,
        source_total=90,
    )
    second = stitcher._compute_timeline_anchored_window(
        start=123,
        end_excl=124,
        in_start=7,
        source_total=90,
    )
    assert first == second


def test_effects_resolve_source_total_frames_falls_back_to_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(effects, "_probe_video_nb_frames", lambda _path: 0)
    monkeypatch.setattr(effects, "_probe_video_duration_seconds", lambda _path: 2.4)
    assert effects._resolve_source_total_frames("/tmp/any.mp4", fps=30) == 72


def test_effects_validate_frame_ops_contract_rejects_out_of_range_source_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    monkeypatch.setattr(effects, "_resolve_source_total_frames", lambda _path, *, fps: 5)
    with pytest.raises(RuntimeError, match=r"source_frame_out_of_range"):
        effects._validate_frame_ops_contract(
            [
                {
                    "timeline_frame": 2,
                    "op_type": "replace_source",
                    "source_clip_path": str(src),
                    "source_frame": 5,
                }
            ],
            render_end_excl=30,
            fps=30,
        )


def test_effects_validate_frame_ops_contract_accepts_valid_source_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    monkeypatch.setattr(effects, "_resolve_source_total_frames", lambda _path, *, fps: 5)
    effects._validate_frame_ops_contract(
        [
            {
                "timeline_frame": 2,
                "op_type": "replace_source",
                "source_clip_path": str(src),
                "source_frame": 4,
            }
        ],
        render_end_excl=30,
        fps=30,
    )


def test_effects_clamp_render_end_to_base_frames_no_deficit() -> None:
    end_excl, deficit = effects._clamp_render_end_to_base_frames(
        audio_start_frame=1,
        requested_end_excl=101,
        base_source_frames=100,
        slack_frames=1,
    )
    assert end_excl == 101
    assert deficit == 0


def test_effects_clamp_render_end_to_base_frames_within_slack() -> None:
    end_excl, deficit = effects._clamp_render_end_to_base_frames(
        audio_start_frame=1,
        requested_end_excl=102,
        base_source_frames=100,
        slack_frames=1,
    )
    assert end_excl == 101
    assert deficit == 1


def test_effects_clamp_render_end_to_base_frames_exceeds_slack() -> None:
    with pytest.raises(RuntimeError, match=r"base_video_too_short"):
        effects._clamp_render_end_to_base_frames(
            audio_start_frame=1,
            requested_end_excl=104,
            base_source_frames=100,
            slack_frames=1,
        )


def test_pick_two_distinct_clips_fallback_when_pool_has_one() -> None:
    pair = stitcher.pick_two_distinct_clips_from_pool(
        [Path("only.mp4")],
        random.Random(1),
        category="P",
        deck_state={},
        max_unique_tries=10,
    )
    assert pair is None


def test_pick_two_distinct_clips_returns_distinct_pair() -> None:
    pair = stitcher.pick_two_distinct_clips_from_pool(
        [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")],
        random.Random(7),
        category="P",
        deck_state={},
        max_unique_tries=10,
    )
    assert pair is not None
    first, second = pair
    assert first != second


def test_stutter_uses_primary_source_alternates() -> None:
    got = [stitcher.stutter_uses_primary_source(i) for i in range(8)]
    assert got == [True, False, True, False, True, False, True, False]


def test_strip_window_matches_and_require_exact_window_message() -> None:
    class _FakeStrip:
        name = "fake"
        frame_final_start = 10
        frame_final_duration = 1

    s = _FakeStrip()
    assert stitcher.strip_window_matches(s, start=10, end_excl=11)
    assert not stitcher.strip_window_matches(s, start=10, end_excl=12)
    with pytest.raises(RuntimeError, match=r"Strip window mismatch"):
        stitcher._ensure_exact_window_or_raise(s, start=10, end_excl=12, context="unit")


@pytest.mark.skipif(
    os.environ.get("RUN_BLENDER_STITCHER_INTEGRATION", "0").strip() != "1",
    reason="Set RUN_BLENDER_STITCHER_INTEGRATION=1 to run Blender/ffmpeg integration test.",
)
def test_stutter_preset_integration_smoke(tmp_path: Path) -> None:
    blender_bin = Path("/Applications/Blender.app/Contents/MacOS/Blender")
    if not blender_bin.exists():
        pytest.skip("Blender binary not found")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not found")

    media_root = tmp_path / "media"
    out_dir = tmp_path / "out"
    media_root.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    src_clip = tmp_path / "src.mp4"
    audio = tmp_path / "audio.wav"
    out_mp4 = out_dir / "stitched.mp4"

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=360x640:rate=30",
            "-t",
            "3",
            "-pix_fmt",
            "yuv420p",
            str(src_clip),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "8",
            str(audio),
        ],
        check=True,
    )
    for c in ("P", "C", "F", "T", "U", "M"):
        bucket = media_root / c
        bucket.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_clip, bucket / "c1.mp4")

    log_path = tmp_path / "stitch.log"
    cmd = [
        str(blender_bin),
        "-b",
        "--python",
        str(_SCRIPT_PATH),
        "--",
        "--media-root",
        str(media_root),
        "--audio",
        str(audio),
        "--bpm",
        "136",
        "--fps",
        "30",
        "--aspect",
        "9:16",
        "--preset",
        "4p4sbc4f4t2sp2u2c2wm",
        "--seed",
        "123",
        "--output",
        str(out_mp4),
        "--render-end-seconds",
        "8",
    ]
    with log_path.open("wb") as lf:
        proc = subprocess.run(cmd, check=False, stdout=lf, stderr=subprocess.STDOUT)
    assert proc.returncode == 0, log_path.read_text(encoding="utf-8", errors="replace")
    assert out_mp4.exists()
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    assert "Overlap detected" not in log_text


@pytest.mark.skipif(
    os.environ.get("RUN_BLENDER_STITCHER_INTEGRATION", "0").strip() != "1",
    reason="Set RUN_BLENDER_STITCHER_INTEGRATION=1 to run Blender/ffmpeg integration test.",
)
def test_effects_pass_replace_source_frame_offset_regression(tmp_path: Path) -> None:
    blender_bin = Path("/Applications/Blender.app/Contents/MacOS/Blender")
    if not blender_bin.exists():
        pytest.skip("Blender binary not found")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not found")

    source_clip = tmp_path / "source_clip.mp4"
    base_video = tmp_path / "base.mp4"
    audio = tmp_path / "audio.wav"
    plan_path = tmp_path / "effects_plan.json"
    out_mp4 = tmp_path / "effects_out.mp4"
    log_path = tmp_path / "effects.log"

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=360x640:rate=30",
            "-t",
            "2",
            "-pix_fmt",
            "yuv420p",
            str(base_video),
        ],
        check=True,
    )
    shutil.copy2(base_video, source_clip)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "2",
            str(audio),
        ],
        check=True,
    )
    plan_path.write_text(
        (
            "{\n"
            '  "version": 1,\n'
            '  "run_id": "integration",\n'
            '  "cut_idx": 1,\n'
            '  "preset_raw": "integration",\n'
            '  "seed": 123,\n'
            '  "fps": 30,\n'
            '  "render_end_frame_exclusive": 32,\n'
            f'  "base_video_path": "{str(base_video)}",\n'
            '  "segments": [],\n'
            '  "frame_ops": [\n'
            "    {\n"
            '      "timeline_frame": 2,\n'
            '      "op_type": "replace_source",\n'
            '      "color": "",\n'
            '      "source_role": "primary",\n'
            f'      "source_clip_path": "{str(source_clip)}",\n'
            '      "source_frame": 1,\n'
            '      "segment_index": 0\n'
            "    }\n"
            "  ],\n"
            '  "warnings": []\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    cmd = [
        str(blender_bin),
        "-b",
        "--python",
        str(_EFFECTS_SCRIPT_PATH),
        "--",
        "--input-video",
        str(base_video),
        "--audio",
        str(audio),
        "--effects-plan",
        str(plan_path),
        "--fps",
        "30",
        "--aspect",
        "9:16",
        "--output",
        str(out_mp4),
        "--render-end-seconds",
        "1.0",
    ]
    with log_path.open("wb") as lf:
        proc = subprocess.run(cmd, check=False, stdout=lf, stderr=subprocess.STDOUT)
    assert proc.returncode == 0, log_path.read_text(encoding="utf-8", errors="replace")
    assert out_mp4.exists()

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    assert "Effects strip window mismatch" not in log_text
    assert "effects_contract_fail" not in log_text

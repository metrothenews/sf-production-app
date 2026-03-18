from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import bpy

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}


def aspect_to_resolution(aspect: str) -> Tuple[int, int]:
    """Map a simple aspect preset to a concrete render resolution.

    Notes:
      - This only changes the *canvas* size. It does not scale/crop individual strips.
      - The user can still override with --width/--height.
    """
    a = aspect.strip().lower()
    if a in {"9:16", "9x16", "vertical"}:
        return (1080, 1920)
    if a in {"16:9", "16x9", "horizontal"}:
        return (1920, 1080)
    raise ValueError(f"Unsupported --aspect {aspect!r}. Use '16:9' or '9:16'.")


# ----------------------------
# VSE API helpers (4.5-safe)
# ----------------------------

def _ensure_sequence_editor(scene: bpy.types.Scene) -> bpy.types.SequenceEditor:
    se = scene.sequence_editor
    if se is None:
        scene.sequence_editor_create()
        se = scene.sequence_editor
    assert se is not None
    return se


def _all_strips(se: bpy.types.SequenceEditor) -> List[bpy.types.Sequence]:
    if hasattr(se, "strips_all"):
        return list(se.strips_all)
    return list(se.sequences_all)


def _strips_collection(se: bpy.types.SequenceEditor):
    if hasattr(se, "strips"):
        return se.strips
    return se.sequences


def _remove_strip(se: bpy.types.SequenceEditor, strip: bpy.types.Sequence) -> None:
    _strips_collection(se).remove(strip)


def clear_vse(scene: bpy.types.Scene, *, best_effort: bool = False) -> None:
    se = _ensure_sequence_editor(scene)
    strips = _all_strips(se)
    if not strips:
        return

    # Effects-first reduces dependency remove issues
    effects = [s for s in strips if getattr(s, "type", "") in {"CROSS", "GLOW", "TRANSFORM", "SPEED", "COLOR"}]
    non_effects = [s for s in strips if s not in effects]

    for bucket in (effects, non_effects):
        for s in list(bucket):
            try:
                _remove_strip(se, s)
            except Exception:
                if not best_effort:
                    raise

    for s in list(_all_strips(se)):
        try:
            _remove_strip(se, s)
        except Exception:
            if not best_effort:
                raise


# ----------------------------
# Preset parsing
# ----------------------------

@dataclass(frozen=True)
class Step:
    rate: int
    category: str
    modifier: str = ""


@dataclass(frozen=True)
class SegmentPlan:
    segment_index: int
    timeline_start_frame: int
    timeline_end_frame_exclusive: int
    category: str
    modifier: str
    mode: str
    primary_clip_path: str
    secondary_clip_path: str
    primary_in_start: int
    secondary_in_start: int
    fallback_applied: bool
    fallback_reason: str


ALLOWED_STEP_MODIFIERS = {"", "b", "w", "s", "sb", "sw"}


def parse_preset(preset: str) -> List[Step]:
    s = preset.strip()
    if not s:
        raise ValueError("Preset is empty.")

    def read_int(idx: int) -> Tuple[int, int]:
        j = idx
        while j < len(s) and s[j].isdigit():
            j += 1
        if j == idx:
            raise ValueError(f"Expected digits at position {idx} in preset: {preset!r}")
        return int(s[idx:j]), j

    out: List[Step] = []
    i = 0
    while i < len(s):
        if not s[i].isdigit():
            raise ValueError(f"Expected digit at position {i} in preset: {preset!r}")
        n, j = read_int(i)
        if j >= len(s):
            raise ValueError(f"Missing category letter after digits at position {i} in preset: {preset!r}")
        k = j
        while k < len(s) and s[k].isalpha():
            k += 1
        if k == j:
            raise ValueError(f"Expected category letter at position {j} in preset: {preset!r}")
        suffix = s[j:k]
        cat = suffix[-1]
        modifier = suffix[:-1].lower()
        if modifier not in ALLOWED_STEP_MODIFIERS:
            token = s[i:k]
            raise ValueError(
                f"Unsupported modifier '{modifier}' in token '{token}' at position {i} in preset: {preset!r}. "
                f"Allowed modifiers={sorted(ALLOWED_STEP_MODIFIERS)}"
            )
        out.append(Step(rate=n, category=cat, modifier=modifier))
        i = k
    return out


def _step_mode_from_modifier(modifier: str) -> str:
    mod = str(modifier or "").strip().lower()
    mapping = {
        "": "normal",
        "b": "flash_black",
        "w": "flash_white",
        "s": "stutter_two",
        "sb": "stutter_black",
        "sw": "stutter_white",
    }
    if mod not in mapping:
        raise ValueError(f"Unsupported step modifier: {modifier!r}")
    return mapping[mod]


def _choose_initial_in_start(
    *,
    rng: random.Random,
    source_frames: int,
    segment_len: int,
    random_inpoints: bool,
    fit_segment: bool,
) -> int:
    src = max(1, int(source_frames))
    seg = max(1, int(segment_len))
    if not random_inpoints or src <= 1:
        return 0
    if fit_segment and src > seg:
        max_start = src - seg
        return rng.randrange(0, max_start + 1) if max_start > 0 else 0
    return rng.randrange(0, src)


def _build_frame_ops_for_segment(
    *,
    segment_index: int,
    mode: str,
    start: int,
    end_excl: int,
    primary_clip_path: str,
    secondary_clip_path: str,
    primary_source_frames: int,
    secondary_source_frames: int,
    primary_in_start: int,
    secondary_in_start: int,
) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []
    start_i = int(start)
    end_i = int(end_excl)
    if end_i <= start_i:
        return ops

    if mode == "flash_black":
        ops.append(
            {
                "timeline_frame": start_i,
                "op_type": "replace_color",
                "color": "black",
                "source_role": "",
                "source_clip_path": "",
                "source_frame": -1,
                "segment_index": int(segment_index),
            }
        )
        return ops
    if mode == "flash_white":
        ops.append(
            {
                "timeline_frame": start_i,
                "op_type": "replace_color",
                "color": "white",
                "source_role": "",
                "source_clip_path": "",
                "source_frame": -1,
                "segment_index": int(segment_index),
            }
        )
        return ops

    if mode == "stutter_black":
        src = max(1, int(primary_source_frames))
        pos = int(primary_in_start)
        for rel, frame in enumerate(range(start_i, end_i)):
            if stutter_uses_primary_source(rel):
                ops.append(
                    {
                        "timeline_frame": int(frame),
                        "op_type": "replace_color",
                        "color": "black",
                        "source_role": "",
                        "source_clip_path": "",
                        "source_frame": -1,
                        "segment_index": int(segment_index),
                    }
                )
            else:
                ops.append(
                    {
                        "timeline_frame": int(frame),
                        "op_type": "replace_source",
                        "color": "",
                        "source_role": "primary",
                        "source_clip_path": str(primary_clip_path),
                        "source_frame": int(pos % src),
                        "segment_index": int(segment_index),
                    }
                )
                pos += 1
        return ops

    if mode == "stutter_white":
        src = max(1, int(primary_source_frames))
        pos = int(primary_in_start)
        for rel, frame in enumerate(range(start_i, end_i)):
            if stutter_uses_primary_source(rel):
                ops.append(
                    {
                        "timeline_frame": int(frame),
                        "op_type": "replace_color",
                        "color": "white",
                        "source_role": "",
                        "source_clip_path": "",
                        "source_frame": -1,
                        "segment_index": int(segment_index),
                    }
                )
            else:
                ops.append(
                    {
                        "timeline_frame": int(frame),
                        "op_type": "replace_source",
                        "color": "",
                        "source_role": "primary",
                        "source_clip_path": str(primary_clip_path),
                        "source_frame": int(pos % src),
                        "segment_index": int(segment_index),
                    }
                )
                pos += 1
        return ops

    if mode == "stutter_two":
        if not secondary_clip_path:
            return ops
        src_a = max(1, int(primary_source_frames))
        src_b = max(1, int(secondary_source_frames))
        pos_a = int(primary_in_start)
        pos_b = int(secondary_in_start)
        for rel, frame in enumerate(range(start_i, end_i)):
            if stutter_uses_primary_source(rel):
                ops.append(
                    {
                        "timeline_frame": int(frame),
                        "op_type": "replace_source",
                        "color": "",
                        "source_role": "primary",
                        "source_clip_path": str(primary_clip_path),
                        "source_frame": int(pos_a % src_a),
                        "segment_index": int(segment_index),
                    }
                )
                pos_a += 1
            else:
                ops.append(
                    {
                        "timeline_frame": int(frame),
                        "op_type": "replace_source",
                        "color": "",
                        "source_role": "secondary",
                        "source_clip_path": str(secondary_clip_path),
                        "source_frame": int(pos_b % src_b),
                        "segment_index": int(segment_index),
                    }
                )
                pos_b += 1
        return ops

    return ops


def _validate_unique_frame_ops(frame_ops: Sequence[Dict[str, Any]]) -> None:
    seen: Dict[int, Dict[str, Any]] = {}
    for op in frame_ops:
        frame = int(op.get("timeline_frame", -1))
        if frame in seen:
            prev = seen[frame]
            raise RuntimeError(
                "Duplicate frame op detected "
                f"timeline_frame={frame} prev={prev} next={op}"
            )
        seen[frame] = dict(op)


# ----------------------------
# Media scanning + pools
# ----------------------------

def list_videos_in_dir(folder: Path) -> List[Path]:
    vids: List[Path] = []
    if not folder.exists():
        return vids
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            vids.append(p)
    vids.sort()
    return vids


def build_pools(media_root: Path) -> Dict[str, List[Path]]:
    pools: Dict[str, List[Path]] = {}
    if not media_root.exists():
        raise FileNotFoundError(f"Media root not found: {media_root}")
    for child in media_root.iterdir():
        if child.is_dir():
            key = child.name[:1].upper()
            if not key.isalpha():
                continue
            pools.setdefault(key, []).extend(list_videos_in_dir(child))
    # Keep deterministic ordering per pool
    for k in list(pools.keys()):
        pools[k].sort()
    return pools


# ----------------------------
# BPM timing
# ----------------------------

def beats_per_measure() -> int:
    return 4  # fixed: 4-beat measures


def frames_per_beat(bpm: float, fps: int) -> Fraction:
    # frames per beat = fps * 60 / bpm
    if bpm <= 0:
        raise ValueError("BPM must be > 0.")
    return Fraction(fps * 60, 1) / Fraction(int(round(bpm * 1000)), 1000)


def measure_frames(bpm: float, fps: int) -> Fraction:
    return frames_per_beat(bpm, fps) * beats_per_measure()


def seconds_to_frame_excl(seconds: float, fps: int) -> int:
    ms = max(0, int(round(float(seconds) * 1000.0)))
    # end_excl needs to be one frame past the last included frame.
    return max(2, 2 + ((ms * int(fps) + 500) // 1000))


# ----------------------------
# FFmpeg helpers (optional but required by this script mode)
# ----------------------------

def have_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return True
    except Exception:
        return False


def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def ensure_cache_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cached_intermediate_is_usable(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        if path.stat().st_size < 1024:
            return False
    except Exception:
        return False
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    raw = str(proc.stdout or "").strip().splitlines()
    if not raw:
        return False
    try:
        duration = float(raw[-1].strip())
    except Exception:
        return False
    return duration > 0.0


def transcode_to_intermediate(src: Path, cache_dir: Path, *, fps: int) -> Path:
    """
    Transcode arbitrary inputs to a consistent intermediate that Blender likes:
      - CFR (constant frame rate)
      - Even dimensions
      - Blender-friendly MOV proxy + no audio
    Cache key includes src hash and fps.
    """
    cache_dir = ensure_cache_dir(cache_dir)
    # Prefix version so we do not reuse older proxy variants.
    key = f"proxyv3_{file_sha1(src)}_{fps}"
    out = cache_dir / f"{src.stem}_{key}.mov"
    if _cached_intermediate_is_usable(out):
        return out
    if out.exists():
        try:
            out.unlink()
        except Exception:
            pass

    base = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-fflags", "+genpts",
        "-probesize", "100M",
        "-analyzeduration", "100M",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-map", "0:v:0",
        "-an",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-r", str(int(fps)),
        "-fps_mode", "cfr",
        "-movflags", "+faststart",
        str(out),
    ]

    attempts = []
    # Use RGBA intraframe proxies first; avoids Blender swscale crashes seen on ProRes/yuv422p10le.
    attempts.append(base[:-1] + ["-c:v", "png", "-pix_fmt", "rgba", str(out)])
    attempts.append(base[:-1] + ["-c:v", "qtrle", "-pix_fmt", "argb", str(out)])

    last_err = None
    for cmd in attempts:
        try:
            subprocess.run(cmd, check=True)
            if _cached_intermediate_is_usable(out):
                return out
            if out.exists():
                try:
                    out.unlink()
                except Exception:
                    pass
        except Exception as e:
            last_err = e

    raise RuntimeError(f"ffmpeg transcode failed for {src}: {last_err}")


# ----------------------------
# Strip creation helpers
# ----------------------------

def add_sound(scene: bpy.types.Scene, filepath: str, *, channel: int, frame_start: int) -> bpy.types.Sequence:
    se = _ensure_sequence_editor(scene)
    return _strips_collection(se).new_sound(
        name=Path(filepath).stem[:63],
        filepath=filepath,
        channel=channel,
        frame_start=frame_start,
    )


def add_movie(scene: bpy.types.Scene, filepath: str, *, channel: int, frame_start: int) -> bpy.types.Sequence:
    se = _ensure_sequence_editor(scene)
    return _strips_collection(se).new_movie(
        name=Path(filepath).stem[:63],
        filepath=filepath,
        channel=channel,
        frame_start=frame_start,
    )


def add_color_frame(
    scene: bpy.types.Scene,
    *,
    channel: int,
    frame_start: int,
    rgb: Tuple[float, float, float],
) -> bpy.types.Sequence:
    se = _ensure_sequence_editor(scene)
    coll = _strips_collection(se)
    kwargs = {
        "name": f"FLASH_{int(frame_start)}",
        "type": "COLOR",
        "channel": int(channel),
        "frame_start": int(frame_start),
        "frame_end": int(frame_start) + 1,
    }
    try:
        strip = coll.new_effect(**kwargs)
    except TypeError:
        strip = coll.new_effect(seq1=None, seq2=None, **kwargs)
    if hasattr(strip, "color"):
        try:
            strip.color = (float(rgb[0]), float(rgb[1]), float(rgb[2]))
        except Exception:
            pass
    _set_color_window(strip, int(frame_start), int(frame_start) + 1)
    _ensure_exact_window_or_raise(
        strip,
        start=int(frame_start),
        end_excl=int(frame_start) + 1,
        context="add_color_frame",
    )
    return strip


def _set_strip_extension_knobs_zero(strip: bpy.types.Sequence) -> None:
    for attr in (
        "frame_still_start",
        "frame_still_end",
        "animation_offset_start",
        "animation_offset_end",
    ):
        if hasattr(strip, attr):
            try:
                setattr(strip, attr, 0)
            except Exception:
                pass


def _set_color_window(strip: bpy.types.Sequence, start: int, end_excl: int) -> None:
    strip.frame_start = int(start)
    if hasattr(strip, "frame_end"):
        try:
            strip.frame_end = int(end_excl)
        except Exception:
            pass
    _set_strip_extension_knobs_zero(strip)


def strip_final_window(strip: bpy.types.Sequence) -> Tuple[int, int]:
    start = int(getattr(strip, "frame_final_start", getattr(strip, "frame_start", 0)))
    dur = int(getattr(strip, "frame_final_duration", getattr(strip, "frame_duration", 0)))
    dur = max(0, dur)
    return start, start + dur


def strip_window_matches(strip: bpy.types.Sequence, *, start: int, end_excl: int) -> bool:
    got_start, got_end_excl = strip_final_window(strip)
    return got_start == int(start) and got_end_excl == int(end_excl)


def _ensure_exact_window_or_raise(
    strip: bpy.types.Sequence,
    *,
    start: int,
    end_excl: int,
    context: str,
) -> None:
    if strip_window_matches(strip, start=int(start), end_excl=int(end_excl)):
        return
    got_start, got_end_excl = strip_final_window(strip)
    raise RuntimeError(
        "Strip window mismatch "
        f"context={context} strip={getattr(strip, 'name', '<unnamed>')!r} "
        f"expected=[{int(start)},{int(end_excl)}) got=[{got_start},{got_end_excl})"
    )


def strip_source_frames(strip: bpy.types.Sequence) -> int:
    dur = int(getattr(strip, "frame_final_duration", 0))
    if dur <= 0:
        dur = int(getattr(strip, "frame_duration", 0))
    return dur


def strip_stable_source_frames(strip: bpy.types.Sequence) -> int:
    """Return the immutable source frame length for trim math."""
    dur = int(getattr(strip, "frame_duration", 0))
    if dur <= 0:
        dur = int(getattr(strip, "frame_final_duration", 0))
    return int(dur)


def _compute_timeline_anchored_window(
    *,
    start: int,
    end_excl: int,
    in_start: int,
    source_total: int,
) -> Tuple[int, int, int, int]:
    src_total = int(source_total)
    if src_total <= 0:
        raise RuntimeError(f"Invalid source_total={source_total} for strip windowing.")
    req_len = max(1, int(end_excl) - int(start))
    off_start = max(0, int(in_start))
    if off_start >= src_total:
        off_start = max(0, src_total - 1)
    max_visible = max(1, src_total - off_start)
    visible_len = min(req_len, max_visible)
    off_end = max(0, (src_total - off_start) - visible_len)
    frame_start = int(start) - off_start
    return (int(frame_start), int(off_start), int(off_end), int(visible_len))


def set_strip_window(strip: bpy.types.Sequence, start: int, end_excl: int, *, in_start: int = 0) -> None:
    """
    Make this strip cover exactly [start, end_excl) (or less if the source is shorter),
    starting the strip content at in_start frames into the source.
    """
    _set_strip_extension_knobs_zero(strip)
    src_total = strip_stable_source_frames(strip)
    if src_total <= 0:
        raise RuntimeError(f"Unknown duration for strip '{strip.name}'.")
    frame_start, off_start, off_end, _ = _compute_timeline_anchored_window(
        start=int(start),
        end_excl=int(end_excl),
        in_start=int(in_start),
        source_total=int(src_total),
    )
    strip.frame_start = int(frame_start)
    if hasattr(strip, "frame_offset_start"):
        strip.frame_offset_start = int(off_start)
    if hasattr(strip, "frame_offset_end"):
        strip.frame_offset_end = int(off_end)
    _set_strip_extension_knobs_zero(strip)


def add_movie_frame(
    scene: bpy.types.Scene,
    *,
    filepath: str,
    channel: int,
    frame_start: int,
    in_start: int,
) -> bpy.types.Sequence:
    strip = add_movie(scene, filepath, channel=int(channel), frame_start=int(frame_start))
    mute_movie_audio(strip)
    expected_start = int(frame_start)
    expected_end_excl = int(frame_start) + 1
    set_strip_window(strip, expected_start, expected_end_excl, in_start=int(in_start))
    if not strip_window_matches(strip, start=expected_start, end_excl=expected_end_excl):
        first_start, first_end = strip_final_window(strip)
        first_dur = max(0, int(first_end) - int(first_start))
        # One correction pass handles strips where still/animation offsets are reintroduced.
        set_strip_window(strip, expected_start, expected_end_excl, in_start=int(in_start))
        second_start, second_end = strip_final_window(strip)
        second_dur = max(0, int(second_end) - int(second_start))
        if second_dur > first_dur:
            raise RuntimeError(
                "Strip correction broadened window "
                f"context=add_movie_frame strip={getattr(strip, 'name', '<unnamed>')!r} "
                f"before=[{first_start},{first_end}) after=[{second_start},{second_end}) "
                f"expected=[{expected_start},{expected_end_excl})"
            )
    _ensure_exact_window_or_raise(
        strip,
        start=expected_start,
        end_excl=expected_end_excl,
        context="add_movie_frame",
    )
    return strip


def probe_movie_source_frames(scene: bpy.types.Scene, *, filepath: str, channel: int) -> int:
    se = _ensure_sequence_editor(scene)
    probe = add_movie(scene, filepath, channel=int(channel), frame_start=1)
    try:
        mute_movie_audio(probe)
        src = strip_source_frames(probe)
        if src <= 0:
            raise RuntimeError(f"Unknown duration for strip '{probe.name}'.")
        return int(src)
    finally:
        try:
            _remove_strip(se, probe)
        except Exception:
            pass


def tile_movie_to_fill(
    scene: bpy.types.Scene,
    *,
    filepath: str,
    channel: int,
    start: int,
    end_excl: int,
    rng: random.Random,
    random_inpoints: bool,
    max_tiles: int,
) -> None:
    """
    Fill [start, end_excl) with MOVIE strips by looping the same source clip.

    This fixes "dips to black" caused by short clips:
      - Previously: one strip got shortened to available frames, but the timeline cursor
        advanced to the full segment end -> gap -> black.
      - Now: we add as many back-to-back strips as needed to cover every frame.
    """
    cur = int(start)
    seg_len = max(1, int(end_excl - start))
    tiles = 0
    first = True

    while cur < int(end_excl):
        tiles += 1
        if tiles > int(max_tiles):
            raise RuntimeError(
                f"Clip too short to fill segment without exceeding --max-segments-per-cut={max_tiles}. "
                f"segment_len={end_excl-start} frames"
            )

        strip = add_movie(scene, filepath, channel=int(channel), frame_start=int(cur))
        mute_movie_audio(strip)

        src = strip_source_frames(strip)
        if src <= 0:
            raise RuntimeError(f"Unknown duration for strip '{strip.name}' (cannot loop).")

        if first and random_inpoints:
            # If the clip can cover the whole segment, choose an inpoint that avoids looping.
            # Otherwise, choose any inpoint; the rest of the segment will be filled by looping.
            if src > seg_len:
                max_start = src - seg_len
                in_start = rng.randrange(0, max_start + 1) if max_start > 0 else 0
            else:
                in_start = rng.randrange(0, src) if src > 1 else 0
        else:
            in_start = 0

        # How many frames can this tile contribute?
        if in_start >= src:
            in_start = max(0, src - 1)
        avail = max(1, int(src - in_start))
        remaining = int(end_excl) - int(cur)
        tile_len = remaining if remaining <= avail else avail

        # Force exactly this tile's coverage; next tile continues immediately.
        set_strip_window(strip, int(cur), int(cur + tile_len), in_start=int(in_start))

        cur += int(tile_len)
        first = False


def stutter_uses_primary_source(rel_frame_index: int) -> bool:
    return (int(rel_frame_index) % 2) == 0


def render_stutter_two_clips(
    scene: bpy.types.Scene,
    *,
    filepath_a: str,
    filepath_b: str,
    channel: int,
    start: int,
    end_excl: int,
    rng: random.Random,
    random_inpoints: bool,
) -> None:
    src_a = probe_movie_source_frames(scene, filepath=filepath_a, channel=int(channel))
    src_b = probe_movie_source_frames(scene, filepath=filepath_b, channel=int(channel))
    pos_a = rng.randrange(0, src_a) if random_inpoints and src_a > 1 else 0
    pos_b = rng.randrange(0, src_b) if random_inpoints and src_b > 1 else 0

    for rel, frame in enumerate(range(int(start), int(end_excl))):
        use_a = stutter_uses_primary_source(rel)
        if use_a:
            add_movie_frame(
                scene,
                filepath=filepath_a,
                channel=int(channel),
                frame_start=int(frame),
                in_start=int(pos_a % src_a),
            )
            pos_a += 1
        else:
            add_movie_frame(
                scene,
                filepath=filepath_b,
                channel=int(channel),
                frame_start=int(frame),
                in_start=int(pos_b % src_b),
            )
            pos_b += 1


def render_stutter_color_clip(
    scene: bpy.types.Scene,
    *,
    filepath: str,
    color_rgb: Tuple[float, float, float],
    channel: int,
    start: int,
    end_excl: int,
    rng: random.Random,
    random_inpoints: bool,
) -> None:
    src = probe_movie_source_frames(scene, filepath=filepath, channel=int(channel))
    pos = rng.randrange(0, src) if random_inpoints and src > 1 else 0

    for rel, frame in enumerate(range(int(start), int(end_excl))):
        if stutter_uses_primary_source(rel):
            add_color_frame(scene, channel=int(channel), frame_start=int(frame), rgb=color_rgb)
            continue
        add_movie_frame(
            scene,
            filepath=filepath,
            channel=int(channel),
            frame_start=int(frame),
            in_start=int(pos % src),
        )
        pos += 1


# ----------------------------
# Safety / cleanup helpers
# ----------------------------

def remove_non_master_audio(scene: bpy.types.Scene, *, master_audio_path: str, preserve_aux: bool) -> None:
    """
    Some movie imports can bring audio. We keep:
      - The master audio strip at master_audio_path
      - Any additional SOUND strips only if preserve_aux=True
    Otherwise, remove SOUND strips that aren't the master.
    """
    se = _ensure_sequence_editor(scene)
    master_abs = str(Path(master_audio_path).expanduser().resolve())
    for s in list(_all_strips(se)):
        if getattr(s, "type", "") == "SOUND":
            path = getattr(s, "sound", None)
            fp = getattr(path, "filepath", "") if path else ""
            fp_abs = ""
            try:
                fp_abs = str(Path(fp).expanduser().resolve())
            except Exception:
                fp_abs = fp
            if fp_abs != master_abs:
                if not preserve_aux:
                    try:
                        _remove_strip(se, s)
                    except Exception:
                        pass


def mute_movie_audio(strip: bpy.types.Sequence) -> None:
    # Mute embedded audio if Blender exposes this property.
    if hasattr(strip, "volume"):
        try:
            strip.volume = 0.0
        except Exception:
            pass


def assert_no_overlaps(scene: bpy.types.Scene) -> None:
    """
    Basic overlap sanity check per-channel:
    For each channel, ensure no two strips overlap in [start, end_excl).
    """
    se = _ensure_sequence_editor(scene)
    strips = _all_strips(se)
    by_ch: Dict[int, List[bpy.types.Sequence]] = {}
    for s in strips:
        ch = int(getattr(s, "channel", 0))
        by_ch.setdefault(ch, []).append(s)

    for ch, lst in by_ch.items():
        lst.sort(key=lambda x: int(getattr(x, "frame_final_start", getattr(x, "frame_start", 0))))
        prev_end = None
        for s in lst:
            start = int(getattr(s, "frame_final_start", getattr(s, "frame_start", 0)))
            dur = int(getattr(s, "frame_final_duration", getattr(s, "frame_duration", 0)))
            end_excl = start + max(0, dur)
            if prev_end is not None and start < prev_end:
                print(
                    "overlap_diagnostics "
                    f"channel={ch} strip={getattr(s, 'name', '<unnamed>')!r} strip_start={start} "
                    f"prev_end={prev_end}"
                )
                raise RuntimeError(
                    f"Overlap detected on channel {ch}: '{s.name}' starts {start} < prev_end {prev_end}."
                )
            prev_end = end_excl


def _assert_no_overlaps_in_window(
    scene: bpy.types.Scene,
    *,
    channel: int,
    start: int,
    end_excl: int,
    mode: str,
) -> None:
    se = _ensure_sequence_editor(scene)
    window = []
    for s in _all_strips(se):
        if int(getattr(s, "channel", 0)) != int(channel):
            continue
        s_start, s_end = strip_final_window(s)
        if s_end <= int(start) or s_start >= int(end_excl):
            continue
        window.append((s_start, s_end, s))
    window.sort(key=lambda x: (x[0], x[1], getattr(x[2], "name", "")))

    prev_end: Optional[int] = None
    prev_name = ""
    for s_start, s_end, s in window:
        if prev_end is not None and s_start < prev_end:
            print(
                "stutter_overlap_diagnostics "
                f"mode={mode} channel={int(channel)} segment=[{int(start)},{int(end_excl)}) "
                f"strip={getattr(s, 'name', '<unnamed>')!r} strip_start={s_start} prev_end={prev_end} "
                f"prev_strip={prev_name!r}"
            )
            raise RuntimeError(
                "Stutter segment overlap "
                f"mode={mode} channel={int(channel)} segment=[{int(start)},{int(end_excl)}) "
                f"strip={getattr(s, 'name', '<unnamed>')!r} strip_start={s_start} "
                f"prev_strip={prev_name!r} prev_end={prev_end}"
            )
        prev_end = s_end
        prev_name = str(getattr(s, "name", ""))


# ----------------------------
# Timeline builder
# ----------------------------

def pick_clip_from_pool(
    pool: List[Path],
    rng: random.Random,
    *,
    category: str,
    deck_state: Dict[str, List[int]],
    max_unique_tries: int,
) -> Path:
    if not pool:
        raise RuntimeError("Empty pool.")
    key = str(category or "").upper() or "_"
    deck = deck_state.get(key)
    if not deck:
        deck = list(range(len(pool)))
        rng.shuffle(deck)
        deck_state[key] = deck
    idx = deck.pop()
    return pool[idx]


def pick_two_distinct_clips_from_pool(
    pool: List[Path],
    rng: random.Random,
    *,
    category: str,
    deck_state: Dict[str, List[int]],
    max_unique_tries: int,
) -> Optional[Tuple[Path, Path]]:
    unique = {str(p) for p in pool}
    if len(unique) < 2:
        return None

    first = pick_clip_from_pool(
        pool,
        rng,
        category=category,
        deck_state=deck_state,
        max_unique_tries=max_unique_tries,
    )
    tries = max(2, int(max_unique_tries), len(pool) * 2)
    for _ in range(tries):
        second = pick_clip_from_pool(
            pool,
            rng,
            category=category,
            deck_state=deck_state,
            max_unique_tries=max_unique_tries,
        )
        if second != first:
            return (first, second)
    return None


def build_timeline(args: argparse.Namespace) -> bpy.types.Scene:
    media_root = Path(args.media_root).expanduser().resolve()
    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if not have_ffmpeg():
        raise RuntimeError("This mode requires system ffmpeg. Install: brew install ffmpeg")

    steps = parse_preset(args.preset)
    pools = build_pools(media_root)

    rng = random.Random()
    rng.seed(None if args.seed == 0 else args.seed)

    scene = bpy.context.scene
    if args.scene:
        if args.scene not in bpy.data.scenes:
            raise ValueError(f"Scene not found: '{args.scene}'.")
        scene = bpy.data.scenes[args.scene]

    clear_vse(scene, best_effort=args.best_effort)
    _ensure_sequence_editor(scene)

    scene.render.fps = int(args.fps)
    scene.render.fps_base = 1.0

    audio_strip = add_sound(scene, str(audio_path), channel=args.audio_channel, frame_start=1)
    audio_start = int(audio_strip.frame_start)
    audio_len = int(getattr(audio_strip, "frame_final_duration", 0)) or int(getattr(audio_strip, "frame_duration", 0))
    if audio_len <= 0:
        raise RuntimeError("Master audio strip has unknown duration.")
    audio_end_excl = audio_start + audio_len  # exclusive end
    if float(getattr(args, "render_end_seconds", 0.0) or 0.0) > 0.0:
        cap_end_excl = seconds_to_frame_excl(float(args.render_end_seconds), int(args.fps))
        audio_end_excl = min(int(audio_end_excl), int(cap_end_excl))
    if audio_end_excl <= audio_start:
        raise RuntimeError("Computed stitch render end is invalid after applying render-end-seconds cap.")
    scene["_stitch_render_end_excl"] = int(audio_end_excl)

    mf = measure_frames(args.bpm, int(args.fps))  # Fraction
    if mf <= 0:
        raise RuntimeError("Invalid measure_frames computed.")

    cursor = audio_start
    idx = 0
    remainder = Fraction(0, 1)
    category_decks: Dict[str, List[int]] = {}

    cache_dir = Path(args.cache_dir).expanduser().resolve()
    pass1_mode = str(getattr(args, "effects_pass1_mode", "legacy") or "legacy").strip().lower()
    pass1_base_only = pass1_mode == "base_only"
    segments_payload: List[SegmentPlan] = []
    frame_ops_payload: List[Dict[str, Any]] = []
    warnings_payload: List[str] = []

    while cursor < audio_end_excl:
        step = steps[idx]
        cuts = int(step.rate)
        cat = step.category.upper()
        mode = _step_mode_from_modifier(step.modifier)

        if cuts <= 0:
            raise RuntimeError(f"Invalid cut rate {cuts} in preset step: {step}")
        if cat not in pools or not pools[cat]:
            raise RuntimeError(f"No clips found for category '{cat}' under {media_root}")

        seg_len_f = (mf / cuts) + remainder
        seg_len_i = int(seg_len_f)  # floor
        remainder = seg_len_f - seg_len_i
        if seg_len_i <= 0:
            seg_len_i = 1

        start = cursor
        end_excl = min(audio_end_excl, start + seg_len_i)
        seg_len = max(1, int(end_excl - start))
        print(
            "segment_planned "
            f"segment_index={int(len(segments_payload))} category={cat} modifier={step.modifier!r} "
            f"mode={mode} segment_frames={seg_len} timeline_start={start} timeline_end_excl={end_excl}"
        )

        if pass1_base_only:
            primary_clip = pick_clip_from_pool(
                pools[cat],
                rng,
                category=cat,
                deck_state=category_decks,
                max_unique_tries=int(args.max_unique_tries),
            )
            secondary_clip: Optional[Path] = None
            fallback_applied = False
            fallback_reason = ""
            effective_mode = mode

            if mode == "stutter_two":
                pair = pick_two_distinct_clips_from_pool(
                    pools[cat],
                    rng,
                    category=cat,
                    deck_state=category_decks,
                    max_unique_tries=int(args.max_unique_tries),
                )
                if pair is None:
                    effective_mode = "normal"
                    fallback_applied = True
                    fallback_reason = "insufficient_unique_clips"
                    warning = (
                        "stutter_fallback=normal "
                        f"category={cat} reason={fallback_reason} segment_start={start} segment_end={end_excl}"
                    )
                    print(warning)
                    warnings_payload.append(warning)
                else:
                    primary_clip = pair[0]
                    secondary_clip = pair[1]

            primary_inter = transcode_to_intermediate(primary_clip, cache_dir, fps=int(args.fps))
            secondary_inter = (
                transcode_to_intermediate(secondary_clip, cache_dir, fps=int(args.fps))
                if secondary_clip is not None
                else None
            )

            primary_src_frames = probe_movie_source_frames(
                scene,
                filepath=str(primary_inter),
                channel=int(args.video_channel),
            )
            secondary_src_frames = (
                probe_movie_source_frames(
                    scene,
                    filepath=str(secondary_inter),
                    channel=int(args.video_channel),
                )
                if secondary_inter is not None
                else 0
            )

            primary_in_start = _choose_initial_in_start(
                rng=rng,
                source_frames=int(primary_src_frames),
                segment_len=int(seg_len),
                random_inpoints=bool(args.random_inpoints),
                fit_segment=bool(effective_mode in {"normal", "flash_black", "flash_white"}),
            )
            secondary_in_start = _choose_initial_in_start(
                rng=rng,
                source_frames=int(secondary_src_frames) if secondary_src_frames > 0 else 1,
                segment_len=int(seg_len),
                random_inpoints=bool(args.random_inpoints),
                fit_segment=False,
            )
            if secondary_inter is None:
                secondary_in_start = 0

            segment = SegmentPlan(
                segment_index=int(len(segments_payload)),
                timeline_start_frame=int(start),
                timeline_end_frame_exclusive=int(end_excl),
                category=str(cat),
                modifier=str(step.modifier or ""),
                mode=str(effective_mode),
                primary_clip_path=str(primary_inter),
                secondary_clip_path=str(secondary_inter or ""),
                primary_in_start=int(primary_in_start),
                secondary_in_start=int(secondary_in_start),
                fallback_applied=bool(fallback_applied),
                fallback_reason=str(fallback_reason),
            )
            segments_payload.append(segment)
            print(
                "segment_mode "
                f"segment_index={int(segment.segment_index)} mode={effective_mode} "
                f"primary_clip={segment.primary_clip_path!r} secondary_clip={segment.secondary_clip_path!r} "
                f"primary_in_start={int(segment.primary_in_start)} secondary_in_start={int(segment.secondary_in_start)}"
            )
            frame_ops_payload.extend(
                _build_frame_ops_for_segment(
                    segment_index=int(segment.segment_index),
                    mode=str(effective_mode),
                    start=int(start),
                    end_excl=int(end_excl),
                    primary_clip_path=str(primary_inter),
                    secondary_clip_path=str(secondary_inter or ""),
                    primary_source_frames=int(primary_src_frames),
                    secondary_source_frames=int(secondary_src_frames),
                    primary_in_start=int(primary_in_start),
                    secondary_in_start=int(secondary_in_start),
                )
            )

            # Pass1 base render ignores modifiers and always fills the segment with primary media.
            tile_movie_to_fill(
                scene,
                filepath=str(primary_inter),
                channel=int(args.video_channel),
                start=int(start),
                end_excl=int(end_excl),
                rng=rng,
                random_inpoints=bool(args.random_inpoints),
                max_tiles=int(args.max_segments_per_cut),
            )
        else:
            # Legacy mode applies modifiers directly during stitch pass.
            if mode == "normal":
                clip_path = pick_clip_from_pool(
                    pools[cat],
                    rng,
                    category=cat,
                    deck_state=category_decks,
                    max_unique_tries=int(args.max_unique_tries),
                )
                inter = transcode_to_intermediate(clip_path, cache_dir, fps=int(args.fps))
                tile_movie_to_fill(
                    scene,
                    filepath=str(inter),
                    channel=int(args.video_channel),
                    start=int(start),
                    end_excl=int(end_excl),
                    rng=rng,
                    random_inpoints=bool(args.random_inpoints),
                    max_tiles=int(args.max_segments_per_cut),
                )
            elif mode in {"flash_black", "flash_white"}:
                clip_path = pick_clip_from_pool(
                    pools[cat],
                    rng,
                    category=cat,
                    deck_state=category_decks,
                    max_unique_tries=int(args.max_unique_tries),
                )
                inter = transcode_to_intermediate(clip_path, cache_dir, fps=int(args.fps))
                rgb = (0.0, 0.0, 0.0) if mode == "flash_black" else (1.0, 1.0, 1.0)
                add_color_frame(scene, channel=int(args.video_channel), frame_start=int(start), rgb=rgb)
                if int(end_excl) - int(start) > 1:
                    tile_movie_to_fill(
                        scene,
                        filepath=str(inter),
                        channel=int(args.video_channel),
                        start=int(start) + 1,
                        end_excl=int(end_excl),
                        rng=rng,
                        random_inpoints=bool(args.random_inpoints),
                        max_tiles=int(args.max_segments_per_cut),
                    )
            elif mode == "stutter_two":
                pair = pick_two_distinct_clips_from_pool(
                    pools[cat],
                    rng,
                    category=cat,
                    deck_state=category_decks,
                    max_unique_tries=int(args.max_unique_tries),
                )
                if pair is None:
                    print(
                        "stutter_fallback=normal "
                        f"category={cat} reason=insufficient_unique_clips segment_start={start} segment_end={end_excl}"
                    )
                    clip_path = pick_clip_from_pool(
                        pools[cat],
                        rng,
                        category=cat,
                        deck_state=category_decks,
                        max_unique_tries=int(args.max_unique_tries),
                    )
                    inter = transcode_to_intermediate(clip_path, cache_dir, fps=int(args.fps))
                    tile_movie_to_fill(
                        scene,
                        filepath=str(inter),
                        channel=int(args.video_channel),
                        start=int(start),
                        end_excl=int(end_excl),
                        rng=rng,
                        random_inpoints=bool(args.random_inpoints),
                        max_tiles=int(args.max_segments_per_cut),
                    )
                else:
                    inter_a = transcode_to_intermediate(pair[0], cache_dir, fps=int(args.fps))
                    inter_b = transcode_to_intermediate(pair[1], cache_dir, fps=int(args.fps))
                    render_stutter_two_clips(
                        scene,
                        filepath_a=str(inter_a),
                        filepath_b=str(inter_b),
                        channel=int(args.video_channel),
                        start=int(start),
                        end_excl=int(end_excl),
                        rng=rng,
                        random_inpoints=bool(args.random_inpoints),
                    )
                    _assert_no_overlaps_in_window(
                        scene,
                        channel=int(args.video_channel),
                        start=int(start),
                        end_excl=int(end_excl),
                        mode="stutter_two",
                    )
            else:
                clip_path = pick_clip_from_pool(
                    pools[cat],
                    rng,
                    category=cat,
                    deck_state=category_decks,
                    max_unique_tries=int(args.max_unique_tries),
                )
                inter = transcode_to_intermediate(clip_path, cache_dir, fps=int(args.fps))
                rgb = (0.0, 0.0, 0.0) if mode == "stutter_black" else (1.0, 1.0, 1.0)
                render_stutter_color_clip(
                    scene,
                    filepath=str(inter),
                    color_rgb=rgb,
                    channel=int(args.video_channel),
                    start=int(start),
                    end_excl=int(end_excl),
                    rng=rng,
                    random_inpoints=bool(args.random_inpoints),
                )
                _assert_no_overlaps_in_window(
                    scene,
                    channel=int(args.video_channel),
                    start=int(start),
                    end_excl=int(end_excl),
                    mode=mode,
                )

        # Clean up any imported audio from any of the created movie strips
        remove_non_master_audio(scene, preserve_aux=bool(args.preserve_aux_audio), master_audio_path=str(audio_path))

        cursor = end_excl
        idx = (idx + 1) % len(steps)

    scene.render.use_sequencer = True

    if args.width and args.height:
        scene.render.resolution_x = int(args.width)
        scene.render.resolution_y = int(args.height)
        expected = (int(args.width), int(args.height))
    else:
        if getattr(args, "aspect", ""):
            w, h = aspect_to_resolution(str(args.aspect))
            scene.render.resolution_x = int(w)
            scene.render.resolution_y = int(h)
            expected = (int(w), int(h))
        else:
            expected = None

    scene.render.resolution_percentage = int(args.resolution_percentage)

    if expected is not None:
        got = (int(scene.render.resolution_x), int(scene.render.resolution_y))
        if got != expected:
            raise RuntimeError(
                f"Failed to set render resolution: expected {expected[0]}x{expected[1]}, got {got[0]}x{got[1]}"
            )

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        scene.render.filepath = str(out_path)
        # Persist output/audio metadata for post-render ffmpeg muxing in render_from_audio().
        scene["_stitch_output_path"] = str(out_path)
        scene["_stitch_audio_path"] = str(audio_path)
        scene["_stitch_fps"] = int(args.fps)

    effects_plan_out = str(getattr(args, "effects_plan_out", "") or "").strip()
    if effects_plan_out:
        if not pass1_base_only:
            raise RuntimeError("--effects-plan-out requires --effects-pass1-mode=base_only.")
        _validate_unique_frame_ops(frame_ops_payload)
        payload = {
            "version": 1,
            "run_id": str(getattr(args, "run_id", "") or ""),
            "cut_idx": int(getattr(args, "cut_idx", 0) or 0),
            "preset_raw": str(args.preset),
            "seed": int(args.seed),
            "fps": int(args.fps),
            "render_end_frame_exclusive": int(audio_end_excl),
            "base_video_path": str(Path(args.output).expanduser().resolve()) if str(args.output or "").strip() else "",
            "segments": [
                {
                    "segment_index": int(s.segment_index),
                    "timeline_start_frame": int(s.timeline_start_frame),
                    "timeline_end_frame_exclusive": int(s.timeline_end_frame_exclusive),
                    "category": str(s.category),
                    "modifier": str(s.modifier),
                    "mode": str(s.mode),
                    "primary_clip_path": str(s.primary_clip_path),
                    "secondary_clip_path": str(s.secondary_clip_path),
                    "primary_in_start": int(s.primary_in_start),
                    "secondary_in_start": int(s.secondary_in_start),
                    "fallback_applied": bool(s.fallback_applied),
                    "fallback_reason": str(s.fallback_reason),
                }
                for s in segments_payload
            ],
            "frame_ops": list(frame_ops_payload),
            "warnings": list(warnings_payload),
        }
        plan_path = Path(effects_plan_out).expanduser().resolve()
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(
            "effects_plan_written "
            f"path={plan_path} segments={len(segments_payload)} frame_ops={len(frame_ops_payload)}"
        )

    assert_no_overlaps(scene)
    return scene


def render_from_audio(scene: bpy.types.Scene) -> None:
    """
    IMPORTANT:
      bpy.ops.render.render() renders the CONTEXT scene by default.
      If your .blend has multiple scenes (or you used --scene), you must override context,
      otherwise you can end up rendering a different scene with different resolution settings.
    """
    se = _ensure_sequence_editor(scene)
    sounds = [s for s in _all_strips(se) if getattr(s, "type", "") == "SOUND"]
    if not sounds:
        raise RuntimeError("No SOUND strips found to determine render range.")

    sounds.sort(key=lambda s: int(getattr(s, "frame_final_duration", getattr(s, "frame_duration", 0))), reverse=True)
    master = sounds[0]
    start = int(getattr(master, "frame_final_start", getattr(master, "frame_start", 1)))
    dur = int(getattr(master, "frame_final_duration", getattr(master, "frame_duration", 0)))
    if dur <= 0:
        raise RuntimeError("Master audio duration unknown; cannot set render range.")
    end_excl = start + dur
    cap_end_excl = int(scene.get("_stitch_render_end_excl") or 0)
    if cap_end_excl > 1:
        end_excl = min(int(end_excl), int(cap_end_excl))
    if end_excl <= start:
        raise RuntimeError("Computed stitch render end is invalid.")

    scene.frame_start = start
    scene.frame_end = max(start, end_excl - 1)

    scene.render.use_sequencer = True

    out_raw = str(scene.get("_stitch_output_path") or scene.render.filepath or "").strip()
    if not out_raw:
        raise RuntimeError("Missing stitch output path.")
    out_path = Path(out_raw).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path = str(scene.get("_stitch_audio_path") or "").strip()
    if not audio_path:
        raise RuntimeError("Missing stitch audio path for mux stage.")
    fps = int(scene.get("_stitch_fps") or int(scene.render.fps) or 30)

    # Avoid Blender's FFmpeg movie writer instability: render PNG frames, then mux via external ffmpeg.
    frames_dir = Path(tempfile.mkdtemp(prefix="vse_frames_"))
    scene.render.filepath = str(frames_dir / "frame_")
    scene.render.image_settings.file_format = "PNG"
    if hasattr(scene.render.image_settings, "color_mode"):
        scene.render.image_settings.color_mode = "RGB"
    if hasattr(scene.render.image_settings, "color_depth"):
        scene.render.image_settings.color_depth = "8"

    view_layer = scene.view_layers[0] if scene.view_layers else bpy.context.view_layer
    try:
        with bpy.context.temp_override(scene=scene, view_layer=view_layer):
            bpy.ops.render.render(animation=True, use_viewport=False)

        frame_glob = sorted(frames_dir.glob("frame_*.png"))
        if not frame_glob:
            raise RuntimeError(f"No PNG frames rendered under {frames_dir}")

        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            str(fps),
            "-start_number",
            str(int(start)),
            "-i",
            str(frames_dir / "frame_%04d.png"),
            "-i",
            str(Path(audio_path).expanduser().resolve()),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        proc = subprocess.run(ffmpeg_cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                "ffmpeg mux failed for stitch output: "
                f"code={proc.returncode} stderr={(proc.stderr or '').strip()}"
            )
    finally:
        try:
            shutil.rmtree(frames_dir, ignore_errors=True)
        except Exception:
            pass


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--media-root", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--bpm", required=True, type=float)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument(
        "--render-end-seconds",
        type=float,
        default=0.0,
        help="Optional cap for stitch render duration in seconds from frame 1. 0 disables cap.",
    )
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--cut-idx", type=int, default=0)
    ap.add_argument("--scene", default="")
    ap.add_argument("--best-effort", action="store_true")
    ap.add_argument("--random-inpoints", action="store_true")
    ap.add_argument("--max-unique-tries", type=int, default=200)
    ap.add_argument("--max-segments-per-cut", type=int, default=64)  # now used as the tiling cap
    ap.add_argument("--preserve-aux-audio", action="store_true")
    ap.add_argument("--video-channel", type=int, default=2)
    ap.add_argument("--audio-channel", type=int, default=1)
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--output", default="")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)

    ap.add_argument(
        "--aspect",
        default="9:16",
        help="Set output aspect preset (e.g. 9:16 or 16:9). Ignored if --width/--height are set.",
    )

    ap.add_argument("--resolution-percentage", type=int, default=100)
    ap.add_argument("--ffmpeg-format", default="MPEG4")
    ap.add_argument("--vcodec", default="H264")
    ap.add_argument("--acodec", default="AAC")
    ap.add_argument("--cache-dir", default="~/.vse_stitcher_cache")
    ap.add_argument("--effects-plan-out", default="")
    ap.add_argument(
        "--effects-pass1-mode",
        default="legacy",
        choices=["legacy", "base_only"],
        help="legacy=apply modifiers in stitch pass; base_only=ignore modifiers and emit plan for pass2.",
    )

    args = ap.parse_args(argv)

    if not getattr(bpy.app.build_options, "codec_ffmpeg", False):
        raise RuntimeError("This Blender build does not have FFmpeg enabled (codec_ffmpeg is False).")

    scene = build_timeline(args)

    if not args.no_render:
        if not args.output:
            raise RuntimeError("Refusing to render without --output.")
        render_from_audio(scene)

    return 0


if __name__ == "__main__":
    try:
        user_args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
        raise SystemExit(main(user_args))
    except SystemExit:
        raise
    except Exception as e:
        print(f"\nERROR: {e}\n", file=sys.stderr)
        raise

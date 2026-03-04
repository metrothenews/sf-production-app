from __future__ import annotations

import argparse
import hashlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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
        cat = s[j]
        if not cat.isalpha():
            raise ValueError(f"Expected category letter at position {j} in preset: {preset!r}")
        out.append(Step(rate=n, category=cat))
        i = j + 1
    return out


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


def strip_source_frames(strip: bpy.types.Sequence) -> int:
    dur = int(getattr(strip, "frame_final_duration", 0))
    if dur <= 0:
        dur = int(getattr(strip, "frame_duration", 0))
    return dur


def set_strip_window(strip: bpy.types.Sequence, start: int, end_excl: int, *, in_start: int = 0) -> None:
    """
    Make this strip cover exactly [start, end_excl) (or less if the source is shorter),
    starting the strip content at in_start frames into the source.
    """
    strip.frame_start = int(start)
    if hasattr(strip, "frame_offset_start"):
        strip.frame_offset_start = max(0, int(in_start))

    want = max(1, int(end_excl - start))
    src = strip_source_frames(strip)
    if src <= 0:
        raise RuntimeError(f"Unknown duration for strip '{strip.name}'.")

    off_start = int(getattr(strip, "frame_offset_start", 0))
    max_visible = max(1, src - off_start)
    if want > max_visible:
        want = max_visible

    off_end = max(0, (src - off_start) - want)
    if hasattr(strip, "frame_offset_end"):
        strip.frame_offset_end = int(off_end)


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
                raise RuntimeError(
                    f"Overlap detected on channel {ch}: '{s.name}' starts {start} < prev_end {prev_end}."
                )
            prev_end = end_excl


# ----------------------------
# Timeline builder
# ----------------------------

def pick_clip_from_pool(pool: List[Path], rng: random.Random, *, max_unique_tries: int) -> Path:
    if not pool:
        raise RuntimeError("Empty pool.")
    for _ in range(max_unique_tries):
        return pool[rng.randrange(0, len(pool))]
    return pool[rng.randrange(0, len(pool))]


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

    cache_dir = Path(args.cache_dir).expanduser().resolve()

    while cursor < audio_end_excl:
        step = steps[idx]
        cuts = int(step.rate)
        cat = step.category.upper()

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

        clip_path = pick_clip_from_pool(pools[cat], rng, max_unique_tries=int(args.max_unique_tries))
        inter = transcode_to_intermediate(clip_path, cache_dir, fps=int(args.fps))

        # FIX: loop/tile short clips to fill the entire allotted window (prevents dips to black)
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

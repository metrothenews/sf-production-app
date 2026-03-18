from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import bpy


def aspect_to_resolution(aspect: str) -> Tuple[int, int]:
    a = aspect.strip().lower()
    if a in {"9:16", "9x16", "vertical"}:
        return (1080, 1920)
    if a in {"16:9", "16x9", "horizontal"}:
        return (1920, 1080)
    raise ValueError(f"Unsupported --aspect {aspect!r}. Use '16:9' or '9:16'.")


def seconds_to_frame_excl(seconds: float, fps: int) -> int:
    ms = max(0, int(round(float(seconds) * 1000.0)))
    return max(2, 2 + ((ms * int(fps) + 500) // 1000))


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


def clear_vse(scene: bpy.types.Scene) -> None:
    se = _ensure_sequence_editor(scene)
    for s in list(_all_strips(se)):
        try:
            _remove_strip(se, s)
        except Exception:
            pass
    for s in list(_all_strips(se)):
        try:
            _remove_strip(se, s)
        except Exception:
            pass


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


def mute_movie_audio(strip: bpy.types.Sequence) -> None:
    if hasattr(strip, "volume"):
        try:
            strip.volume = 0.0
        except Exception:
            pass


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


def strip_final_window(strip: bpy.types.Sequence) -> Tuple[int, int]:
    start = int(getattr(strip, "frame_final_start", getattr(strip, "frame_start", 0)))
    dur = int(getattr(strip, "frame_final_duration", getattr(strip, "frame_duration", 0)))
    return start, start + max(0, dur)


def strip_window_matches(strip: bpy.types.Sequence, *, start: int, end_excl: int) -> bool:
    got_start, got_end = strip_final_window(strip)
    return got_start == int(start) and got_end == int(end_excl)


def _ensure_exact_window_or_raise(
    strip: bpy.types.Sequence,
    *,
    start: int,
    end_excl: int,
    context: str,
) -> None:
    if strip_window_matches(strip, start=int(start), end_excl=int(end_excl)):
        return
    got_start, got_end = strip_final_window(strip)
    raise RuntimeError(
        "Effects strip window mismatch "
        f"context={context} strip={getattr(strip, 'name', '<unnamed>')!r} "
        f"expected=[{int(start)},{int(end_excl)}) got=[{got_start},{got_end})"
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
    expected_end = int(frame_start) + 1
    set_strip_window(strip, expected_start, expected_end, in_start=int(in_start))
    if not strip_window_matches(strip, start=expected_start, end_excl=expected_end):
        first_start, first_end = strip_final_window(strip)
        first_dur = max(0, int(first_end) - int(first_start))
        set_strip_window(strip, expected_start, expected_end, in_start=int(in_start))
        second_start, second_end = strip_final_window(strip)
        second_dur = max(0, int(second_end) - int(second_start))
        if second_dur > first_dur:
            raise RuntimeError(
                "Effects strip correction broadened window "
                f"context=add_movie_frame strip={getattr(strip, 'name', '<unnamed>')!r} "
                f"before=[{first_start},{first_end}) after=[{second_start},{second_end}) "
                f"expected=[{expected_start},{expected_end})"
            )
    _ensure_exact_window_or_raise(
        strip,
        start=expected_start,
        end_excl=expected_end,
        context="add_movie_frame",
    )
    return strip


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
        "name": f"FX_FLASH_{int(frame_start)}",
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
    strip.frame_start = int(frame_start)
    if hasattr(strip, "frame_end"):
        try:
            strip.frame_end = int(frame_start) + 1
        except Exception:
            pass
    _set_strip_extension_knobs_zero(strip)
    _ensure_exact_window_or_raise(
        strip,
        start=int(frame_start),
        end_excl=int(frame_start) + 1,
        context="add_color_frame",
    )
    return strip


def remove_non_master_audio(scene: bpy.types.Scene, *, master_audio_path: str) -> None:
    se = _ensure_sequence_editor(scene)
    master_abs = str(Path(master_audio_path).expanduser().resolve())
    for s in list(_all_strips(se)):
        if getattr(s, "type", "") != "SOUND":
            continue
        sound = getattr(s, "sound", None)
        fp = getattr(sound, "filepath", "") if sound else ""
        try:
            fp_abs = str(Path(fp).expanduser().resolve())
        except Exception:
            fp_abs = str(fp)
        if fp_abs != master_abs:
            try:
                _remove_strip(se, s)
            except Exception:
                pass


def assert_no_overlaps(scene: bpy.types.Scene) -> None:
    se = _ensure_sequence_editor(scene)
    by_ch: Dict[int, List[bpy.types.Sequence]] = {}
    for s in _all_strips(se):
        by_ch.setdefault(int(getattr(s, "channel", 0)), []).append(s)
    for ch, lst in by_ch.items():
        lst.sort(key=lambda x: int(getattr(x, "frame_final_start", getattr(x, "frame_start", 0))))
        prev_end: Optional[int] = None
        for s in lst:
            start, end_excl = strip_final_window(s)
            if prev_end is not None and start < prev_end:
                print(
                    "effects_overlap_diagnostics "
                    f"channel={ch} strip={getattr(s, 'name', '<unnamed>')!r} "
                    f"strip_start={start} prev_end={prev_end}"
                )
                raise RuntimeError(
                    f"Overlap detected on channel {ch}: '{s.name}' starts {start} < prev_end {prev_end}."
                )
            prev_end = end_excl


def _probe_video_duration_seconds(path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    raw = str(proc.stdout or "").strip().splitlines()
    if not raw:
        raise RuntimeError(f"ffprobe returned empty duration for {path}")
    return float(raw[-1].strip())


def _probe_video_nb_frames(path: str) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_frames",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        return 0
    raw = str(proc.stdout or "").strip().splitlines()
    if not raw:
        return 0
    try:
        return int(raw[-1].strip())
    except Exception:
        return 0


def _resolve_source_total_frames(path: str, *, fps: int) -> int:
    nb_frames = int(_probe_video_nb_frames(path))
    if nb_frames > 0:
        return nb_frames
    duration_s = float(_probe_video_duration_seconds(path))
    estimated = int(round(max(0.0, duration_s) * float(max(1, int(fps)))))
    if estimated <= 0:
        raise RuntimeError(
            f"effects_contract_fail reason=source_frame_probe_failed path={path!r} fps={int(fps)}"
        )
    return estimated


def _load_effects_plan(path: str) -> Dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Effects plan not found: {p}")
    payload = json.loads(p.read_text(encoding="utf-8"))
    if int(payload.get("version", 0)) != 1:
        raise RuntimeError(f"Unsupported effects plan version: {payload.get('version')!r}")
    return payload


def _clamp_render_end_to_base_frames(
    *,
    audio_start_frame: int,
    requested_end_excl: int,
    base_source_frames: int,
    slack_frames: int,
) -> tuple[int, int]:
    start = int(audio_start_frame)
    end_excl = int(requested_end_excl)
    available = max(1, int(base_source_frames))
    required = max(1, end_excl - start)
    deficit = required - available
    if deficit <= 0:
        return end_excl, 0
    if deficit <= max(0, int(slack_frames)):
        return start + available, int(deficit)
    raise RuntimeError(
        "effects_contract_fail reason=base_video_too_short "
        f"required_frames={required} available_frames={available}"
    )


def _validate_frame_ops_contract(frame_ops: Sequence[Dict[str, Any]], *, render_end_excl: int, fps: int) -> None:
    seen: Dict[int, Dict[str, Any]] = {}
    source_total_cache: Dict[str, int] = {}
    for raw in frame_ops:
        frame = int(raw.get("timeline_frame", -1))
        if frame < 1 or frame >= int(render_end_excl):
            raise RuntimeError(f"effects_contract_fail reason=timeline_frame_out_of_range frame={frame}")
        if frame in seen:
            raise RuntimeError(
                f"effects_contract_fail reason=duplicate_timeline_frame frame={frame} prev={seen[frame]} next={raw}"
            )
        op_type = str(raw.get("op_type") or "").strip()
        if op_type not in {"replace_color", "replace_source"}:
            raise RuntimeError(f"effects_contract_fail reason=invalid_op_type frame={frame} op_type={op_type!r}")
        if op_type == "replace_color":
            color = str(raw.get("color") or "").strip().lower()
            if color not in {"black", "white"}:
                raise RuntimeError(f"effects_contract_fail reason=invalid_color frame={frame} color={color!r}")
        if op_type == "replace_source":
            src = str(raw.get("source_clip_path") or "").strip()
            if not src:
                raise RuntimeError(f"effects_contract_fail reason=missing_source_clip frame={frame}")
            if not Path(src).exists():
                raise RuntimeError(f"effects_contract_fail reason=missing_source_file frame={frame} path={src}")
            src_frame = int(raw.get("source_frame", -1))
            if src_frame < 0:
                raise RuntimeError(f"effects_contract_fail reason=invalid_source_frame frame={frame} source_frame={src_frame}")
            if src not in source_total_cache:
                try:
                    source_total_cache[src] = int(_resolve_source_total_frames(src, fps=int(fps)))
                except Exception as exc:
                    raise RuntimeError(
                        "effects_contract_fail reason=source_probe_failed "
                        f"frame={frame} path={src} error={exc}"
                    ) from exc
            source_total = int(source_total_cache[src])
            if src_frame >= source_total:
                raise RuntimeError(
                    "effects_contract_fail reason=source_frame_out_of_range "
                    f"frame={frame} source_frame={src_frame} source_total_frames={source_total} path={src}"
                )
        seen[frame] = dict(raw)


def render_from_audio(scene: bpy.types.Scene) -> None:
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
    cap_end_excl = int(scene.get("_effects_render_end_excl") or 0)
    if cap_end_excl > 1:
        end_excl = min(int(end_excl), int(cap_end_excl))
    if end_excl <= start:
        raise RuntimeError("Computed effects render end is invalid.")

    scene.frame_start = start
    scene.frame_end = max(start, end_excl - 1)
    scene.render.use_sequencer = True

    out_raw = str(scene.get("_effects_output_path") or scene.render.filepath or "").strip()
    if not out_raw:
        raise RuntimeError("Missing effects output path.")
    out_path = Path(out_raw).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path = str(scene.get("_effects_audio_path") or "").strip()
    if not audio_path:
        raise RuntimeError("Missing effects audio path for mux stage.")
    fps = int(scene.get("_effects_fps") or int(scene.render.fps) or 30)

    frames_dir = Path(tempfile.mkdtemp(prefix="vse_effects_frames_"))
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
                "ffmpeg mux failed for effects output: "
                f"code={proc.returncode} stderr={(proc.stderr or '').strip()}"
            )
    finally:
        try:
            shutil.rmtree(frames_dir, ignore_errors=True)
        except Exception:
            pass


def build_effects_timeline(args: argparse.Namespace) -> Tuple[bpy.types.Scene, int]:
    input_video = Path(args.input_video).expanduser().resolve()
    audio_path = Path(args.audio).expanduser().resolve()
    if not input_video.exists():
        raise FileNotFoundError(f"Input video not found: {input_video}")
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    plan = _load_effects_plan(args.effects_plan)
    plan_sha = hashlib.sha1(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    print(
        "effects_plan_loaded "
        f"path={Path(args.effects_plan).expanduser().resolve()} "
        f"segments={len(list(plan.get('segments') or []))} "
        f"frame_ops={len(list(plan.get('frame_ops') or []))} "
        f"plan_sha={plan_sha}"
    )
    scene = bpy.context.scene
    if args.scene:
        if args.scene not in bpy.data.scenes:
            raise ValueError(f"Scene not found: '{args.scene}'.")
        scene = bpy.data.scenes[args.scene]
    clear_vse(scene)
    _ensure_sequence_editor(scene)

    scene.render.fps = int(args.fps)
    scene.render.fps_base = 1.0
    if args.width and args.height:
        scene.render.resolution_x = int(args.width)
        scene.render.resolution_y = int(args.height)
    else:
        w, h = aspect_to_resolution(str(args.aspect))
        scene.render.resolution_x = int(w)
        scene.render.resolution_y = int(h)
    scene.render.resolution_percentage = int(args.resolution_percentage)

    audio_strip = add_sound(scene, str(audio_path), channel=int(args.audio_channel), frame_start=1)
    audio_start = int(audio_strip.frame_start)
    audio_len = int(getattr(audio_strip, "frame_final_duration", 0)) or int(getattr(audio_strip, "frame_duration", 0))
    if audio_len <= 0:
        raise RuntimeError("Master audio strip has unknown duration.")
    audio_end_excl = audio_start + audio_len
    plan_end_excl = int(plan.get("render_end_frame_exclusive", 0) or 0)
    if plan_end_excl > 1:
        audio_end_excl = min(audio_end_excl, plan_end_excl)
    if float(args.render_end_seconds or 0.0) > 0.0:
        audio_end_excl = min(audio_end_excl, seconds_to_frame_excl(float(args.render_end_seconds), int(args.fps)))
    if audio_end_excl <= audio_start:
        raise RuntimeError("Computed effects render end is invalid.")
    scene["_effects_render_end_excl"] = int(audio_end_excl)

    base_strip = add_movie(scene, str(input_video), channel=int(args.base_video_channel), frame_start=1)
    mute_movie_audio(base_strip)
    base_source_frames = strip_source_frames(base_strip)
    slack_frames = max(0, int(os.environ.get("PIPELINE_EFFECTS_BASE_FRAME_SLACK", "1") or "1"))
    clamped_end_excl, deficit = _clamp_render_end_to_base_frames(
        audio_start_frame=int(audio_start),
        requested_end_excl=int(audio_end_excl),
        base_source_frames=int(base_source_frames),
        slack_frames=int(slack_frames),
    )
    print(
        "base_horizon_check "
        f"requested_end_excl={int(audio_end_excl)} available_frames={int(base_source_frames)} "
        f"slack_frames={int(slack_frames)} clamped_end_excl={int(clamped_end_excl)} "
        f"deficit_frames={int(deficit)}"
    )
    if deficit > 0:
        print(
            "effects_contract_warn reason=base_video_short_clamped "
            f"deficit_frames={int(deficit)} requested_end_excl={int(audio_end_excl)} "
            f"clamped_end_excl={int(clamped_end_excl)} base_source_frames={int(base_source_frames)} "
            f"input_video={input_video}"
        )
        audio_end_excl = int(clamped_end_excl)
        scene["_effects_render_end_excl"] = int(audio_end_excl)

    frame_ops = list(plan.get("frame_ops") or [])
    if frame_ops:
        before = len(frame_ops)
        frame_ops = [op for op in frame_ops if int(op.get("timeline_frame", -1)) < int(audio_end_excl)]
        dropped = before - len(frame_ops)
        if dropped > 0:
            print(
                "effects_contract_warn reason=frame_ops_trimmed_for_clamped_end "
                f"dropped_ops={int(dropped)} render_end_excl={int(audio_end_excl)}"
            )
    _validate_frame_ops_contract(frame_ops, render_end_excl=int(audio_end_excl), fps=int(args.fps))
    frame_ops_sorted = sorted(frame_ops, key=lambda x: int(x.get("timeline_frame", 0)))

    replaced = 0
    print(f"effects_apply_start frame_ops={len(frame_ops_sorted)}")
    for op in frame_ops_sorted:
        op_idx = replaced + 1
        frame = int(op.get("timeline_frame", 0))
        op_type = str(op.get("op_type") or "")
        src_for_context = str(op.get("source_clip_path") or "").strip()
        src_frame_for_context = int(op.get("source_frame", -1))
        seg_for_context = int(op.get("segment_index", -1))
        try:
            if op_idx <= 5 or (op_idx % 50) == 0:
                print(
                    "op_apply "
                    f"op_idx={int(op_idx)} timeline_frame={int(frame)} op_type={op_type!r} "
                    f"source_frame={int(src_frame_for_context)} segment_index={int(seg_for_context)}"
                )
            if op_type == "replace_color":
                color = str(op.get("color") or "").strip().lower()
                rgb = (0.0, 0.0, 0.0) if color == "black" else (1.0, 1.0, 1.0)
                add_color_frame(scene, channel=int(args.effects_channel), frame_start=int(frame), rgb=rgb)
            elif op_type == "replace_source":
                add_movie_frame(
                    scene,
                    filepath=src_for_context,
                    channel=int(args.effects_channel),
                    frame_start=int(frame),
                    in_start=int(src_frame_for_context),
                )
            else:
                raise RuntimeError(f"effects_contract_fail reason=unsupported_op_type op_type={op_type!r}")
        except Exception as exc:
            raise RuntimeError(
                "effects_contract_fail reason=op_apply_failed "
                f"timeline_frame={int(frame)} op_type={op_type!r} "
                f"source_frame={int(src_frame_for_context)} "
                f"source_clip_path={src_for_context!r} "
                f"segment_index={int(seg_for_context)} error={exc}"
            ) from exc
        replaced += 1

    remove_non_master_audio(scene, master_audio_path=str(audio_path))
    assert_no_overlaps(scene)

    out_path = Path(args.output).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(out_path)
    scene["_effects_output_path"] = str(out_path)
    scene["_effects_audio_path"] = str(audio_path)
    scene["_effects_fps"] = int(args.fps)
    print(f"effects_apply_done replaced_frames={replaced}")
    return scene, replaced


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-video", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--effects-plan", required=True)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--render-end-seconds", type=float, default=0.0)
    ap.add_argument("--scene", default="")
    ap.add_argument("--output", required=True)
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--resolution-percentage", type=int, default=100)
    ap.add_argument("--base-video-channel", type=int, default=2)
    ap.add_argument("--effects-channel", type=int, default=3)
    ap.add_argument("--audio-channel", type=int, default=1)
    args = ap.parse_args(argv)

    scene, _ = build_effects_timeline(args)
    render_from_audio(scene)

    expected_frames = max(1, int(scene.get("_effects_render_end_excl") or 2) - 1)
    expected_s = float(expected_frames) / float(max(1, int(args.fps)))
    got_s = _probe_video_duration_seconds(str(Path(args.output).expanduser().resolve()))
    got_frames = _probe_video_nb_frames(str(Path(args.output).expanduser().resolve()))
    tolerance_s = 0.20
    tolerance_frames = 2
    if got_s + tolerance_s < expected_s:
        raise RuntimeError(
            "effects_contract_fail reason=duration_too_short "
            f"expected_s={expected_s:.3f} got_s={got_s:.3f} tolerance_s={tolerance_s:.3f}"
        )
    if got_frames > 0 and got_frames + tolerance_frames < expected_frames:
        raise RuntimeError(
            "effects_contract_fail reason=frame_count_too_short "
            f"expected_frames={expected_frames} got_frames={got_frames} tolerance_frames={tolerance_frames}"
        )
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

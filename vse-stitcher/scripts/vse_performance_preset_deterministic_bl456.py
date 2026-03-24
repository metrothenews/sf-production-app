#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import random
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


TOKEN_RE = re.compile(r"(\d+)([a-fA-F])([a-zA-Z]*)")
ALLOWED_MODIFIERS = {"r", "b", "w"}
SYNC_MODE_TIMELINE_LOCKED = "timeline_locked"
SYNC_MODE_LEGACY_RANDOM = "legacy_random"
SHORT_CLIP_POLICY_WRAP_LOOP = "wrap_loop"
SHORT_CLIP_POLICY_CLAMP = "clamp"
SHORT_CLIP_POLICY_FAIL = "fail"


@dataclass(frozen=True)
class SourceSpan:
    start_frame: int
    frame_count: int


@dataclass(frozen=True)
class SourceExtractionPlan:
    spans: list[SourceSpan]
    pad_frames: int
    wraps_before: int
    wraps_in_token: int


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg command failed rc={proc.returncode} cmd={' '.join(cmd)} "
            f"stderr={str(proc.stderr or '').strip()}"
        )


def _extract_user_args_from_blender_argv(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    return args[args.index("--") + 1 :] if "--" in args else []


def _ffprobe_duration(path: str) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {str(proc.stderr or '').strip()}")
    raw = str(proc.stdout or "").strip().splitlines()
    if not raw:
        raise RuntimeError(f"ffprobe returned empty duration for {path}")
    return max(0.0, float(raw[-1]))


def _duration_to_frames(duration_s: float, fps: int) -> int:
    return max(1, int(round(float(duration_s) * float(fps))))


def _frames_to_seconds(frames: int, fps: int) -> float:
    return max(0.0, float(frames) / float(max(1, int(fps))))


def _seconds_to_frames(seconds: float, fps: int) -> int:
    return max(0, int(round(max(0.0, float(seconds)) * float(max(1, int(fps))))))


def _ffprobe_video_frame_count(path: str, fps: int) -> int:
    proc = subprocess.run(
        [
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
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        raw = str(proc.stdout or "").strip().splitlines()
        if raw:
            try:
                out = int(raw[-1].strip())
                if out > 0:
                    return out
            except Exception:
                pass
    return _duration_to_frames(_ffprobe_duration(path), fps)


def _parse_preset(preset: str) -> list[dict[str, Any]]:
    raw = str(preset or "").strip()
    if not raw:
        raise RuntimeError("Preset is required.")
    out: list[dict[str, Any]] = []
    idx = 0
    while idx < len(raw):
        m = TOKEN_RE.match(raw, idx)
        if not m:
            raise RuntimeError(f"Invalid preset token at offset {idx}: {raw!r}")
        duration_text, clip_label, modifiers_raw = m.groups()
        duration = int(duration_text)
        if duration <= 0:
            raise RuntimeError(f"Preset duration must be > 0: {raw!r}")
        modifiers = str(modifiers_raw or "").lower()
        for ch in modifiers:
            if ch not in ALLOWED_MODIFIERS:
                raise RuntimeError(f"Unsupported preset modifier {ch!r}; allowed: r,b,w")
        out.append({"duration": duration, "clip": clip_label.lower(), "modifiers": modifiers})
        idx = m.end()
    if not out:
        raise RuntimeError(f"Preset has no valid tokens: {raw!r}")
    return out


def _allocate_token_frames(tokens: list[dict[str, Any]], total_frames: int) -> list[int]:
    count = len(tokens)
    if count <= 0:
        raise RuntimeError("Token allocation requires at least one preset token.")
    target = int(total_frames)
    if target < count:
        raise RuntimeError(
            f"Token allocation impossible: total_frames={target} token_count={count} (need >= token_count)."
        )
    weights = [max(0, int(t.get("duration") or 0)) for t in tokens]
    total_weight = sum(weights)
    if total_weight <= 0:
        raise RuntimeError("Token allocation requires positive total token duration weight.")

    out = [1 for _ in tokens]
    remaining = target - count
    if remaining <= 0:
        return out

    raw = [(float(remaining) * float(w)) / float(total_weight) for w in weights]
    floors = [int(math.floor(x)) for x in raw]
    for i, val in enumerate(floors):
        out[i] += val

    assigned = sum(out)
    rest = target - assigned
    order = sorted(range(count), key=lambda i: (-(raw[i] - float(floors[i])), i))
    for idx in order:
        if rest <= 0:
            break
        out[idx] += 1
        rest -= 1
    if rest != 0:
        raise RuntimeError(f"Token allocation failed to settle remainder; rest={rest}.")
    if sum(out) != target:
        raise RuntimeError(
            f"Token allocation mismatch: expected_frames={target} actual_frames={sum(out)} allocation={out}"
        )
    if any(v <= 0 for v in out):
        raise RuntimeError(f"Token allocation produced non-positive token frames: {out}")
    return out


def _token_start_frames(timeline_offset_frames: int, token_frames: Sequence[int]) -> list[int]:
    cursor = max(0, int(timeline_offset_frames))
    out: list[int] = []
    for frames in token_frames:
        out.append(cursor)
        cursor += max(0, int(frames))
    return out


def _normalize_sync_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {SYNC_MODE_TIMELINE_LOCKED, SYNC_MODE_LEGACY_RANDOM}:
        raise RuntimeError(
            "Invalid sync mode; expected one of: timeline_locked, legacy_random "
            f"(got {value!r})."
        )
    return mode


def _normalize_short_clip_policy(value: str) -> str:
    policy = str(value or "").strip().lower()
    if policy not in {
        SHORT_CLIP_POLICY_WRAP_LOOP,
        SHORT_CLIP_POLICY_CLAMP,
        SHORT_CLIP_POLICY_FAIL,
    }:
        raise RuntimeError(
            "Invalid short clip policy; expected one of: wrap_loop, clamp, fail "
            f"(got {value!r})."
        )
    return policy


def _flash_frame_count(modifiers: str, token_frames: int, fps: int) -> int:
    mods = str(modifiers or "").strip().lower()
    if "b" not in mods and "w" not in mods:
        return 0
    total_frames = max(0, int(token_frames))
    if total_frames <= 1:
        return 0
    max_flash_frames = max(1, int(round(0.06 * float(max(1, int(fps))))))
    suggested = max(1, int(round(0.15 * float(total_frames))))
    flash_frames = min(max_flash_frames, suggested, total_frames - 1)
    return max(0, int(flash_frames))


def _plan_source_extraction(
    *,
    clip_total_frames: int,
    absolute_start_frame: int,
    frame_count: int,
    short_clip_policy: str,
) -> SourceExtractionPlan:
    clip_frames = max(1, int(clip_total_frames))
    required_frames = max(0, int(frame_count))
    if required_frames <= 0:
        raise RuntimeError("Source extraction requires frame_count > 0.")
    abs_start = max(0, int(absolute_start_frame))
    policy = _normalize_short_clip_policy(short_clip_policy)

    if policy == SHORT_CLIP_POLICY_WRAP_LOOP:
        wraps_before = abs_start // clip_frames
        cursor = abs_start % clip_frames
        rem = required_frames
        spans: list[SourceSpan] = []
        wraps_in_token = 0
        while rem > 0:
            take = min(rem, clip_frames - cursor)
            spans.append(SourceSpan(start_frame=cursor, frame_count=take))
            rem -= take
            if rem > 0:
                wraps_in_token += 1
                cursor = 0
        return SourceExtractionPlan(
            spans=spans,
            pad_frames=0,
            wraps_before=wraps_before,
            wraps_in_token=wraps_in_token,
        )

    if policy == SHORT_CLIP_POLICY_FAIL:
        if abs_start + required_frames > clip_frames:
            raise RuntimeError(
                "Short clip policy=fail violated: "
                f"clip_total_frames={clip_frames} start_frame={abs_start} required_frames={required_frames}."
            )
        return SourceExtractionPlan(
            spans=[SourceSpan(start_frame=abs_start, frame_count=required_frames)],
            pad_frames=0,
            wraps_before=0,
            wraps_in_token=0,
        )

    # SHORT_CLIP_POLICY_CLAMP
    if clip_frames >= required_frames:
        max_start = clip_frames - required_frames
        start = min(abs_start, max_start)
        return SourceExtractionPlan(
            spans=[SourceSpan(start_frame=start, frame_count=required_frames)],
            pad_frames=0,
            wraps_before=0,
            wraps_in_token=0,
        )
    start = min(abs_start, clip_frames - 1)
    available = max(1, clip_frames - start)
    take = min(required_frames, available)
    pad = max(0, required_frames - take)
    return SourceExtractionPlan(
        spans=[SourceSpan(start_frame=start, frame_count=take)],
        pad_frames=pad,
        wraps_before=0,
        wraps_in_token=0,
    )


def _aspect_to_resolution(aspect: str) -> tuple[int, int]:
    key = str(aspect or "").strip().lower()
    if key in {"9:16", "9x16", "vertical"}:
        return 1080, 1920
    if key in {"16:9", "16x9", "horizontal"}:
        return 1920, 1080
    if key in {"1:1", "1x1", "square"}:
        return 1080, 1080
    return 1080, 1920


def _build_segment_from_clip(
    *,
    clip_path: str,
    output_path: str,
    duration_s: float,
    start_s: float,
    fps: int,
    width: int,
    height: int,
) -> None:
    vf = [f"scale={width}:{height}:force_original_aspect_ratio=decrease"]
    vf.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
    vf.append(f"fps={int(fps)}")
    vf.append("format=yuv420p")
    _run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{max(0.0, start_s):.6f}",
            "-t",
            f"{max(1.0 / float(max(1, int(fps))), duration_s):.6f}",
            "-i",
            clip_path,
            "-an",
            "-vf",
            ",".join(vf),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            output_path,
        ]
    )


def _build_flash_segment(
    *,
    output_path: str,
    duration_s: float,
    fps: int,
    width: int,
    height: int,
    color: str,
) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:size={width}x{height}:rate={int(fps)}",
            "-t",
            f"{max(1.0 / float(max(1, int(fps))), duration_s):.6f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            output_path,
        ]
    )


def _concat_videos(paths: list[str], output_path: str) -> None:
    if not paths:
        raise RuntimeError("No video segments to concat.")
    list_file = Path(tempfile.gettempdir()) / f"perf_concat_{Path(output_path).stem}.txt"
    list_file.write_text("\n".join(f"file '{p}'" for p in paths), encoding="utf-8")
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            output_path,
        ]
    )


def _pad_tail_with_freeze(*, input_path: str, output_path: str, pad_frames: int, fps: int) -> None:
    if int(pad_frames) <= 0:
        shutil.copy2(input_path, output_path)
        return
    pad_seconds = _frames_to_seconds(int(pad_frames), fps)
    vf = f"tpad=stop_mode=clone:stop_duration={pad_seconds:.6f},fps={int(fps)},format=yuv420p"
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-an",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            output_path,
        ]
    )


def _reverse_video(*, input_path: str, output_path: str, fps: int) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-an",
            "-vf",
            f"reverse,fps={int(fps)},format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            output_path,
        ]
    )


def _build_clip_portion(
    *,
    clip_path: str,
    work_dir: Path,
    token_tag: str,
    start_frame: int,
    frame_count: int,
    clip_total_frames: int,
    short_clip_policy: str,
    fps: int,
    width: int,
    height: int,
    reverse: bool,
) -> tuple[str, SourceExtractionPlan]:
    plan = _plan_source_extraction(
        clip_total_frames=clip_total_frames,
        absolute_start_frame=start_frame,
        frame_count=frame_count,
        short_clip_policy=short_clip_policy,
    )
    span_paths: list[str] = []
    for span_idx, span in enumerate(plan.spans, start=1):
        span_path = str(work_dir / f"{token_tag}_span_{span_idx:02d}.mp4")
        _build_segment_from_clip(
            clip_path=clip_path,
            output_path=span_path,
            duration_s=_frames_to_seconds(span.frame_count, fps),
            start_s=_frames_to_seconds(span.start_frame, fps),
            fps=fps,
            width=width,
            height=height,
        )
        span_paths.append(span_path)

    extracted_path = span_paths[0]
    if len(span_paths) > 1:
        extracted_path = str(work_dir / f"{token_tag}_spans.mp4")
        _concat_videos(span_paths, extracted_path)

    if plan.pad_frames > 0:
        padded_path = str(work_dir / f"{token_tag}_padded.mp4")
        _pad_tail_with_freeze(
            input_path=extracted_path,
            output_path=padded_path,
            pad_frames=plan.pad_frames,
            fps=fps,
        )
        extracted_path = padded_path

    if reverse:
        reversed_path = str(work_dir / f"{token_tag}_reversed.mp4")
        _reverse_video(input_path=extracted_path, output_path=reversed_path, fps=fps)
        extracted_path = reversed_path

    return extracted_path, plan


def _mux_master_audio(*, video_path: str, audio_path: str, output_path: str) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            output_path,
        ]
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--fps", type=int, required=True)
    ap.add_argument("--aspect", required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeline-offset-seconds", type=float, default=0.0)
    ap.add_argument("--sync-mode", default=SYNC_MODE_TIMELINE_LOCKED)
    ap.add_argument("--short-clip-policy", default=SHORT_CLIP_POLICY_WRAP_LOOP)
    ap.add_argument("--output", required=True)
    ap.add_argument("--render-end-seconds", type=float, default=0.0)
    for label in ("a", "b", "c", "d", "e", "f"):
        ap.add_argument(f"--clip-{label}", default="")
    args = ap.parse_args(argv)

    fps = int(args.fps)
    if fps <= 0:
        raise RuntimeError(f"Invalid fps; expected > 0 (got {fps}).")
    sync_mode = _normalize_sync_mode(args.sync_mode)
    short_clip_policy = _normalize_short_clip_policy(args.short_clip_policy)

    clip_map: dict[str, str] = {}
    for label in ("a", "b", "c", "d", "e", "f"):
        path = str(getattr(args, f"clip_{label}") or "").strip()
        if path:
            clip_map[label] = path
    if not clip_map:
        raise RuntimeError("At least one --clip-* input is required.")

    tokens = _parse_preset(args.preset)
    required_labels = sorted({str(t.get("clip") or "").strip().lower() for t in tokens})
    missing = [lab for lab in required_labels if lab and lab not in clip_map]
    if missing:
        raise RuntimeError(f"Preset references missing clip labels={missing}; provided={sorted(clip_map.keys())}")

    audio_duration = _ffprobe_duration(args.audio)
    target_duration = float(args.render_end_seconds) if float(args.render_end_seconds) > 0 else audio_duration
    if target_duration <= 0:
        raise RuntimeError("Unable to determine target duration.")

    cut_total_frames = _seconds_to_frames(target_duration, fps)
    if cut_total_frames <= 0:
        raise RuntimeError(
            f"Invalid cut duration after conversion to frames: target_duration={target_duration:.6f} fps={fps}"
        )
    token_frames = _allocate_token_frames(tokens, cut_total_frames)
    timeline_offset_frames = _seconds_to_frames(float(args.timeline_offset_seconds), fps)
    token_start_frames = _token_start_frames(timeline_offset_frames, token_frames)

    clip_frame_counts = {label: _ffprobe_video_frame_count(path, fps) for label, path in clip_map.items()}
    width, height = _aspect_to_resolution(args.aspect)
    rng = random.Random(int(args.seed))

    work_dir = Path(tempfile.gettempdir()) / f"performance_preset_{Path(args.output).stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    built_parts: list[str] = []

    for idx, token in enumerate(tokens, start=1):
        clip_label = str(token.get("clip") or "").strip().lower()
        modifiers = str(token.get("modifiers") or "").lower()
        token_total_frames = int(token_frames[idx - 1])
        token_start_frame = int(token_start_frames[idx - 1])
        flash_color = "black" if "b" in modifiers else ("white" if "w" in modifiers else "")
        flash_frames = _flash_frame_count(modifiers, token_total_frames, fps)
        clip_frames_needed = token_total_frames - flash_frames

        clip_path = clip_map[clip_label]
        clip_total_frames = int(clip_frame_counts[clip_label])

        if sync_mode == SYNC_MODE_TIMELINE_LOCKED:
            clip_start_frame = token_start_frame + flash_frames
        else:
            max_start = max(0, clip_total_frames - max(0, clip_frames_needed))
            clip_start_frame = int(rng.randrange(0, max_start + 1)) if max_start > 0 else 0

        token_tag = f"token_{idx:03d}"
        clip_part_path = ""
        plan = SourceExtractionPlan(spans=[], pad_frames=0, wraps_before=0, wraps_in_token=0)
        if clip_frames_needed > 0:
            clip_part_path, plan = _build_clip_portion(
                clip_path=clip_path,
                work_dir=work_dir,
                token_tag=token_tag,
                start_frame=clip_start_frame,
                frame_count=clip_frames_needed,
                clip_total_frames=clip_total_frames,
                short_clip_policy=short_clip_policy,
                fps=fps,
                width=width,
                height=height,
                reverse=("r" in modifiers),
            )

        token_parts: list[str] = []
        if flash_frames > 0 and flash_color:
            flash_path = str(work_dir / f"{token_tag}_flash.mp4")
            _build_flash_segment(
                output_path=flash_path,
                duration_s=_frames_to_seconds(flash_frames, fps),
                fps=fps,
                width=width,
                height=height,
                color=flash_color,
            )
            token_parts.append(flash_path)
        if clip_part_path:
            token_parts.append(clip_part_path)
        if not token_parts:
            raise RuntimeError(f"Token produced no output parts at idx={idx}.")

        final_part = token_parts[0]
        if len(token_parts) > 1:
            final_part = str(work_dir / f"{token_tag}_joined.mp4")
            _concat_videos(token_parts, final_part)
        built_parts.append(final_part)

        print(
            "performance_sync_token "
            f"idx={idx} "
            f"clip={clip_label} "
            f"mode={sync_mode} "
            f"policy={short_clip_policy} "
            f"token_frames={token_total_frames} "
            f"token_start_frame={token_start_frame} "
            f"token_start_s={_frames_to_seconds(token_start_frame, fps):.6f} "
            f"clip_start_frame={clip_start_frame} "
            f"clip_start_s={_frames_to_seconds(clip_start_frame, fps):.6f} "
            f"clip_frames={clip_frames_needed} "
            f"flash_frames={flash_frames} "
            f"wraps_before={plan.wraps_before} "
            f"wraps_in_token={plan.wraps_in_token} "
            f"pad_frames={plan.pad_frames}"
        )
        if plan.wraps_before > 0 or plan.wraps_in_token > 0 or plan.pad_frames > 0:
            print(
                "performance_sync_warning "
                f"idx={idx} "
                f"clip={clip_label} "
                f"wraps_before={plan.wraps_before} "
                f"wraps_in_token={plan.wraps_in_token} "
                f"pad_frames={plan.pad_frames}"
            )

    if not built_parts:
        raise RuntimeError("No rendered performance token parts were produced.")
    if len(built_parts) == 1:
        base_video = built_parts[0]
    else:
        base_video = str(work_dir / "base_video.mp4")
        _concat_videos(built_parts, base_video)

    out_path = Path(args.output).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _mux_master_audio(video_path=base_video, audio_path=args.audio, output_path=str(out_path))
    print(f"performance_output={out_path}")
    return 0


if __name__ == "__main__":
    user_args = _extract_user_args_from_blender_argv(sys.argv)
    raise SystemExit(main(user_args))

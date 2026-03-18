#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any


MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".wav", ".mp3", ".m4a"}


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def _probe_media(path: Path) -> dict[str, Any]:
    proc = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(path),
        ]
    )
    return {
        "path": str(path),
        "returncode": int(proc.returncode),
        "stdout_json": json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else {},
        "stderr": str(proc.stderr or "").strip(),
    }


def _probe_duration_seconds(path: Path) -> float:
    proc = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    if proc.returncode != 0:
        return 0.0
    try:
        return max(0.0, float(str(proc.stdout or "").strip().splitlines()[-1]))
    except Exception:
        return 0.0


def _capture_env_snapshot(out_path: Path) -> None:
    blender_proc = _run(["/Applications/Blender.app/Contents/MacOS/Blender", "--version"])
    if blender_proc.returncode != 0:
        blender_proc = _run(["blender", "--version"])
    ffmpeg_proc = _run(["ffmpeg", "-version"])
    git_proc = _run(["git", "rev-parse", "--short", "HEAD"])
    snapshot = {
        "captured_utc": _utc_now_iso(),
        "blender_version": str(blender_proc.stdout or "").strip(),
        "ffmpeg_version": str(ffmpeg_proc.stdout or "").strip(),
        "git_head_short": str(git_proc.stdout or "").strip(),
        "selected_env": {
            key: str(os.environ.get(key) or "")
            for key in sorted(
                k
                for k in os.environ.keys()
                if k.startswith("PIPELINE_")
                or k.startswith("BLENDER_")
                or k.startswith("DRIVE_API_")
                or k in {"CONTROL_SHEET_ID", "SHEETS_JSON_BASE_URL"}
            )
        },
    }
    out_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _collect_logs(run_dir: Path, out_dir: Path) -> list[str]:
    copied: list[str] = []
    logs = run_dir / "logs"
    if not logs.exists():
        return copied
    for p in sorted(logs.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(run_dir)
        target = out_dir / "logs" / rel.name
        if _copy_if_exists(p, target):
            copied.append(str(rel))
    return copied


def _collect_effects_plans(run_dir: Path, out_dir: Path) -> list[str]:
    copied: list[str] = []
    outputs = run_dir / "outputs"
    if not outputs.exists():
        return copied
    for p in sorted(outputs.glob("*_effects_plan.json")):
        target = out_dir / "effects_plans" / p.name
        if _copy_if_exists(p, target):
            copied.append(str(p.relative_to(run_dir)))
    return copied


def _collect_media_files(run_dir: Path) -> list[Path]:
    out: list[Path] = []
    for sub in ("outputs", "inputs"):
        root = run_dir / sub
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
                out.append(p)
    return out


def _write_probes(media_files: list[Path], out_dir: Path) -> list[str]:
    wrote: list[str] = []
    probes_dir = out_dir / "probes"
    probes_dir.mkdir(parents=True, exist_ok=True)
    for p in media_files:
        info = _probe_media(p)
        target = probes_dir / f"{p.stem}.ffprobe.json"
        target.write_text(json.dumps(info, indent=2, sort_keys=True), encoding="utf-8")
        wrote.append(str(target))
    return wrote


def _sample_frames(media_files: list[Path], out_dir: Path, *, frame_samples: int) -> list[str]:
    generated: list[str] = []
    samples_dir = out_dir / "frame_samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    max_targets = max(1, min(4, len(media_files)))
    for media in media_files[:max_targets]:
        if media.suffix.lower() not in {".mp4", ".mov", ".mkv", ".avi"}:
            continue
        duration = _probe_duration_seconds(media)
        if duration <= 0.0:
            continue
        clip_dir = samples_dir / media.stem
        clip_dir.mkdir(parents=True, exist_ok=True)
        sample_count = max(1, int(frame_samples))
        for idx in range(sample_count):
            frac = (idx + 1) / float(sample_count + 1)
            ts = max(0.0, min(duration, duration * frac))
            out_png = clip_dir / f"sample_{idx+1:02d}.png"
            proc = _run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{ts:.3f}",
                    "-i",
                    str(media),
                    "-frames:v",
                    "1",
                    str(out_png),
                ]
            )
            if proc.returncode == 0 and out_png.exists():
                generated.append(str(out_png))
    return generated


def _zip_dir(source_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(source_dir.rglob("*")):
            if not p.is_file():
                continue
            if p.resolve() == zip_path.resolve():
                continue
            zf.write(p, arcname=str(p.relative_to(source_dir)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a render diagnostics bundle.")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--render-job-id", default="")
    ap.add_argument("--error", default="")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--frame-samples", type=int, default=12)
    args = ap.parse_args()

    run_dir = Path(args.run_dir).expanduser().resolve()
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    logs = _collect_logs(run_dir, out_dir)
    plans = _collect_effects_plans(run_dir, out_dir)
    media = _collect_media_files(run_dir)
    probes = _write_probes(media, out_dir)
    frames = _sample_frames(media, out_dir, frame_samples=max(1, int(args.frame_samples)))
    env_path = out_dir / "environment.json"
    _capture_env_snapshot(env_path)

    summary = {
        "generated_utc": _utc_now_iso(),
        "run_id": str(args.run_id),
        "render_job_id": str(args.render_job_id or ""),
        "error": str(args.error or ""),
        "run_dir": str(run_dir),
        "output_dir": str(out_dir),
        "logs_copied": logs,
        "effects_plans_copied": plans,
        "media_files": [str(p) for p in media],
        "probes_written": probes,
        "frame_samples_written": frames,
        "environment_snapshot": str(env_path),
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    bundle_path = out_dir / "bundle.zip"
    _zip_dir(out_dir, bundle_path)
    print(
        f"forensics_bundle_written run_id={args.run_id} summary={summary_path} "
        f"bundle={bundle_path} logs={len(logs)} probes={len(probes)} frames={len(frames)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

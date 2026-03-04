#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

# Allow importing from ../src without installing a package
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.drive_proxy import DriveProxyError, from_env as drive_proxy_from_env  # noqa: E402
from so_sync.drive_upload import DriveUploadError, from_env as drive_upload_from_env  # noqa: E402
from so_sync.gateway import SheetsGateway  # noqa: E402
from so_sync.repositories import normalize_enabled_value  # noqa: E402
from so_sync.render_contract import (  # noqa: E402
    RENDER_STATUS_FAILED,
    RENDER_STATUS_QUEUED,
    RENDER_STATUS_RETRY,
    RENDER_STATUS_RUNNING,
    RENDER_STATUS_SUCCESS,
)
from so_sync.render_leaser import lease_one_render_job, mark_failed, mark_running, mark_success  # noqa: E402
from so_sync.render_repo import (  # noqa: E402
    append_outputs_index_rows,
    count_outputs_for_run,
    read_outputs_index_all,
    read_render_jobs_all,
    upsert_render_run,
)

DEFAULT_BASE = "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"
WORKSPACE_ROOT = REPO_ROOT.parent
_MAC_BLENDER_BIN = "/Applications/Blender.app/Contents/MacOS/Blender"
DEFAULT_BLENDER_BIN = _MAC_BLENDER_BIN if Path(_MAC_BLENDER_BIN).exists() else "blender"
VSE_TEXT_SCRIPT_PATH = os.environ.get("VSE_TEXT_SCRIPT_PATH", "").strip() or str(
    (WORKSPACE_ROOT / "vse-text" / "scripts" / "vse_bpm_preset_deterministic_bl456.py").resolve()
)
VSE_STITCHER_SCRIPT_PATH = os.environ.get("VSE_STITCHER_SCRIPT_PATH", "").strip() or str(
    (WORKSPACE_ROOT / "vse-stitcher" / "scripts" / "vse_bpm_preset_deterministic_bl456.py").resolve()
)
DEFAULT_FFMPEG_BIN = "ffmpeg"


def _must_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _to_int(raw: str, fallback: int) -> int:
    try:
        return int(str(raw).strip())
    except Exception:
        return fallback


def _to_float(raw: Any, fallback: float) -> float:
    try:
        return float(str(raw).strip())
    except Exception:
        return fallback


def _debug_job(job: Dict[str, Any]) -> None:
    if os.environ.get("DEBUG_DUMP", "0").strip() != "1":
        return
    printable = {k: job.get(k) for k in sorted(job.keys())}
    print(f"DEBUG_RENDER_JOB {json.dumps(printable, sort_keys=True, separators=(',', ':'))}")


def _lease_diagnostics(job_rows: list[Dict[str, Any]], sample_limit: int = 5) -> str:
    enabled_count = sum(1 for r in job_rows if normalize_enabled_value(r.get("enabled")))
    eligible_count = sum(
        1
        for r in job_rows
        if normalize_enabled_value(r.get("enabled"))
        and str(r.get("status") or "").strip().lower() in {RENDER_STATUS_QUEUED, RENDER_STATUS_RETRY}
    )
    sample = [
        {
            "render_job_id": str(r.get("render_job_id") or "").strip(),
            "status": str(r.get("status") or "").strip().lower(),
            "enabled": normalize_enabled_value(r.get("enabled")),
        }
        for r in job_rows[:sample_limit]
    ]
    return (
        f"No render jobs leased. enabled_jobs={enabled_count} eligible_jobs={eligible_count} "
        f"sample={json.dumps(sample, sort_keys=True, separators=(',', ':'))}"
    )


def _new_render_run(run_id: str, render_job_id: str, status: str, now: str, dedupe_key: str | None = None) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "run_type": "render",
        "render_job_id": render_job_id,
        "status": status,
        "started_utc": None,
        "finished_utc": None,
        "dedupe_key": dedupe_key,
        "error_msg": None,
        "artifacts_written": 0,
        "created_utc": now,
        "updated_utc": now,
    }


def _write_noop_artifact_local(render_job_id: str) -> tuple[str, int, str]:
    content = f"noop artifact for {render_job_id}\n".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    name = f"{render_job_id}.noop.txt"
    path = Path(tempfile.gettempdir()) / name
    path.write_bytes(content)
    return str(path), len(content), digest


def _output_id_for(run_id: str, artifact_type: str, path: str) -> str:
    seed = f"{run_id}:{artifact_type}:{path}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def _basename(path: str) -> str:
    return Path(path).name


def _file_bytes_sha256(path: str) -> tuple[int, str]:
    data = Path(path).read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def _sanitize_label(label: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in str(label).strip())
    return safe or "clip"


def build_pipeline_workdir_paths(run_id: str, *, base_tmp_dir: str | None = None) -> dict[str, str]:
    root = Path(base_tmp_dir or tempfile.gettempdir()) / "so_work" / run_id
    return {
        "root": str(root),
        "inputs": str(root / "inputs"),
        "clips": str(root / "inputs" / "clips"),
        "outputs": str(root / "outputs"),
        "logs": str(root / "logs"),
    }


def ensure_pipeline_workdirs(paths: dict[str, str]) -> dict[str, str]:
    for key in ("root", "inputs", "clips", "outputs", "logs"):
        Path(paths[key]).mkdir(parents=True, exist_ok=True)
    return paths


def build_clip_filename(label: str, drive_file_id: str, ext: str = ".mp4") -> str:
    return f"{_sanitize_label(label)}__{drive_file_id}{ext}"


def resolve_pipeline_v1_input_ids(manifest: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    inputs = manifest.get("inputs", {}) if isinstance(manifest, dict) else {}
    clips_raw = inputs.get("clips", [])
    clips: list[dict[str, str]] = []
    if isinstance(clips_raw, list):
        for i, raw in enumerate(clips_raw, start=1):
            if not isinstance(raw, dict):
                continue
            drive_id = str(raw.get("drive_file_id") or "").strip()
            if not drive_id:
                continue
            label = str(raw.get("label") or f"clip_{i:03d}").strip()
            clips.append({"drive_file_id": drive_id, "label": label})
    audio_id = str(inputs.get("audio_drive_file_id") or job.get("audio_drive_file_id") or "").strip()
    lyrics_id = str(inputs.get("lyrics_srt_drive_file_id") or job.get("srt_drive_file_id") or "").strip()
    copy_id = str(inputs.get("copy_srt_drive_file_id") or job.get("copy_srt_drive_file_id") or "").strip()
    return {
        "audio_drive_file_id": audio_id,
        "lyrics_srt_drive_file_id": lyrics_id,
        "copy_srt_drive_file_id": copy_id,
        "clips": clips,
    }


def plan_pipeline_v1_stages(manifest: dict[str, Any], resolved_inputs: dict[str, Any]) -> list[str]:
    stages = ["stitch", "lyrics"]
    copy_enabled = bool(str(resolved_inputs.get("copy_srt_path") or "").strip())
    copy_cfg = manifest.get("copy_pass", {}) if isinstance(manifest, dict) else {}
    if copy_enabled and str(copy_cfg.get("enabled", "true")).strip().lower() not in {"false", "0", "no"}:
        stages.append("copy")
    return stages


def _safe_link_or_copy(src: str, dst: str) -> None:
    src_path = Path(src)
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        return
    try:
        os.link(src_path, dst_path)
    except Exception:
        shutil.copy2(src_path, dst_path)


def _extract_stitch_categories(stitch_preset: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\d+([A-Za-z])", str(stitch_preset or "")):
        cat = str(m.group(1) or "").upper()
        if not cat or not cat.isalpha() or cat in seen:
            continue
        seen.add(cat)
        out.append(cat)
    return out


def _build_stitcher_media_root(clip_paths: list[str], media_root_dir: str, *, stitch_preset: str = "") -> str:
    if not clip_paths:
        raise RuntimeError("pipeline_v1 requires at least one clip for stitch stage")
    root = Path(media_root_dir)
    root.mkdir(parents=True, exist_ok=True)
    # Mirror clips into every category referenced by the preset.
    # Keep A/B/C as fallback buckets for legacy presets.
    buckets = _extract_stitch_categories(stitch_preset)
    for legacy in ("A", "B", "C"):
        if legacy not in buckets:
            buckets.append(legacy)
    for bucket in buckets:
        bucket_dir = root / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        for i, clip in enumerate(clip_paths, start=1):
            src = Path(clip)
            name = f"{i:03d}_{src.name}"
            _safe_link_or_copy(str(src), str(bucket_dir / name))
    return str(root)


def build_ffmpeg_cut_command(
    *,
    ffmpeg_bin: str,
    input_path: str,
    start: str,
    end: str,
    output_path: str,
) -> list[str]:
    return [
        ffmpeg_bin,
        "-y",
        "-ss",
        start,
        "-to",
        end,
        "-i",
        input_path,
        "-c",
        "copy",
        output_path,
    ]


def _parse_ffmpeg_timestamp_to_seconds(value: str) -> float:
    m = re.match(r"^\s*(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?\s*$", str(value or ""))
    if not m:
        raise ValueError(f"Invalid ffmpeg timestamp: {value!r}")
    hh, mm, ss, ms = m.groups()
    millis = int(str(ms or "0").ljust(3, "0")[:3])
    return (int(hh) * 3600.0) + (int(mm) * 60.0) + int(ss) + (millis / 1000.0)


def _probe_media_duration_seconds(path: str) -> float:
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
    try:
        duration = float(raw[-1].strip())
    except Exception as exc:
        raise RuntimeError(f"Unable to parse ffprobe duration for {path}: {raw[-1]!r}") from exc
    if duration <= 0:
        raise RuntimeError(f"Non-positive media duration for {path}: {duration}")
    return duration


def _log_tail_excerpt(log_path: str, *, max_lines: int = 25, max_chars: int = 1200) -> str:
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = text.splitlines()[-max(1, int(max_lines)) :]
    snippet = "\n".join(lines).strip()
    if len(snippet) > max(80, int(max_chars)):
        snippet = snippet[-max_chars:]
    return snippet


def _pick_cut_source_video(
    *,
    base_mp4: str,
    candidate_mp4: str,
    max_cut_end_s: float,
    candidate_label: str,
    candidate_log_path: str,
    tolerance_s: float = 0.05,
) -> str:
    if max_cut_end_s <= 0:
        return candidate_mp4
    candidate_duration = _probe_media_duration_seconds(candidate_mp4)
    if candidate_duration + tolerance_s >= max_cut_end_s:
        return candidate_mp4
    base_duration = _probe_media_duration_seconds(base_mp4)
    if base_duration + tolerance_s >= max_cut_end_s:
        return base_mp4
    raise RuntimeError(
        "pipeline_v1 rendered media too short for requested cuts "
        f"(max_cut_end_s={max_cut_end_s:.3f} candidate_label={candidate_label} "
        f"candidate_duration_s={candidate_duration:.3f} base_duration_s={base_duration:.3f} "
        f"candidate={candidate_mp4} base={base_mp4} candidate_log={candidate_log_path})."
    )


def build_pipeline_subfolder_name(campaign_id: str, run_id: str) -> str:
    mode = os.environ.get("PIPELINE_OUTPUT_SUBFOLDER_MODE", "campaign").strip().lower()
    if mode == "none":
        return ""
    if mode == "run":
        return f"{_sanitize_label(campaign_id or 'campaign')}__{run_id}"
    # Default: stable campaign folder keeps batch outputs together.
    return _sanitize_label(campaign_id or "campaign")


def should_upload_artifact(
    existing_rows: list[dict[str, Any]],
    *,
    run_id: str,
    artifact_type: str,
    filename: str,
) -> bool:
    for row in existing_rows:
        if str(row.get("run_id") or "").strip() != run_id:
            continue
        if str(row.get("artifact_type") or "").strip() != artifact_type:
            continue
        if str(row.get("filename") or "").strip() != filename:
            continue
        if str(row.get("drive_file_id") or "").strip():
            return False
    return True


def _find_existing_uploaded_artifact(
    existing_rows: list[dict[str, Any]],
    *,
    run_id: str,
    artifact_type: str,
    filename: str,
) -> dict[str, Any] | None:
    for row in existing_rows:
        if str(row.get("run_id") or "").strip() != run_id:
            continue
        if str(row.get("artifact_type") or "").strip() != artifact_type:
            continue
        if str(row.get("filename") or "").strip() != filename:
            continue
        if str(row.get("drive_file_id") or "").strip():
            return row
    return None


def _pipeline_run_ids_with_shorts(rows: list[dict[str, Any]]) -> set[str]:
    run_ids: set[str] = set()
    for row in rows:
        artifact_type = str(row.get("artifact_type") or "").strip().lower()
        if not artifact_type.startswith("pipeline_short_"):
            continue
        run_id = str(row.get("run_id") or "").strip()
        if run_id:
            run_ids.add(run_id)
    return run_ids


def _is_pipeline_final_video_artifact(
    *,
    artifact_type: str,
    run_id: str,
    run_ids_with_shorts: set[str],
) -> bool:
    normalized = str(artifact_type or "").strip().lower()
    if normalized.startswith("pipeline_short_"):
        return True
    return normalized == "pipeline_final_mp4" and str(run_id or "").strip() not in run_ids_with_shorts


def _collect_pipeline_cleanup_drive_file_ids(
    rows: list[dict[str, Any]],
    *,
    render_job_id: str,
    run_ids_with_shorts: set[str],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    wanted_job_id = str(render_job_id or "").strip()
    for row in rows:
        if str(row.get("render_job_id") or "").strip() != wanted_job_id:
            continue
        artifact_type = str(row.get("artifact_type") or "").strip().lower()
        if not artifact_type.startswith("pipeline_"):
            continue
        drive_file_id = str(row.get("drive_file_id") or "").strip()
        if not drive_file_id or drive_file_id in seen:
            continue
        if _is_pipeline_final_video_artifact(
            artifact_type=artifact_type,
            run_id=str(row.get("run_id") or ""),
            run_ids_with_shorts=run_ids_with_shorts,
        ):
            continue
        seen.add(drive_file_id)
        out.append(drive_file_id)
    return out


def _delete_drive_file_best_effort(client: Any, *, drive_file_id: str, reason: str) -> None:
    file_id = str(drive_file_id or "").strip()
    if not file_id:
        return
    deleter = getattr(client, "delete_file", None)
    if not callable(deleter):
        print(f"drive_cleanup_skip file_id={file_id} reason={reason} error=delete_file_not_supported")
        return
    try:
        deleter(file_id)
        print(f"drive_cleanup_deleted file_id={file_id} reason={reason}")
    except Exception as exc:
        print(f"drive_cleanup_skip file_id={file_id} reason={reason} error={exc}")


def build_vse_text_command(job: dict, *, blender_bin: str, script_path: str) -> list[str]:
    cmd = [
        blender_bin,
        "-b",
        "--python",
        script_path,
        "--",
        "--video",
        str(job["input_video_path"]).strip(),
        "--srt",
        str(job["input_srt_path"]).strip(),
        "--audio",
        str(job["input_audio_path"]).strip(),
        "--fps",
        str(job["fps"]).strip(),
        "--aspect",
        str(job["aspect"]).strip(),
        "--preset",
        str(job["preset"]).strip(),
        "--bg-mode",
        str(job["bg_mode"]).strip(),
        "--output",
        str(job["output_local_path"]).strip(),
    ]
    render_end_seconds = _to_float(job.get("render_end_seconds", ""), 0.0)
    if render_end_seconds > 0.0:
        cmd.extend(["--render-end-seconds", f"{render_end_seconds:.3f}"])
    return cmd


def build_outputs_rows_for_vse_text(
    *,
    run_id: str,
    render_job_id: str,
    output_mp4_path: str,
    log_path: str,
    campaign_id: str = "",
    created_utc: str = "",
    mp4_drive_file_id: str = "",
    mp4_drive_folder_id: str = "",
) -> list[dict]:
    mp4_bytes, mp4_sha = _file_bytes_sha256(output_mp4_path)
    log_bytes, log_sha = _file_bytes_sha256(log_path)
    return [
        {
            "output_id": _output_id_for(run_id, "vse_text_mp4", output_mp4_path),
            "run_id": run_id,
            "render_job_id": render_job_id,
            "campaign_id": campaign_id,
            "artifact_type": "vse_text_mp4",
            "drive_file_id": mp4_drive_file_id,
            "drive_folder_id": mp4_drive_folder_id,
            "filename": _basename(output_mp4_path),
            "bytes": mp4_bytes,
            "sha256": mp4_sha,
            "created_utc": created_utc,
            "notes": f"local_path={output_mp4_path}",
        },
        {
            "output_id": _output_id_for(run_id, "vse_text_log", log_path),
            "run_id": run_id,
            "render_job_id": render_job_id,
            "campaign_id": campaign_id,
            "artifact_type": "vse_text_log",
            "drive_file_id": "",
            "drive_folder_id": "",
            "filename": _basename(log_path),
            "bytes": log_bytes,
            "sha256": log_sha,
            "created_utc": created_utc,
            "notes": f"local_path={log_path}",
        },
    ]


def build_vse_stitcher_command(job: dict, *, blender_bin: str, script_path: str) -> list[str]:
    media_root = str(job.get("media_root_path") or "").strip()
    if not media_root:
        media_root = str(job.get("input_manifest_path") or "").strip()
    bpm = str(job.get("bpm") or "").strip()
    if not bpm:
        bpm = str(job.get("stitch_bpm") or "").strip()
    if not bpm:
        bpm = os.environ.get("PIPELINE_STITCH_BPM", "120").strip() or "120"
    cmd = [
        blender_bin,
        "-b",
        "--python",
        script_path,
        "--",
        "--media-root",
        media_root,
        "--audio",
        str(job["input_audio_path"]).strip(),
        "--bpm",
        bpm,
        "--fps",
        str(job["fps"]).strip(),
        "--aspect",
        str(job["aspect"]).strip(),
        "--preset",
        str(job["stitch_preset"]).strip(),
        "--seed",
        str(job["seed"]).strip(),
        "--output",
        str(job["output_local_path"]).strip(),
    ]
    render_end_seconds = _to_float(job.get("render_end_seconds", ""), 0.0)
    if render_end_seconds > 0.0:
        cmd.extend(["--render-end-seconds", f"{render_end_seconds:.3f}"])
    return cmd


def build_outputs_rows_for_vse_stitcher(
    *,
    run_id: str,
    render_job_id: str,
    output_mp4_path: str,
    log_path: str,
    campaign_id: str = "",
    created_utc: str = "",
    mp4_drive_file_id: str = "",
    mp4_drive_folder_id: str = "",
) -> list[dict]:
    mp4_bytes, mp4_sha = _file_bytes_sha256(output_mp4_path)
    log_bytes, log_sha = _file_bytes_sha256(log_path)
    return [
        {
            "output_id": _output_id_for(run_id, "vse_stitcher_mp4", output_mp4_path),
            "run_id": run_id,
            "render_job_id": render_job_id,
            "campaign_id": campaign_id,
            "artifact_type": "vse_stitcher_mp4",
            "drive_file_id": mp4_drive_file_id,
            "drive_folder_id": mp4_drive_folder_id,
            "filename": _basename(output_mp4_path),
            "bytes": mp4_bytes,
            "sha256": mp4_sha,
            "created_utc": created_utc,
            "notes": f"local_path={output_mp4_path}",
        },
        {
            "output_id": _output_id_for(run_id, "vse_stitcher_log", log_path),
            "run_id": run_id,
            "render_job_id": render_job_id,
            "campaign_id": campaign_id,
            "artifact_type": "vse_stitcher_log",
            "drive_file_id": "",
            "drive_folder_id": "",
            "filename": _basename(log_path),
            "bytes": log_bytes,
            "sha256": log_sha,
            "created_utc": created_utc,
            "notes": f"local_path={log_path}",
        },
    ]


def _require_job_field(job: Dict[str, Any], field: str, *, renderer: str) -> str:
    value = str(job.get(field) or "").strip()
    if not value:
        raise RuntimeError(f"renderer={renderer} requires non-empty field: {field}")
    return value


def _build_output_row_from_file(
    *,
    run_id: str,
    render_job_id: str,
    campaign_id: str,
    artifact_type: str,
    file_path: str,
    created_utc: str,
    notes_prefix: str = "local_path=",
) -> dict[str, Any]:
    num_bytes, digest = _file_bytes_sha256(file_path)
    return {
        "output_id": _output_id_for(run_id, artifact_type, file_path),
        "run_id": run_id,
        "render_job_id": render_job_id,
        "campaign_id": campaign_id,
        "artifact_type": artifact_type,
        "drive_file_id": "",
        "drive_folder_id": "",
        "filename": _basename(file_path),
        "bytes": num_bytes,
        "sha256": digest,
        "created_utc": created_utc,
        "notes": f"{notes_prefix}{file_path}",
    }


def _guess_mime_type(path: str) -> str:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".mov":
        return "video/quicktime"
    if suffix == ".json":
        return "application/json"
    if suffix == ".srt":
        return "application/x-subrip"
    if suffix == ".log" or suffix == ".txt":
        return "text/plain"
    return "application/octet-stream"


def _run_to_log(
    cmd: list[str],
    log_path: str,
    *,
    stage: str,
    retries: int = 0,
    retry_delay_seconds: float = 1.0,
) -> None:
    attempts = max(1, int(retries) + 1)
    delay = max(0.0, float(retry_delay_seconds))
    log_file = Path(log_path)
    for attempt in range(1, attempts + 1):
        mode = "wb" if attempt == 1 else "ab"
        with log_file.open(mode) as log_f:
            if attempt > 1:
                log_f.write(f"\n--- retry attempt {attempt}/{attempts} stage={stage} ---\n".encode("utf-8"))
            proc = subprocess.run(cmd, stdout=log_f, stderr=subprocess.STDOUT, check=False)
        if proc.returncode == 0:
            return
        if attempt < attempts:
            time.sleep(delay)
            continue
        tail = _log_tail_excerpt(log_path).replace("\n", " | ")
        raise RuntimeError(
            f"stage={stage} failed exit_code={proc.returncode} attempts={attempts} log_path={log_path} log_tail={tail}"
        )


def _load_json_file(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in manifest: {path}")
    return data


def _proxy_error_reason(exc: DriveProxyError) -> str:
    msg = str(exc)
    m = re.search(r"status=(\d{3})", msg)
    if not m:
        return "proxy_error"
    code = m.group(1)
    if code == "404":
        return "proxy_404"
    if code == "403":
        return "proxy_403"
    if code == "401":
        return "proxy_401"
    return f"proxy_{code}"


def _download_with_fallback(
    drive_file_id: str,
    dst_path: str,
    *,
    proxy_client: Any | None = None,
    upload_client: Any | None = None,
) -> str:
    proxy = proxy_client
    if proxy is None:
        try:
            proxy = drive_proxy_from_env()
        except DriveProxyError as exc:
            reason = "proxy_unconfigured" if "DRIVE_PROXY_BASE_URL is required" in str(exc) else _proxy_error_reason(exc)
            print(f"drive_download_fallback=drive_api file_id={drive_file_id} reason={reason}")
            uploader = upload_client or drive_upload_from_env()
            try:
                return uploader.download_file(drive_file_id, dst_path)
            except DriveUploadError as up_exc:
                raise RuntimeError(str(up_exc)) from up_exc
    try:
        return proxy.download_file(drive_file_id, dst_path)
    except DriveProxyError as exc:
        reason = _proxy_error_reason(exc)
        print(f"drive_download_fallback=drive_api file_id={drive_file_id} reason={reason}")
        uploader = upload_client or drive_upload_from_env()
        try:
            return uploader.download_file(drive_file_id, dst_path)
        except DriveUploadError as up_exc:
            raise RuntimeError(str(up_exc)) from up_exc


def resolve_and_download_pipeline_v1_inputs(job: dict[str, Any], run_id: str) -> dict[str, Any]:
    paths = ensure_pipeline_workdirs(build_pipeline_workdir_paths(run_id))
    manifest_local_path = str(Path(paths["inputs"]) / "manifest.json")

    manifest_path_from_job = str(job.get("input_manifest_path") or "").strip()
    if not manifest_path_from_job:
        manifest_ref = str(job.get("input_manifest_drive_file_id") or "").strip()
        if manifest_ref and Path(manifest_ref).exists():
            manifest_path_from_job = manifest_ref
    if manifest_path_from_job:
        manifest = _load_json_file(manifest_path_from_job)
        Path(manifest_local_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    else:
        manifest_id = str(job.get("input_manifest_drive_file_id") or "").strip()
        if not manifest_id:
            raise RuntimeError("renderer=pipeline_v1 requires input_manifest_drive_file_id or input_manifest_path")
        _download_with_fallback(manifest_id, manifest_local_path)
        manifest = _load_json_file(manifest_local_path)

    resolved_ids = resolve_pipeline_v1_input_ids(manifest, job)

    audio_path = str(job.get("input_audio_path") or "").strip()
    if not audio_path:
        audio_id = str(resolved_ids.get("audio_drive_file_id") or "").strip()
        if not audio_id:
            raise RuntimeError("pipeline_v1 requires audio input via manifest or audio_drive_file_id")
        audio_path = str(Path(paths["inputs"]) / "audio.wav")
        _download_with_fallback(audio_id, audio_path)

    lyrics_srt_path = str(job.get("input_srt_path") or "").strip()
    if not lyrics_srt_path:
        lyrics_id = str(resolved_ids.get("lyrics_srt_drive_file_id") or "").strip()
        if not lyrics_id:
            raise RuntimeError("pipeline_v1 requires lyrics srt via manifest or srt_drive_file_id")
        lyrics_srt_path = str(Path(paths["inputs"]) / "lyrics.srt")
        _download_with_fallback(lyrics_id, lyrics_srt_path)

    copy_srt_path = ""
    copy_from_job = str(job.get("copy_srt_path") or "").strip()
    if copy_from_job:
        copy_srt_path = copy_from_job
    else:
        copy_id = str(resolved_ids.get("copy_srt_drive_file_id") or "").strip()
        if copy_id:
            copy_srt_path = str(Path(paths["inputs"]) / "copy.srt")
            _download_with_fallback(copy_id, copy_srt_path)

    clip_paths: list[str] = []
    clip_specs = list(resolved_ids.get("clips") or [])
    for clip in clip_specs:
        drive_id = str(clip.get("drive_file_id") or "").strip()
        label = str(clip.get("label") or drive_id).strip()
        if not drive_id:
            continue
        clip_path = str(Path(paths["clips"]) / build_clip_filename(label, drive_id))
        _download_with_fallback(drive_id, clip_path)
        clip_paths.append(clip_path)

    return {
        "paths": paths,
        "manifest": manifest,
        "manifest_path": manifest_local_path,
        "audio_path": audio_path,
        "lyrics_srt_path": lyrics_srt_path,
        "copy_srt_path": copy_srt_path,
        "clip_paths": clip_paths,
    }


def _maybe_download_drive_inputs(job: Dict[str, Any]) -> None:
    input_mode = str(job.get("input_mode") or "local").strip().lower()
    if input_mode != "drive":
        return
    drive_id = (
        str(job.get("input_manifest_drive_file_id") or "").strip()
        or str(job.get("audio_drive_file_id") or "").strip()
        or str(job.get("srt_drive_file_id") or "").strip()
    )
    if not drive_id:
        raise RuntimeError("input_mode=drive requires one of input_manifest_drive_file_id/audio_drive_file_id/srt_drive_file_id")
    dst = str(Path(tempfile.gettempdir()) / f"{drive_id}.bin")
    _download_with_fallback(drive_id, dst)


def _handle_output_mode(job: Dict[str, Any], local_path: str, filename: str) -> tuple[str, str]:
    output_mode = str(job.get("output_mode") or "local").strip().lower()
    if output_mode == "local":
        return "", ""
    if output_mode != "drive":
        raise RuntimeError(f"Unsupported output_mode: {output_mode}")
    client = drive_proxy_from_env()
    dst_folder = str(job.get("output_drive_folder_id") or "").strip()
    if not dst_folder:
        raise RuntimeError("output_mode=drive requires output_drive_folder_id")
    try:
        drive_file_id = client.upload_file(local_path, dst_folder, filename)
    except NotImplementedError as e:
        raise RuntimeError(
            "Drive upload not implemented for render agent; next_action=use output_mode=local "
            "or implement drive proxy upload endpoint."
        ) from e
    except DriveProxyError as e:
        raise RuntimeError(str(e)) from e
    return drive_file_id, dst_folder


def run_once() -> int:
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", DEFAULT_BASE).strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    agent_id = os.environ.get("AGENT_ID", socket.gethostname()).strip() or "render-agent"
    lease_seconds = _to_int(os.environ.get("LEASE_SECONDS", "900"), 900)
    idempotency_verify = os.environ.get("IDEMPOTENCY_VERIFY", "0").strip() == "1"

    gw = SheetsGateway(base_url=base_url, api_key=api_key)
    now = _utc_now_iso()
    job = lease_one_render_job(gw, control_sheet_id, now, lease_seconds, agent_id)
    if not job:
        try:
            all_jobs = read_render_jobs_all(gw, control_sheet_id)
            print(_lease_diagnostics(all_jobs))
        except Exception as diag_err:
            print(f"No render jobs leased. diagnostics_error={diag_err}")
        return 0
    _debug_job(job)
    if str(job.get("status") or "").strip().lower() == RENDER_STATUS_SUCCESS:
        raise RuntimeError("Idempotency violated: leased render job is already success.")

    render_job_id = str(job.get("render_job_id") or "").strip()
    run_id = uuid.uuid4().hex[:32]
    dedupe_key = f"render:{render_job_id}:{run_id}"
    run_row = _new_render_run(run_id, render_job_id, RENDER_STATUS_QUEUED, now, dedupe_key=dedupe_key)
    upsert_render_run(gw, control_sheet_id, run_row)

    try:
        started = _utc_now_iso()
        mark_running(gw, control_sheet_id, render_job_id, run_id, now_utc=started)
        run_row["status"] = RENDER_STATUS_RUNNING
        run_row["started_utc"] = started
        run_row["updated_utc"] = started
        upsert_render_run(gw, control_sheet_id, run_row)

        renderer = str(job.get("renderer") or "noop").strip().lower()
        if renderer == "noop":
            _maybe_download_drive_inputs(job)
            local_path, num_bytes, digest = _write_noop_artifact_local(render_job_id)
            output_filename = str(job.get("output_filename") or "").strip() or f"{render_job_id}.noop.txt"
            drive_file_id, drive_folder_id = _handle_output_mode(job, local_path, output_filename)

            created = _utc_now_iso()
            artifact_row = {
                "output_id": uuid.uuid4().hex[:32],
                "run_id": run_id,
                "render_job_id": render_job_id,
                "campaign_id": job.get("campaign_id"),
                "artifact_type": "noop",
                "drive_file_id": drive_file_id,
                "drive_folder_id": drive_folder_id,
                "filename": output_filename,
                "bytes": num_bytes,
                "sha256": digest,
                "created_utc": created,
                "notes": "render_agent noop artifact",
            }
            artifacts_written = append_outputs_index_rows(gw, control_sheet_id, [artifact_row])
        elif renderer == "vse_text":
            _require_job_field(job, "input_video_path", renderer=renderer)
            _require_job_field(job, "input_audio_path", renderer=renderer)
            _require_job_field(job, "input_srt_path", renderer=renderer)
            _require_job_field(job, "preset", renderer=renderer)
            _require_job_field(job, "fps", renderer=renderer)
            _require_job_field(job, "aspect", renderer=renderer)
            _require_job_field(job, "bg_mode", renderer=renderer)
            output_mp4_path = _require_job_field(job, "output_local_path", renderer=renderer)

            blender_bin = os.environ.get("BLENDER_BIN", "").strip() or DEFAULT_BLENDER_BIN
            cmd = build_vse_text_command(job, blender_bin=blender_bin, script_path=VSE_TEXT_SCRIPT_PATH)
            stage_retries = _to_int(os.environ.get("BLENDER_STAGE_RETRIES", "1"), 1)
            stage_retry_delay_s = float(os.environ.get("BLENDER_STAGE_RETRY_DELAY_SECONDS", "1"))

            logs_dir = Path(tempfile.gettempdir()) / "so_render_logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"{run_id}.log"
            _run_to_log(
                cmd,
                str(log_path),
                stage="vse_text",
                retries=stage_retries,
                retry_delay_seconds=stage_retry_delay_s,
            )

            output_filename = str(job.get("output_filename") or "").strip() or Path(output_mp4_path).name
            drive_file_id, drive_folder_id = _handle_output_mode(job, output_mp4_path, output_filename)
            created = _utc_now_iso()
            artifact_rows = build_outputs_rows_for_vse_text(
                run_id=run_id,
                render_job_id=render_job_id,
                output_mp4_path=output_mp4_path,
                log_path=str(log_path),
                campaign_id=str(job.get("campaign_id") or ""),
                created_utc=created,
                mp4_drive_file_id=drive_file_id,
                mp4_drive_folder_id=drive_folder_id,
            )
            artifacts_written = append_outputs_index_rows(gw, control_sheet_id, artifact_rows)
        elif renderer == "vse_stitcher":
            manifest_path = str(job.get("input_manifest_path") or job.get("input_manifest_drive_file_id") or "").strip()
            if not manifest_path:
                raise RuntimeError("renderer=vse_stitcher requires non-empty field: input_manifest_path")
            job["input_manifest_path"] = manifest_path
            _require_job_field(job, "input_audio_path", renderer=renderer)
            _require_job_field(job, "fps", renderer=renderer)
            _require_job_field(job, "aspect", renderer=renderer)
            _require_job_field(job, "stitch_preset", renderer=renderer)
            _require_job_field(job, "seed", renderer=renderer)
            output_mp4_path = _require_job_field(job, "output_local_path", renderer=renderer)

            blender_bin = os.environ.get("BLENDER_BIN", "").strip() or DEFAULT_BLENDER_BIN
            cmd = build_vse_stitcher_command(job, blender_bin=blender_bin, script_path=VSE_STITCHER_SCRIPT_PATH)
            stage_retries = _to_int(os.environ.get("BLENDER_STAGE_RETRIES", "1"), 1)
            stage_retry_delay_s = float(os.environ.get("BLENDER_STAGE_RETRY_DELAY_SECONDS", "1"))

            logs_dir = Path(tempfile.gettempdir()) / "so_render_logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"{run_id}.log"
            _run_to_log(
                cmd,
                str(log_path),
                stage="vse_stitcher",
                retries=stage_retries,
                retry_delay_seconds=stage_retry_delay_s,
            )

            output_filename = str(job.get("output_filename") or "").strip() or Path(output_mp4_path).name
            drive_file_id, drive_folder_id = _handle_output_mode(job, output_mp4_path, output_filename)
            created = _utc_now_iso()
            artifact_rows = build_outputs_rows_for_vse_stitcher(
                run_id=run_id,
                render_job_id=render_job_id,
                output_mp4_path=output_mp4_path,
                log_path=str(log_path),
                campaign_id=str(job.get("campaign_id") or ""),
                created_utc=created,
                mp4_drive_file_id=drive_file_id,
                mp4_drive_folder_id=drive_folder_id,
            )
            artifacts_written = append_outputs_index_rows(gw, control_sheet_id, artifact_rows)
        elif renderer == "pipeline_v1":
            _require_job_field(job, "stitch_preset", renderer=renderer)
            _require_job_field(job, "seed", renderer=renderer)
            _require_job_field(job, "preset", renderer=renderer)
            _require_job_field(job, "fps", renderer=renderer)
            _require_job_field(job, "aspect", renderer=renderer)
            _require_job_field(job, "bg_mode", renderer=renderer)

            resolved = resolve_and_download_pipeline_v1_inputs(job, run_id)
            paths = resolved["paths"]
            manifest = resolved["manifest"]
            stage_plan = plan_pipeline_v1_stages(manifest, resolved)
            cuts = manifest.get("cuts", []) if isinstance(manifest, dict) else []
            normalized_cuts = [c for c in cuts if isinstance(c, dict)] if isinstance(cuts, list) else []
            max_cut_end_s = (
                max(_parse_ffmpeg_timestamp_to_seconds(str(c.get("end") or "")) for c in normalized_cuts)
                if normalized_cuts
                else 0.0
            )
            render_end_padding_s = max(0.0, _to_float(os.environ.get("PIPELINE_RENDER_END_PADDING_SECONDS", "1.0"), 1.0))
            render_end_seconds = (max_cut_end_s + render_end_padding_s) if max_cut_end_s > 0.0 else 0.0

            blender_bin = os.environ.get("BLENDER_BIN", "").strip() or DEFAULT_BLENDER_BIN
            ffmpeg_bin = os.environ.get("FFMPEG_BIN", "").strip() or DEFAULT_FFMPEG_BIN
            blender_stage_retries = _to_int(os.environ.get("PIPELINE_BLENDER_STAGE_RETRIES", "1"), 1)
            blender_retry_delay_s = float(os.environ.get("PIPELINE_BLENDER_RETRY_DELAY_SECONDS", "1"))

            base_mp4 = str(Path(paths["outputs"]) / "base.mp4")
            lyrics_mp4 = str(Path(paths["outputs"]) / "lyrics.mp4")
            final_mp4 = lyrics_mp4

            stitch_job = {
                "media_root_path": _build_stitcher_media_root(
                    resolved["clip_paths"],
                    paths["clips"],
                    stitch_preset=str(job.get("stitch_preset") or ""),
                ),
                "input_audio_path": resolved["audio_path"],
                "fps": str(job.get("fps") or ""),
                "aspect": str(job.get("aspect") or ""),
                "stitch_preset": str(job.get("stitch_preset") or ""),
                "bpm": str(job.get("bpm") or manifest.get("stitch", {}).get("bpm") or ""),
                "seed": str(job.get("seed") or ""),
                "output_local_path": base_mp4,
                "render_end_seconds": render_end_seconds,
            }
            stitch_log = str(Path(paths["logs"]) / "stitch.log")
            stitch_cmd = build_vse_stitcher_command(stitch_job, blender_bin=blender_bin, script_path=VSE_STITCHER_SCRIPT_PATH)
            try:
                _run_to_log(
                    stitch_cmd,
                    stitch_log,
                    stage="stitch",
                    retries=blender_stage_retries,
                    retry_delay_seconds=blender_retry_delay_s,
                )
            except Exception as stitch_exc:
                allow_stitch_fallback = os.environ.get("PIPELINE_STITCH_FALLBACK_COPY_FIRST", "1").strip() == "1"
                if not allow_stitch_fallback:
                    raise
                if not resolved["clip_paths"]:
                    raise
                shutil.copy2(resolved["clip_paths"][0], base_mp4)
                with Path(stitch_log).open("a", encoding="utf-8") as lf:
                    lf.write("\n")
                    lf.write(f"stitch_fallback=copy_first_clip reason={stitch_exc}\n")
            if not Path(base_mp4).exists() and resolved["clip_paths"]:
                shutil.copy2(resolved["clip_paths"][0], base_mp4)
                with Path(stitch_log).open("a", encoding="utf-8") as lf:
                    lf.write("\n")
                    lf.write("stitch_fallback=copy_first_clip reason=missing_base_output\n")

            lyrics_job = {
                "input_video_path": base_mp4,
                "input_srt_path": resolved["lyrics_srt_path"],
                "input_audio_path": resolved["audio_path"],
                "fps": str(job.get("fps") or ""),
                "aspect": str(job.get("aspect") or ""),
                "preset": str(job.get("preset") or ""),
                "bg_mode": str(job.get("bg_mode") or ""),
                "output_local_path": lyrics_mp4,
                "render_end_seconds": render_end_seconds,
            }
            lyrics_log = str(Path(paths["logs"]) / "lyrics.log")
            lyrics_cmd = build_vse_text_command(lyrics_job, blender_bin=blender_bin, script_path=VSE_TEXT_SCRIPT_PATH)
            _run_to_log(
                lyrics_cmd,
                lyrics_log,
                stage="lyrics",
                retries=blender_stage_retries,
                retry_delay_seconds=blender_retry_delay_s,
            )
            if not Path(lyrics_mp4).exists():
                shutil.copy2(base_mp4, lyrics_mp4)
                with Path(lyrics_log).open("a", encoding="utf-8") as lf:
                    lf.write("\n")
                    lf.write("lyrics_fallback=copy_base reason=missing_lyrics_output\n")

            copy_log = ""
            if "copy" in stage_plan:
                final_mp4 = str(Path(paths["outputs"]) / "final.mp4")
                copy_job = {
                    "input_video_path": lyrics_mp4,
                    "input_srt_path": resolved["copy_srt_path"],
                    "input_audio_path": resolved["audio_path"],
                    "fps": str(job.get("fps") or ""),
                    "aspect": str(job.get("aspect") or ""),
                    "preset": str(job.get("preset") or ""),
                    "bg_mode": str(job.get("bg_mode") or ""),
                    "output_local_path": final_mp4,
                    "render_end_seconds": render_end_seconds,
                }
                copy_log = str(Path(paths["logs"]) / "copy.log")
                copy_cmd = build_vse_text_command(copy_job, blender_bin=blender_bin, script_path=VSE_TEXT_SCRIPT_PATH)
                _run_to_log(
                    copy_cmd,
                    copy_log,
                    stage="copy",
                    retries=blender_stage_retries,
                    retry_delay_seconds=blender_retry_delay_s,
                )

            # Some preset/script combinations can report success while not materializing lyrics output.
            # Keep pipeline moving by falling back to stitch output for cut extraction.
            if not Path(final_mp4).exists():
                final_mp4 = base_mp4
            source_label = "copy" if copy_log else "lyrics"
            source_log = copy_log or lyrics_log
            selected_source = _pick_cut_source_video(
                base_mp4=base_mp4,
                candidate_mp4=final_mp4,
                max_cut_end_s=max_cut_end_s,
                candidate_label=source_label,
                candidate_log_path=source_log,
            )
            if selected_source != final_mp4:
                with Path(source_log).open("a", encoding="utf-8") as lf:
                    lf.write("\n")
                    lf.write(
                        "video_fallback=use_base_for_cuts reason=short_overlay_output "
                        f"required_cut_end_s={max_cut_end_s:.3f}\n"
                    )
                final_mp4 = selected_source

            cut_logs: list[str] = []
            shorts: list[str] = []
            if isinstance(cuts, list):
                if normalized_cuts:
                    source_duration = _probe_media_duration_seconds(final_mp4)
                    # If source media is unexpectedly short, fail fast instead of silently producing one-frame outputs.
                    if max_cut_end_s > (source_duration + 0.05):
                        raise RuntimeError(
                            "pipeline_v1 cut range exceeds rendered media duration "
                            f"(duration_s={source_duration:.3f} max_cut_end_s={max_cut_end_s:.3f} source={final_mp4})."
                        )
                for i, cut in enumerate(cuts, start=1):
                    if not isinstance(cut, dict):
                        continue
                    start_tc = str(cut.get("start") or "").strip()
                    end_tc = str(cut.get("end") or "").strip()
                    filename = str(cut.get("filename") or f"short_{i:02d}.mp4").strip()
                    if not start_tc or not end_tc or not filename:
                        continue
                    out_short = str(Path(paths["outputs"]) / filename)
                    cut_log = str(Path(paths["logs"]) / f"cut_{i:02d}.log")
                    cut_cmd = build_ffmpeg_cut_command(
                        ffmpeg_bin=ffmpeg_bin,
                        input_path=final_mp4,
                        start=start_tc,
                        end=end_tc,
                        output_path=out_short,
                    )
                    _run_to_log(cut_cmd, cut_log, stage=f"cut:{filename}")
                    shorts.append(out_short)
                    cut_logs.append(cut_log)

            created = _utc_now_iso()
            campaign_id = str(job.get("campaign_id") or "")
            artifact_rows: list[dict[str, Any]] = [
                _build_output_row_from_file(
                    run_id=run_id,
                    render_job_id=render_job_id,
                    campaign_id=campaign_id,
                    artifact_type="pipeline_base_mp4",
                    file_path=base_mp4,
                    created_utc=created,
                ),
                _build_output_row_from_file(
                    run_id=run_id,
                    render_job_id=render_job_id,
                    campaign_id=campaign_id,
                    artifact_type="pipeline_lyrics_mp4",
                    file_path=lyrics_mp4,
                    created_utc=created,
                ),
                _build_output_row_from_file(
                    run_id=run_id,
                    render_job_id=render_job_id,
                    campaign_id=campaign_id,
                    artifact_type="pipeline_final_mp4",
                    file_path=final_mp4,
                    created_utc=created,
                ),
                _build_output_row_from_file(
                    run_id=run_id,
                    render_job_id=render_job_id,
                    campaign_id=campaign_id,
                    artifact_type="pipeline_stitch_log",
                    file_path=stitch_log,
                    created_utc=created,
                ),
                _build_output_row_from_file(
                    run_id=run_id,
                    render_job_id=render_job_id,
                    campaign_id=campaign_id,
                    artifact_type="pipeline_lyrics_log",
                    file_path=lyrics_log,
                    created_utc=created,
                ),
            ]
            if copy_log:
                artifact_rows.append(
                    _build_output_row_from_file(
                        run_id=run_id,
                        render_job_id=render_job_id,
                        campaign_id=campaign_id,
                        artifact_type="pipeline_copy_log",
                        file_path=copy_log,
                        created_utc=created,
                    )
                )
            for i, short_path in enumerate(shorts, start=1):
                artifact_rows.append(
                    _build_output_row_from_file(
                        run_id=run_id,
                        render_job_id=render_job_id,
                        campaign_id=campaign_id,
                        artifact_type=f"pipeline_short_{i:02d}",
                        file_path=short_path,
                        created_utc=created,
                    )
                )
            for i, log_path in enumerate(cut_logs, start=1):
                artifact_rows.append(
                    _build_output_row_from_file(
                        run_id=run_id,
                        render_job_id=render_job_id,
                        campaign_id=campaign_id,
                        artifact_type=f"pipeline_cut_log_{i:02d}",
                        file_path=log_path,
                        created_utc=created,
                    )
                )

            output_mode = str(job.get("output_mode") or "local").strip().lower()
            if output_mode == "drive":
                dst_parent = _require_job_field(job, "output_drive_folder_id", renderer=renderer)
                client = drive_upload_from_env()
                folder_name = build_pipeline_subfolder_name(campaign_id, run_id)
                if folder_name:
                    drive_folder_id = client.find_folder_by_name(folder_name, dst_parent) or client.create_folder(
                        folder_name, dst_parent
                    )
                else:
                    drive_folder_id = dst_parent
                existing_rows = read_outputs_index_all(gw, control_sheet_id)
                run_ids_with_shorts = _pipeline_run_ids_with_shorts(existing_rows)
                if any(str(r.get("artifact_type") or "").strip().lower().startswith("pipeline_short_") for r in artifact_rows):
                    run_ids_with_shorts.add(run_id)
                for row in artifact_rows:
                    artifact_type = str(row.get("artifact_type") or "")
                    filename = str(row.get("filename") or "")
                    if not _is_pipeline_final_video_artifact(
                        artifact_type=artifact_type,
                        run_id=run_id,
                        run_ids_with_shorts=run_ids_with_shorts,
                    ):
                        continue
                    if should_upload_artifact(
                        existing_rows,
                        run_id=run_id,
                        artifact_type=artifact_type,
                        filename=filename,
                    ):
                        local_path = str(row.get("notes") or "").replace("local_path=", "", 1)
                        drive_file_id = client.upload_file(local_path, drive_folder_id, _guess_mime_type(local_path))
                        row["drive_file_id"] = drive_file_id
                        row["drive_folder_id"] = drive_folder_id
                    else:
                        existing = _find_existing_uploaded_artifact(
                            existing_rows,
                            run_id=run_id,
                            artifact_type=artifact_type,
                            filename=filename,
                        )
                        if existing:
                            row["drive_file_id"] = str(existing.get("drive_file_id") or "")
                            row["drive_folder_id"] = str(existing.get("drive_folder_id") or drive_folder_id)

                for stale_drive_file_id in _collect_pipeline_cleanup_drive_file_ids(
                    existing_rows,
                    render_job_id=render_job_id,
                    run_ids_with_shorts=run_ids_with_shorts,
                ):
                    _delete_drive_file_best_effort(
                        client,
                        drive_file_id=stale_drive_file_id,
                        reason=f"non_final_artifact render_job_id={render_job_id}",
                    )

                delete_manifest = os.environ.get("PIPELINE_DELETE_MANIFEST_ON_SUCCESS", "1").strip().lower() not in {
                    "0",
                    "false",
                    "no",
                    "off",
                }
                manifest_drive_file_id = str(job.get("input_manifest_drive_file_id") or "").strip()
                if delete_manifest and manifest_drive_file_id and re.fullmatch(r"[A-Za-z0-9_-]{10,}", manifest_drive_file_id):
                    _delete_drive_file_best_effort(
                        client,
                        drive_file_id=manifest_drive_file_id,
                        reason=f"input_manifest render_job_id={render_job_id}",
                    )
            elif output_mode != "local":
                raise RuntimeError(f"Unsupported output_mode: {output_mode}")
            artifacts_written = append_outputs_index_rows(gw, control_sheet_id, artifact_rows)
        else:
            raise RuntimeError("Renderer not implemented; set renderer=noop for smoke validation.")

        finished = _utc_now_iso()
        mark_success(gw, control_sheet_id, render_job_id, run_id, now_utc=finished)
        if idempotency_verify:
            try:
                outputs_rows = read_outputs_index_all(gw, control_sheet_id)
                matches = count_outputs_for_run(
                    outputs_rows,
                    run_id=run_id,
                    render_job_id=render_job_id,
                    artifact_type="noop",
                )
                if matches != 1:
                    raise RuntimeError(
                        f"Idempotency verify failed: expected exactly 1 noop artifact for run_id={run_id}, found={matches}."
                    )
            except Exception as verify_err:
                if isinstance(verify_err, RuntimeError):
                    raise
                print(f"Idempotency verify skipped: {verify_err}")
        run_row["status"] = RENDER_STATUS_SUCCESS
        run_row["finished_utc"] = finished
        run_row["updated_utc"] = finished
        run_row["artifacts_written"] = artifacts_written
        upsert_render_run(gw, control_sheet_id, run_row)
        print(f"Render run success: run_id={run_id} render_job_id={render_job_id} artifacts_written={artifacts_written}")
        return 0
    except Exception as e:
        if isinstance(e, DriveUploadError):
            e = RuntimeError(str(e))
        finished = _utc_now_iso()
        mark_failed(gw, control_sheet_id, render_job_id, run_id, str(e), now_utc=finished)
        run_row["status"] = RENDER_STATUS_FAILED
        run_row["finished_utc"] = finished
        run_row["updated_utc"] = finished
        run_row["error_msg"] = str(e)
        upsert_render_run(gw, control_sheet_id, run_row)
        print(f"Render run failed: run_id={run_id} render_job_id={render_job_id} error={e}", file=sys.stderr)
        return 1


def loop_forever() -> int:
    poll_seconds = _to_int(os.environ.get("POLL_SECONDS", "10"), 10)
    while True:
        rc = run_once()
        if rc not in {0, 1}:
            return rc
        time.sleep(max(poll_seconds, 1))


def main() -> int:
    parser = argparse.ArgumentParser(description="Render agent v0 (noop renderer)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-once")
    sub.add_parser("loop")
    args = parser.parse_args()

    if args.cmd == "run-once":
        return run_once()
    return loop_forever()


if __name__ == "__main__":
    raise SystemExit(main())

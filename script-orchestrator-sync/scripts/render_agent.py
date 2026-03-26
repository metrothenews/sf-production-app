#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
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
from so_sync.gcs_storage import GcsStorageClient, GcsStorageError, parse_gs_uri  # noqa: E402
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
from render_obs.events import RenderEventLogger  # noqa: E402

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
VSE_EFFECTS_SCRIPT_PATH = os.environ.get("VSE_EFFECTS_SCRIPT_PATH", "").strip() or str(
    (WORKSPACE_ROOT / "vse-stitcher" / "scripts" / "vse_effects_pass_deterministic_bl456.py").resolve()
)
VSE_PERFORMANCE_SCRIPT_PATH = os.environ.get("VSE_PERFORMANCE_SCRIPT_PATH", "").strip() or str(
    (WORKSPACE_ROOT / "vse-stitcher" / "scripts" / "vse_performance_preset_deterministic_bl456.py").resolve()
)
DEFAULT_FFMPEG_BIN = "ffmpeg"
DEFAULT_STAGE_HEARTBEAT_SECONDS = 20.0
INSTAGRAM_DEFAULT_VIDEO_BITRATE_CAP_BPS = 8_000_000
INSTAGRAM_DEFAULT_VIDEO_TARGET_BITRATE_BPS = 6_000_000
INSTAGRAM_DEFAULT_AUDIO_BITRATE = "128k"
INSTAGRAM_DEFAULT_SOURCE_MAX_BYTES = 120 * 1024 * 1024
INSTAGRAM_DEFAULT_SOURCE_MAX_BITRATE_BPS = 20_000_000
EDIT_MODE_FOUND_FOOTAGE = "found_footage"
EDIT_MODE_PERFORMANCE = "performance"
PERFORMANCE_MODE_SIMPLE = "simple_sync_cut"
PERFORMANCE_MODE_PRESET = "preset"
PERFORMANCE_PRESET_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)([a-fA-F])([a-zA-Z]*)")
PERFORMANCE_PRESET_ALLOWED_MODIFIERS = {"r", "b", "w"}
PERFORMANCE_PRESET_TIMING_MODE_BEAT_CUTS = "beat_cuts"
PERFORMANCE_PRESET_TIMING_MODE_NORMALIZED = "normalized"

_EVENT_LOGGER: RenderEventLogger | None = None
_EVENT_RUN_ID = ""
_EVENT_RENDER_JOB_ID = ""
_EVENT_SUMMARY: dict[str, Any] = {
    "stages_started": 0,
    "stages_succeeded": 0,
    "stages_failed": 0,
    "stage_retries": 0,
    "cuts_attempted": 0,
    "cuts_succeeded": 0,
}


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


def _to_bool(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _pipeline_observability_mode() -> str:
    mode = str(os.environ.get("PIPELINE_OBSERVABILITY_MODE", "deep") or "deep").strip().lower()
    return mode if mode in {"basic", "deep"} else "deep"


def _pipeline_json_events_enabled() -> bool:
    if _pipeline_observability_mode() == "basic":
        return False
    return str(os.environ.get("PIPELINE_JSON_EVENTS_ENABLED", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _pipeline_stage_heartbeat_seconds() -> float:
    return max(0.0, _to_float(os.environ.get("PIPELINE_STAGE_HEARTBEAT_SECONDS", str(DEFAULT_STAGE_HEARTBEAT_SECONDS)), DEFAULT_STAGE_HEARTBEAT_SECONDS))


def _pipeline_stage_timeout_seconds() -> float:
    return max(0.0, _to_float(os.environ.get("PIPELINE_STAGE_TIMEOUT_SECONDS", "0"), 0.0))


def _pipeline_forensics_on_failure() -> bool:
    if _pipeline_observability_mode() == "basic":
        return False
    return str(os.environ.get("PIPELINE_FORENSICS_ON_FAILURE", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _pipeline_forensics_upload_enabled() -> bool:
    return str(os.environ.get("PIPELINE_FORENSICS_UPLOAD", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _pipeline_forensics_frame_samples() -> int:
    return max(1, _to_int(os.environ.get("PIPELINE_FORENSICS_FRAME_SAMPLES", "12"), 12))


def _classify_error_code(message: str) -> str:
    text = str(message or "").lower()
    if "overlap detected" in text or "stutter segment overlap" in text:
        return "E_STITCH_OVERLAP"
    if "effects strip window mismatch" in text:
        return "E_EFFECTS_WINDOW"
    if "base_video_too_short" in text:
        return "E_EFFECTS_BASE_SHORT"
    if "duration gate failed" in text or "frame gate failed" in text or "output duration gate failed" in text:
        return "E_OUTPUT_SHORT"
    if "timeout" in text and "stage=" in text:
        return "E_STAGE_TIMEOUT"
    if "confirm_upload_size" in text or "size mismatch" in text:
        return "E_UPLOAD_VERIFY"
    if "ssl" in text or "timed out" in text or "connection reset" in text or "transport" in text:
        return "E_UPLOAD_TRANSPORT"
    if "append_outputs_index_rows" in text or "outputs_index_append" in text:
        return "E_SHEETS_APPEND"
    return "E_UNKNOWN"


def _event_log_path_default(run_id: str) -> str:
    logs_dir = Path(tempfile.gettempdir()) / "so_render_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return str(logs_dir / f"{run_id}.events.jsonl")


def _init_event_logger(*, run_id: str, render_job_id: str, path: str = "") -> None:
    global _EVENT_LOGGER, _EVENT_RUN_ID, _EVENT_RENDER_JOB_ID, _EVENT_SUMMARY
    _EVENT_RUN_ID = str(run_id or "")
    _EVENT_RENDER_JOB_ID = str(render_job_id or "")
    _EVENT_SUMMARY = {
        "stages_started": 0,
        "stages_succeeded": 0,
        "stages_failed": 0,
        "stage_retries": 0,
        "cuts_attempted": 0,
        "cuts_succeeded": 0,
    }
    out_path = str(path or _event_log_path_default(_EVENT_RUN_ID))
    _EVENT_LOGGER = RenderEventLogger(out_path, enabled=_pipeline_json_events_enabled())


def _rebind_event_logger(path: str) -> None:
    global _EVENT_LOGGER
    if _EVENT_LOGGER is None:
        return
    _EVENT_LOGGER.rebind(path)


def _event_log_path() -> str:
    if _EVENT_LOGGER is None:
        return ""
    return str(_EVENT_LOGGER.path)


def _emit_event(
    *,
    event: str,
    stage: str = "",
    cut_idx: int = 0,
    attempt: int = 0,
    severity: str = "info",
    code: str = "",
    message: str = "",
    **extra: Any,
) -> None:
    if _EVENT_LOGGER is None:
        return
    _EVENT_LOGGER.emit(
        run_id=_EVENT_RUN_ID,
        render_job_id=_EVENT_RENDER_JOB_ID,
        cut_idx=int(cut_idx or 0),
        stage=str(stage or ""),
        attempt=int(attempt or 0),
        event=str(event or ""),
        severity=str(severity or "info"),
        code=str(code or ""),
        message=str(message or ""),
        **extra,
    )


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


def _normalize_category_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for ch in raw:
        if ch.isalpha():
            return ch.upper()
    return ""


def _normalize_collection_num(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    m = re.search(r"(\d+)", raw)
    if not m:
        return ""
    return str(int(m.group(1)))


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
            gcs_uri = str(raw.get("gcs_uri") or "").strip()
            if not drive_id and not gcs_uri:
                continue
            if not drive_id and gcs_uri:
                drive_id = f"gcs_{hashlib.sha1(gcs_uri.encode('utf-8')).hexdigest()[:16]}"
            label = str(raw.get("label") or f"clip_{i:03d}").strip()
            clip_row = {"drive_file_id": drive_id, "label": label}
            category = _normalize_category_key(raw.get("category") or raw.get("cat") or "")
            if category:
                clip_row["category"] = category
            collection_num = _normalize_collection_num(raw.get("collection_num"))
            if collection_num:
                clip_row["collection_num"] = collection_num
            if gcs_uri:
                clip_row["gcs_uri"] = gcs_uri
            gcs_object = str(raw.get("gcs_object") or "").strip()
            if gcs_object:
                clip_row["gcs_object"] = gcs_object
            clips.append(clip_row)
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


def resolve_song_title_overlay(manifest: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    text_cfg = manifest.get("text", {}) if isinstance(manifest, dict) else {}
    if not isinstance(text_cfg, dict):
        text_cfg = {}
    song_cfg = text_cfg.get("song_title", {})
    if not isinstance(song_cfg, dict):
        song_cfg = {}
    enabled = _to_bool(song_cfg.get("enabled")) or _to_bool(job.get("display_song_name"))
    text = str(song_cfg.get("text") or job.get("song_name") or "").strip()
    if not enabled or not text:
        return {"enabled": False, "text": ""}
    return {"enabled": True, "text": text}


def resolve_pipeline_edit_mode(manifest: dict[str, Any], job: dict[str, Any]) -> str:
    job_mode = str(job.get("edit_mode") or "").strip().lower()
    if job_mode:
        if job_mode in {EDIT_MODE_FOUND_FOOTAGE, EDIT_MODE_PERFORMANCE}:
            return job_mode
        raise RuntimeError(
            f"Unsupported edit_mode={job_mode!r}; expected {EDIT_MODE_FOUND_FOOTAGE} or {EDIT_MODE_PERFORMANCE}."
        )
    manifest_mode = str(manifest.get("edit_mode") or "").strip().lower() if isinstance(manifest, dict) else ""
    if manifest_mode:
        if manifest_mode in {EDIT_MODE_FOUND_FOOTAGE, EDIT_MODE_PERFORMANCE}:
            return manifest_mode
        raise RuntimeError(
            f"Unsupported manifest edit_mode={manifest_mode!r}; expected {EDIT_MODE_FOUND_FOOTAGE} or {EDIT_MODE_PERFORMANCE}."
        )
    return EDIT_MODE_FOUND_FOOTAGE


def resolve_pipeline_performance_mode(manifest: dict[str, Any], job: dict[str, Any]) -> str:
    perf_cfg = manifest.get("performance", {}) if isinstance(manifest, dict) else {}
    if not isinstance(perf_cfg, dict):
        perf_cfg = {}
    raw = (
        str(job.get("performance_mode") or "").strip().lower()
        or str(perf_cfg.get("mode") or "").strip().lower()
    )
    if raw in {PERFORMANCE_MODE_SIMPLE, PERFORMANCE_MODE_PRESET}:
        return raw
    raise RuntimeError(
        f"Invalid performance mode {raw!r}; expected {PERFORMANCE_MODE_SIMPLE} or {PERFORMANCE_MODE_PRESET}."
    )


def parse_performance_preset_tokens(value: str) -> list[dict[str, Any]]:
    raw = str(value or "").strip()
    if not raw:
        raise RuntimeError("Performance preset is empty.")
    out: list[dict[str, Any]] = []
    idx = 0
    while idx < len(raw):
        m = PERFORMANCE_PRESET_TOKEN_RE.match(raw, idx)
        if not m:
            raise RuntimeError(f"Invalid performance preset token at offset {idx}: {raw!r}")
        duration_text, clip_label, modifiers_raw = m.groups()
        duration = float(duration_text)
        if duration <= 0:
            raise RuntimeError(f"Performance preset duration must be > 0: {raw!r}")
        clip = clip_label.lower()
        modifiers = str(modifiers_raw or "").lower()
        for ch in modifiers:
            if ch not in PERFORMANCE_PRESET_ALLOWED_MODIFIERS:
                raise RuntimeError(
                    f"Unsupported performance preset modifier {ch!r} in {raw!r}; "
                    "allowed modifiers are: r, b, w."
                )
        out.append({"duration": duration, "clip": clip, "modifiers": modifiers})
        idx = m.end()
    if not out:
        raise RuntimeError(f"Performance preset has no valid tokens: {raw!r}")
    return out


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
    s = str(stitch_preset or "").strip()
    i = 0
    while i < len(s):
        if not s[i].isdigit():
            i += 1
            continue
        j = i
        while j < len(s) and s[j].isdigit():
            j += 1
        k = j
        while k < len(s) and s[k].isalpha():
            k += 1
        if k == j:
            i = j + 1
            continue
        suffix = s[j:k]
        cat = suffix[-1].upper()
        if cat and cat.isalpha() and cat not in seen:
            seen.add(cat)
            out.append(cat)
        i = k
    return out


def _build_stitcher_media_root(clip_sources: list[Any], media_root_dir: str, *, stitch_preset: str = "") -> str:
    if not clip_sources:
        raise RuntimeError("pipeline_v1 requires at least one clip for stitch stage")
    root = Path(media_root_dir)
    root.mkdir(parents=True, exist_ok=True)
    normalized: list[dict[str, str]] = []
    for item in clip_sources:
        if isinstance(item, dict):
            path = str(item.get("path") or item.get("clip_path") or "").strip()
            category = _normalize_category_key(item.get("category") or "")
        else:
            path = str(item or "").strip()
            category = ""
        if not path:
            continue
        normalized.append({"path": path, "category": category})
    if not normalized:
        raise RuntimeError("pipeline_v1 stitch stage has no valid clip paths")

    by_category: dict[str, list[str]] = {}
    uncategorized: list[str] = []
    for row in normalized:
        cat = str(row.get("category") or "").strip()
        if cat:
            by_category.setdefault(cat, []).append(str(row["path"]))
        else:
            uncategorized.append(str(row["path"]))

    buckets = _extract_stitch_categories(stitch_preset)
    if not buckets:
        buckets = sorted(by_category.keys()) or ["A", "B", "C"]

    for bucket in buckets:
        pool = list(by_category.get(bucket) or [])
        if not pool and uncategorized:
            # Backward compatibility for manifests that do not carry category tags.
            pool = list(uncategorized)
        if not pool:
            raise RuntimeError(f"No clips found for stitch preset category '{bucket}'")
        bucket_dir = root / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        for i, clip in enumerate(pool, start=1):
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


def _aspect_to_resolution(aspect: str) -> tuple[int, int]:
    key = str(aspect or "").strip().lower()
    if key in {"9:16", "9x16", "vertical"}:
        return (1080, 1920)
    if key in {"16:9", "16x9", "horizontal"}:
        return (1920, 1080)
    return (1080, 1920)


def _instagram_video_bitrate_cap_bps() -> int:
    return max(1_000_000, _to_int(os.environ.get("INSTAGRAM_VIDEO_BITRATE_CAP_BPS", str(INSTAGRAM_DEFAULT_VIDEO_BITRATE_CAP_BPS)), INSTAGRAM_DEFAULT_VIDEO_BITRATE_CAP_BPS))


def _instagram_video_target_bitrate_bps() -> int:
    cap = _instagram_video_bitrate_cap_bps()
    target = _to_int(os.environ.get("INSTAGRAM_VIDEO_TARGET_BITRATE_BPS", str(INSTAGRAM_DEFAULT_VIDEO_TARGET_BITRATE_BPS)), INSTAGRAM_DEFAULT_VIDEO_TARGET_BITRATE_BPS)
    return max(500_000, min(cap, int(target)))


def _instagram_source_max_bytes() -> int:
    return max(10 * 1024 * 1024, _to_int(os.environ.get("INSTAGRAM_SOURCE_MAX_BYTES", str(INSTAGRAM_DEFAULT_SOURCE_MAX_BYTES)), INSTAGRAM_DEFAULT_SOURCE_MAX_BYTES))


def _instagram_source_max_bitrate_bps() -> int:
    return max(5_000_000, _to_int(os.environ.get("INSTAGRAM_SOURCE_MAX_BITRATE_BPS", str(INSTAGRAM_DEFAULT_SOURCE_MAX_BITRATE_BPS)), INSTAGRAM_DEFAULT_SOURCE_MAX_BITRATE_BPS))


def _ffmpeg_bitrate_string(bps: int) -> str:
    kbps = max(64, int(round(float(max(1, int(bps))) / 1000.0)))
    return f"{kbps}k"


def build_ffmpeg_instagram_normalize_command(
    *,
    ffmpeg_bin: str,
    input_path: str,
    output_path: str,
    fps: int,
    aspect: str,
    include_audio: bool,
) -> list[str]:
    width, height = _aspect_to_resolution(aspect)
    target_bps = _instagram_video_target_bitrate_bps()
    cap_bps = _instagram_video_bitrate_cap_bps()
    buf_bps = max(cap_bps * 2, target_bps * 2)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={int(max(1, int(fps)))},"
        "format=yuv420p"
    )
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        input_path,
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        _ffmpeg_bitrate_string(target_bps),
        "-maxrate",
        _ffmpeg_bitrate_string(cap_bps),
        "-bufsize",
        _ffmpeg_bitrate_string(buf_bps),
        "-movflags",
        "+faststart",
    ]
    if include_audio:
        cmd.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                str(os.environ.get("INSTAGRAM_AUDIO_BITRATE", INSTAGRAM_DEFAULT_AUDIO_BITRATE) or INSTAGRAM_DEFAULT_AUDIO_BITRATE),
                "-ar",
                "48000",
                "-ac",
                "2",
                "-shortest",
            ]
        )
    else:
        cmd.extend(["-an"])
    cmd.append(output_path)
    return cmd


def build_ffmpeg_master_audio_cut_command(
    *,
    ffmpeg_bin: str,
    video_input_path: str,
    audio_input_path: str,
    start: str,
    end: str,
    output_path: str,
    fps: int,
    aspect: str,
) -> list[str]:
    start_s = _parse_ffmpeg_timestamp_to_seconds(start)
    end_s = _parse_ffmpeg_timestamp_to_seconds(end)
    width, height = _aspect_to_resolution(aspect)
    target_bps = _instagram_video_target_bitrate_bps()
    cap_bps = _instagram_video_bitrate_cap_bps()
    buf_bps = max(cap_bps * 2, target_bps * 2)
    return [
        ffmpeg_bin,
        "-y",
        "-i",
        video_input_path,
        "-i",
        audio_input_path,
        "-filter_complex",
        (
            f"[0:v]trim=start={start_s:.3f}:end={end_s:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={int(max(1, int(fps)))},"
            "format=yuv420p[v];"
            f"[1:a]atrim=start={start_s:.3f}:end={end_s:.3f},asetpts=PTS-STARTPTS[a]"
        ),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        _ffmpeg_bitrate_string(target_bps),
        "-maxrate",
        _ffmpeg_bitrate_string(cap_bps),
        "-bufsize",
        _ffmpeg_bitrate_string(buf_bps),
        "-c:a",
        "aac",
        "-b:a",
        str(os.environ.get("INSTAGRAM_AUDIO_BITRATE", INSTAGRAM_DEFAULT_AUDIO_BITRATE) or INSTAGRAM_DEFAULT_AUDIO_BITRATE),
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-shortest",
        output_path,
    ]


def build_ffmpeg_audio_trim_command(
    *,
    ffmpeg_bin: str,
    input_path: str,
    start: str,
    end: str,
    output_path: str,
) -> list[str]:
    start_s = _parse_ffmpeg_timestamp_to_seconds(start)
    end_s = _parse_ffmpeg_timestamp_to_seconds(end)
    return [
        ffmpeg_bin,
        "-y",
        "-i",
        input_path,
        "-af",
        f"atrim=start={start_s:.3f}:end={end_s:.3f},asetpts=PTS-STARTPTS",
        "-vn",
        "-acodec",
        "pcm_s16le",
        output_path,
    ]


def _format_seconds_to_ffmpeg_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    hh = total_ms // 3_600_000
    mm = (total_ms % 3_600_000) // 60_000
    ss = (total_ms % 60_000) // 1000
    ms = total_ms % 1000
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"


def _parse_srt_timecode_to_ms(tc: str) -> int:
    m = re.match(r"^\s*(\d+):(\d+):(\d+)[,.](\d+)\s*$", str(tc or ""))
    if not m:
        raise ValueError(f"Bad SRT timecode: {tc!r}")
    hh, mm, ss, ms = [int(x) for x in m.groups()]
    return ((hh * 3600 + mm * 60 + ss) * 1000) + ms


def _extract_srt_timecode_token(raw: str) -> str:
    m = re.search(r"(\d+:\d+:\d+[,.]\d+)", str(raw or ""))
    if not m:
        raise ValueError(f"Bad SRT time token: {raw!r}")
    return str(m.group(1))


def _format_ms_to_srt_timecode(ms: int) -> str:
    value = max(0, int(ms))
    hh = value // 3_600_000
    mm = (value % 3_600_000) // 60_000
    ss = (value % 60_000) // 1000
    rem = value % 1000
    return f"{hh:02d}:{mm:02d}:{ss:02d},{rem:03d}"


def _slice_and_shift_srt_file(
    *,
    input_path: str,
    output_path: str,
    start_s: float,
    end_s: float,
) -> bool:
    src = Path(input_path)
    raw = src.read_text(encoding="utf-8", errors="ignore")
    lines = raw.splitlines()
    start_ms = max(0, int(round(float(start_s) * 1000.0)))
    end_ms = max(start_ms, int(round(float(end_s) * 1000.0)))
    duration_ms = max(0, end_ms - start_ms)
    boundary_epsilon_ms = 1

    parsed_blocks: list[tuple[int, int, list[str]]] = []
    i = 0
    while i < len(lines):
        while i < len(lines) and not str(lines[i]).strip():
            i += 1
        if i >= len(lines):
            break
        # optional numeric index
        if re.match(r"^\s*\d+\s*$", str(lines[i])):
            i += 1
        if i >= len(lines):
            break
        if "-->" not in str(lines[i]):
            i += 1
            continue
        left, right = [x.strip() for x in str(lines[i]).split("-->", 1)]
        i += 1
        try:
            cue_start = _parse_srt_timecode_to_ms(_extract_srt_timecode_token(left))
            cue_end = _parse_srt_timecode_to_ms(_extract_srt_timecode_token(right))
        except Exception:
            while i < len(lines) and str(lines[i]).strip():
                i += 1
            continue
        text_lines: list[str] = []
        while i < len(lines) and str(lines[i]).strip():
            text_lines.append(str(lines[i]))
            i += 1
        parsed_blocks.append((cue_start, cue_end, text_lines))

    if not parsed_blocks:
        Path(output_path).write_text("", encoding="utf-8")
        return False

    first_start_ms = min(s for s, _, _ in parsed_blocks)
    hour_shift_ms = 0
    # Handle Resolve-style SRT clocks (01:00:00,000+) before cut overlap math.
    if first_start_ms >= 1_800_000 and first_start_ms > (end_ms + 60_000):
        hour_shift_ms = -((first_start_ms // 3_600_000) * 3_600_000)

    out_blocks: list[str] = []
    out_idx = 1
    for cue_start, cue_end, text_lines in parsed_blocks:
        shifted_start = cue_start + hour_shift_ms
        shifted_end = cue_end + hour_shift_ms
        if shifted_end <= shifted_start or shifted_end <= 0:
            continue
        if shifted_start < 0:
            shifted_start = 0
        overlap_start = max(shifted_start, start_ms - boundary_epsilon_ms)
        overlap_end = min(shifted_end, end_ms + boundary_epsilon_ms)
        if overlap_end <= overlap_start:
            continue
        rel_start = max(0, min(duration_ms, overlap_start - start_ms))
        rel_end = max(0, min(duration_ms, overlap_end - start_ms))
        if rel_end <= rel_start:
            # Keep exact boundary-touching cues as 1ms clips so per-cut lyric overlays
            # remain visible when cue edges align to cut edges.
            if duration_ms > 0 and abs(shifted_end - start_ms) <= boundary_epsilon_ms:
                rel_start = 0
                rel_end = min(duration_ms, max(1, boundary_epsilon_ms))
            elif duration_ms > 0 and abs(shifted_start - end_ms) <= boundary_epsilon_ms:
                rel_end = duration_ms
                rel_start = max(0, duration_ms - max(1, boundary_epsilon_ms))
            else:
                continue
        block = [
            str(out_idx),
            f"{_format_ms_to_srt_timecode(rel_start)} --> {_format_ms_to_srt_timecode(rel_end)}",
            *text_lines,
            "",
        ]
        out_blocks.append("\n".join(block))
        out_idx += 1

    Path(output_path).write_text("\n".join(out_blocks), encoding="utf-8")
    return bool(out_blocks)


def _allow_empty_lyrics_srt() -> bool:
    return os.environ.get("ALLOW_EMPTY_LYRICS_SRT", "0").strip().lower() in {"1", "true", "yes", "on"}


def _pipeline_per_cut_render_enabled() -> bool:
    return os.environ.get("PIPELINE_PER_CUT_RENDER", "1").strip().lower() not in {"0", "false", "no", "off"}


def _pipeline_effects_pass2_enabled() -> bool:
    return os.environ.get("PIPELINE_EFFECTS_PASS2_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _pipeline_effects_pass2_strict() -> bool:
    return os.environ.get("PIPELINE_EFFECTS_PASS2_STRICT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _pipeline_effects_keep_plan_on_success() -> bool:
    return os.environ.get("PIPELINE_EFFECTS_PASS2_KEEP_PLAN_ON_SUCCESS", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _count_srt_cues(path: str) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8", errors="ignore")
    count = 0
    for line in text.splitlines():
        if "-->" not in line:
            continue
        parts = str(line).split("-->", 1)
        if len(parts) != 2:
            continue
        try:
            _extract_srt_timecode_token(parts[0])
            _extract_srt_timecode_token(parts[1])
        except Exception:
            continue
        count += 1
    return count


def _require_nonempty_srt(path: str, *, label: str) -> int:
    cue_count = _count_srt_cues(path)
    if cue_count > 0 or _allow_empty_lyrics_srt():
        return cue_count
    raise RuntimeError(
        f"{label} has zero subtitle cues at {path}. "
        "Set ALLOW_EMPTY_LYRICS_SRT=1 to bypass this guardrail."
    )


def _assert_file_exists(path: str, *, label: str) -> None:
    p = Path(str(path or "").strip())
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"{label} missing file: {p}")


def _assert_dir_exists(path: str, *, label: str) -> None:
    p = Path(str(path or "").strip())
    if not p.exists() or not p.is_dir():
        raise RuntimeError(f"{label} missing directory: {p}")


def _should_fallback_to_legacy_full_render(*, main_lyrics_cues: int, cuts_with_lyrics: int) -> bool:
    return (int(main_lyrics_cues) > 0) and (int(cuts_with_lyrics) <= 0) and (not _allow_empty_lyrics_srt())


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


def _probe_media_video_frame_count(path: str) -> int:
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


def _probe_media_summary(path: str) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,width,height,nb_frames:format=duration",
        "-of",
        "json",
        path,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        return {
            "path": path,
            "duration_s": 0.0,
            "video_frames": 0,
            "resolution": "",
            "audio_stream_present": False,
            "probe_error": str(proc.stderr or "").strip(),
        }
    try:
        payload = json.loads(str(proc.stdout or "{}"))
    except Exception:
        payload = {}
    streams = list(payload.get("streams") or [])
    fmt = dict(payload.get("format") or {})
    duration_s = _to_float(fmt.get("duration"), 0.0)
    video_frames = 0
    resolution = ""
    audio_present = False
    for stream in streams:
        if str(stream.get("codec_type") or "") == "audio":
            audio_present = True
        if str(stream.get("codec_type") or "") == "video":
            width = _to_int(stream.get("width"), 0)
            height = _to_int(stream.get("height"), 0)
            if width > 0 and height > 0:
                resolution = f"{width}x{height}"
            video_frames = max(video_frames, _to_int(stream.get("nb_frames"), 0))
    return {
        "path": path,
        "duration_s": duration_s,
        "video_frames": video_frames,
        "resolution": resolution,
        "audio_stream_present": audio_present,
    }


def _probe_media_bitrate_bps(path: str) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=bit_rate",
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
        return int(float(raw[-1].strip()))
    except Exception:
        return 0


def _normalize_source_clip_if_needed(
    *,
    ffmpeg_bin: str,
    source_path: str,
    output_path: str,
    fps: int,
    aspect: str,
) -> str:
    src = Path(source_path)
    if not src.exists():
        raise RuntimeError(f"Source clip missing before normalization: {source_path}")
    src_bytes = int(src.stat().st_size)
    src_bitrate = _probe_media_bitrate_bps(str(src))
    over_size = src_bytes > _instagram_source_max_bytes()
    over_bitrate = src_bitrate > _instagram_source_max_bitrate_bps()
    normalize_all = str(os.environ.get("PERFORMANCE_NORMALIZE_SOURCE_CLIPS", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if not normalize_all and not (over_size or over_bitrate):
        return str(src)
    reason = "forced_normalize" if normalize_all else "oversized_or_high_bitrate"
    print(
        "performance_source_normalize_start "
        f"reason={reason} source={source_path} bytes={src_bytes} bitrate_bps={src_bitrate}"
    )
    cmd = build_ffmpeg_instagram_normalize_command(
        ffmpeg_bin=ffmpeg_bin,
        input_path=str(src),
        output_path=output_path,
        fps=fps,
        aspect=aspect,
        include_audio=False,
    )
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "performance source normalization failed "
            f"source={source_path} output={output_path} "
            f"returncode={proc.returncode} stderr={str(proc.stderr or '').strip()}"
        )
    out = Path(output_path)
    if not out.exists() or out.stat().st_size <= 0:
        raise RuntimeError(
            "performance source normalization produced no output "
            f"source={source_path} output={output_path}"
        )
    out_bitrate = _probe_media_bitrate_bps(str(out))
    print(
        "performance_source_normalize_done "
        f"source={source_path} output={output_path} bytes={int(out.stat().st_size)} bitrate_bps={out_bitrate}"
    )
    _delete_local_file_best_effort(str(src))
    return str(out)


def _emit_media_summary_event(*, path: str, stage: str, cut_idx: int) -> None:
    summary = _probe_media_summary(path)
    _emit_event(
        event="media_summary",
        stage=stage,
        cut_idx=cut_idx,
        message="ffprobe summary",
        **summary,
    )


def _materialize_video_fallback_output(
    *,
    output_path: str,
    source_path: str,
    log_path: str = "",
    note: str = "",
) -> bool:
    out = Path(output_path)
    needs_fallback = True
    if out.exists():
        try:
            needs_fallback = out.stat().st_size <= 0
        except Exception:
            needs_fallback = True
    if not needs_fallback:
        return False
    shutil.copy2(source_path, output_path)
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as lf:
            lf.write("\n")
            lf.write(f"{note or 'stage_fallback'}\n")
    return True


def _assert_expected_cut_output(
    *,
    output_path: str,
    expected_duration_s: float,
    expected_frames: int,
    duration_tolerance_s: float = 0.20,
    frame_tolerance: int = 2,
    stage: str = "output_gate",
    cut_idx: int = 0,
) -> None:
    actual_duration_s = _probe_media_duration_seconds(output_path)
    actual_frames = _probe_media_video_frame_count(output_path)
    _emit_event(
        event="output_probe",
        stage=stage,
        cut_idx=cut_idx,
        message="output probe summary",
        file=output_path,
        expected_s=float(expected_duration_s),
        actual_s=float(actual_duration_s),
        expected_frames=int(expected_frames),
        actual_frames=int(actual_frames),
        tolerance_s=float(duration_tolerance_s),
        tolerance_frames=int(frame_tolerance),
    )
    if actual_duration_s + float(duration_tolerance_s) < float(expected_duration_s):
        _emit_event(
            event="output_gate_fail",
            stage=stage,
            cut_idx=cut_idx,
            severity="error",
            code="E_OUTPUT_SHORT",
            message="duration gate failed",
            file=output_path,
            expected_s=float(expected_duration_s),
            actual_s=float(actual_duration_s),
            tolerance_s=float(duration_tolerance_s),
        )
        raise RuntimeError(
            "pipeline_v1 effects output duration gate failed "
            f"expected_s={expected_duration_s:.3f} actual_s={actual_duration_s:.3f} "
            f"tolerance_s={float(duration_tolerance_s):.3f} output={output_path}"
        )
    if actual_frames > 0 and actual_frames + int(frame_tolerance) < int(expected_frames):
        _emit_event(
            event="output_gate_fail",
            stage=stage,
            cut_idx=cut_idx,
            severity="error",
            code="E_OUTPUT_SHORT",
            message="frame gate failed",
            file=output_path,
            expected_frames=int(expected_frames),
            actual_frames=int(actual_frames),
            tolerance_frames=int(frame_tolerance),
        )
        raise RuntimeError(
            "pipeline_v1 effects output frame gate failed "
            f"expected_frames={int(expected_frames)} actual_frames={int(actual_frames)} "
            f"tolerance_frames={int(frame_tolerance)} output={output_path}"
        )
    _emit_event(
        event="output_gate_pass",
        stage=stage,
        cut_idx=cut_idx,
        message="output gate passed",
        file=output_path,
        expected_s=float(expected_duration_s),
        actual_s=float(actual_duration_s),
        expected_frames=int(expected_frames),
        actual_frames=int(actual_frames),
    )


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


def _extract_reveal_auto_speed_baseline_from_log(log_path: str) -> float:
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0.0
    pattern = re.compile(r"reveal_auto_speed_baseline .*?baseline_wps=([0-9]*\.?[0-9]+)")
    for line in reversed(text.splitlines()):
        m = pattern.search(line)
        if not m:
            continue
        return _to_float(m.group(1), 0.0)
    return 0.0


def _materialize_expected_video_output(
    *,
    expected_path: str,
    log_path: str = "",
    stage: str = "",
) -> str:
    expected = Path(expected_path)
    if expected.exists():
        return str(expected)

    candidates: list[Path] = []
    parent = expected.parent
    stem = expected.stem
    suffix = expected.suffix.lower()
    video_exts = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}

    if parent.exists():
        for p in sorted(parent.glob(f"{stem}*")):
            if not p.is_file():
                continue
            if p == expected:
                continue
            ext = p.suffix.lower()
            if ext == suffix or ext in video_exts:
                candidates.append(p)

    if log_path:
        try:
            log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            log_text = ""
        for line in reversed(log_text.splitlines()):
            m = re.search(r"(?:Saved|saved):\s*['\"]?([^'\"]+)", line)
            if not m:
                continue
            raw = str(m.group(1) or "").strip()
            if not raw:
                continue
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = (parent / p).resolve()
            if p.exists() and p.is_file() and p != expected:
                candidates.append(p)
                break

    if not candidates:
        return str(expected)

    # Prefer the largest candidate to avoid selecting sidecar or stale partial files.
    candidates = sorted(candidates, key=lambda p: (p.stat().st_size, p.stat().st_mtime), reverse=True)
    chosen = candidates[0]
    try:
        expected.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(chosen, expected)
        print(
            "render_output_reconciled "
            f"stage={stage or 'unknown'} expected={expected} source={chosen}"
        )
        return str(expected)
    except Exception as exc:
        print(
            "render_output_reconcile_failed "
            f"stage={stage or 'unknown'} expected={expected} source={chosen} error={exc}"
        )
        return str(chosen)


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


def _is_pipeline_drive_upload_artifact(
    *,
    artifact_type: str,
    run_id: str,
    run_ids_with_shorts: set[str],
) -> bool:
    normalized = str(artifact_type or "").strip().lower()
    if _is_pipeline_final_video_artifact(
        artifact_type=normalized,
        run_id=run_id,
        run_ids_with_shorts=run_ids_with_shorts,
    ):
        return True
    return normalized in {"pipeline_events_jsonl", "pipeline_diag_bundle", "pipeline_diag_summary_json"}


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
    auto_baseline_wps = _to_float(job.get("auto_baseline_wps", ""), 0.0)
    if auto_baseline_wps > 0.0:
        cmd.extend(["--auto-baseline-wps", f"{auto_baseline_wps:.6f}"])
    if _to_bool(job.get("display_song_name")):
        song_name = str(job.get("song_name") or "").strip()
        if song_name:
            cmd.extend(["--song-title", song_name])
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
    effects_pass1_mode = str(job.get("effects_pass1_mode") or "").strip()
    if effects_pass1_mode:
        cmd.extend(["--effects-pass1-mode", effects_pass1_mode])
    effects_plan_out = str(job.get("effects_plan_out") or "").strip()
    if effects_plan_out:
        cmd.extend(["--effects-plan-out", effects_plan_out])
    run_id = str(job.get("run_id") or "").strip()
    if run_id:
        cmd.extend(["--run-id", run_id])
    if str(job.get("cut_idx") or "").strip():
        cmd.extend(["--cut-idx", str(job.get("cut_idx")).strip()])
    return cmd


def build_vse_performance_command(job: dict, *, blender_bin: str, script_path: str) -> list[str]:
    timeline_offset_seconds = _to_float(job.get("timeline_offset_seconds"), 0.0)
    sync_mode = str(job.get("sync_mode") or "timeline_locked").strip().lower() or "timeline_locked"
    short_clip_policy = str(job.get("short_clip_policy") or "wrap_loop").strip().lower() or "wrap_loop"
    timing_mode = _normalize_performance_preset_timing_mode(
        str(job.get("timing_mode") or _performance_preset_timing_mode())
    )
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
        "--audio",
        str(job["input_audio_path"]).strip(),
        "--fps",
        str(job["fps"]).strip(),
        "--aspect",
        str(job["aspect"]).strip(),
        "--preset",
        str(job["performance_preset"]).strip(),
        "--bpm",
        bpm,
        "--seed",
        str(job.get("seed") or "").strip() or "1",
        "--timing-mode",
        timing_mode,
        "--timeline-offset-seconds",
        f"{timeline_offset_seconds:.6f}",
        "--sync-mode",
        sync_mode,
        "--short-clip-policy",
        short_clip_policy,
        "--output",
        str(job["output_local_path"]).strip(),
    ]
    clip_map = job.get("clip_map") if isinstance(job.get("clip_map"), dict) else {}
    for label in ("a", "b", "c", "d", "e", "f"):
        path = str(clip_map.get(label) or "").strip()
        if path:
            cmd.extend([f"--clip-{label}", path])
    render_end_seconds = _to_float(job.get("render_end_seconds", ""), 0.0)
    if render_end_seconds > 0.0:
        cmd.extend(["--render-end-seconds", f"{render_end_seconds:.3f}"])
    return cmd


def _performance_preset_sync_mode() -> str:
    mode = str(os.environ.get("PERFORMANCE_PRESET_SYNC_MODE", "timeline_locked") or "").strip().lower()
    if mode not in {"timeline_locked", "legacy_random"}:
        raise RuntimeError(
            "Invalid PERFORMANCE_PRESET_SYNC_MODE; expected one of: timeline_locked, legacy_random."
        )
    return mode


def _normalize_performance_preset_timing_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {PERFORMANCE_PRESET_TIMING_MODE_BEAT_CUTS, PERFORMANCE_PRESET_TIMING_MODE_NORMALIZED}:
        raise RuntimeError(
            "Invalid performance preset timing mode; expected one of: beat_cuts, normalized "
            f"(got {value!r})."
        )
    return mode


def _performance_preset_timing_mode() -> str:
    raw = str(
        os.environ.get("PERFORMANCE_PRESET_TIMING_MODE", PERFORMANCE_PRESET_TIMING_MODE_BEAT_CUTS)
        or PERFORMANCE_PRESET_TIMING_MODE_BEAT_CUTS
    ).strip().lower()
    return _normalize_performance_preset_timing_mode(raw)


def _performance_preset_short_clip_policy() -> str:
    policy = str(os.environ.get("PERFORMANCE_PRESET_SHORT_CLIP_POLICY", "wrap_loop") or "").strip().lower()
    if policy not in {"wrap_loop", "clamp", "fail"}:
        raise RuntimeError(
            "Invalid PERFORMANCE_PRESET_SHORT_CLIP_POLICY; expected one of: wrap_loop, clamp, fail."
        )
    return policy


def build_vse_effects_command(job: dict, *, blender_bin: str, script_path: str) -> list[str]:
    cmd = [
        blender_bin,
        "-b",
        "--python",
        script_path,
        "--",
        "--input-video",
        str(job["input_video_path"]).strip(),
        "--audio",
        str(job["input_audio_path"]).strip(),
        "--effects-plan",
        str(job["effects_plan_path"]).strip(),
        "--fps",
        str(job["fps"]).strip(),
        "--aspect",
        str(job["aspect"]).strip(),
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


def _pipeline_upload_verify_mode() -> str:
    return str(os.environ.get("PIPELINE_UPLOAD_VERIFY_MODE", "size") or "size").strip().lower()


def _pipeline_cleanup_on_success_enabled() -> bool:
    return str(os.environ.get("PIPELINE_LOCAL_CLEANUP_ON_SUCCESS", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _pipeline_cleanup_on_failure_mode() -> str:
    return str(os.environ.get("PIPELINE_LOCAL_CLEANUP_ON_FAILURE", "logs_only") or "logs_only").strip().lower()


def _local_path_from_artifact_row(row: dict[str, Any]) -> str:
    return str(row.get("notes") or "").replace("local_path=", "", 1).strip()


def _artifact_is_mutable_for_upload_verify(artifact_type: str) -> bool:
    return str(artifact_type or "").strip().lower() == "pipeline_events_jsonl"


def _refresh_artifact_row_file_metrics(row: dict[str, Any], *, local_path: str) -> int:
    num_bytes, digest = _file_bytes_sha256(local_path)
    row["bytes"] = int(num_bytes)
    row["sha256"] = str(digest)
    return int(num_bytes)


def _expected_upload_verify_bytes(row: dict[str, Any], *, local_path: str, artifact_type: str) -> int:
    expected = _to_int(row.get("bytes"), 0)
    if _artifact_is_mutable_for_upload_verify(artifact_type):
        try:
            expected = _refresh_artifact_row_file_metrics(row, local_path=local_path)
        except Exception:
            expected = _to_int(row.get("bytes"), 0)
    return int(expected)


def _delete_local_file_best_effort(path: str) -> None:
    raw = str(path or "").strip()
    if not raw:
        return
    p = Path(raw)
    try:
        if p.is_file() or p.is_symlink():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    except Exception as exc:
        print(f"local_cleanup_skip path={raw} error={exc}")


def _prune_run_tree_keep_logs(root_dir: str, logs_dir: str, *, preserve_dirs: list[str] | None = None) -> None:
    root = Path(str(root_dir or "").strip())
    logs = Path(str(logs_dir or "").strip())
    if not root.exists():
        return
    logs_resolved = logs.resolve() if logs.exists() else None
    preserved = set()
    for raw in list(preserve_dirs or []):
        p = Path(str(raw or "").strip())
        if p.exists():
            preserved.add(p.resolve())
    for child in root.iterdir():
        if logs_resolved and child.resolve() == logs_resolved:
            continue
        if child.resolve() in preserved:
            continue
        _delete_local_file_best_effort(str(child))


def _cleanup_pipeline_local_artifacts(paths: dict[str, str], *, success: bool) -> None:
    root = str(paths.get("root") or "").strip()
    logs = str(paths.get("logs") or "").strip()
    diagnostics = str(paths.get("diagnostics") or "").strip()
    if not root:
        return
    if success:
        if not _pipeline_cleanup_on_success_enabled():
            return
        _delete_local_file_best_effort(root)
        return

    mode = _pipeline_cleanup_on_failure_mode()
    if mode in {"none", "off", "false", "0"}:
        return
    if mode == "logs_only":
        preserve = [diagnostics] if diagnostics else []
        _prune_run_tree_keep_logs(root, logs, preserve_dirs=preserve)
        return
    _delete_local_file_best_effort(root)


def _upload_confirm_record_and_delete(
    *,
    gw: SheetsGateway,
    control_sheet_id: str,
    client: Any,
    artifact_row: dict[str, Any],
    drive_folder_id: str,
    verify_mode: str,
) -> dict[str, Any]:
    row = dict(artifact_row)
    local_path = _local_path_from_artifact_row(row)
    if not local_path:
        raise RuntimeError(f"artifact row missing local path: artifact_type={row.get('artifact_type')}")
    mime_type = _guess_mime_type(local_path)
    artifact_type = str(row.get("artifact_type") or "")
    _emit_event(
        event="upload_start",
        stage="upload",
        message="artifact upload start",
        artifact_type=artifact_type,
        file=local_path,
        mime_type=mime_type,
    )
    try:
        drive_file_id = client.upload_file(local_path, drive_folder_id, mime_type)
    except Exception as exc:
        _emit_event(
            event="upload_fail",
            stage="upload",
            severity="error",
            code=_classify_error_code(str(exc)),
            message="artifact upload failed",
            artifact_type=artifact_type,
            file=local_path,
            exception=str(exc),
        )
        raise

    if verify_mode != "size":
        raise RuntimeError(f"Unsupported PIPELINE_UPLOAD_VERIFY_MODE: {verify_mode}")
    expected_bytes = _expected_upload_verify_bytes(
        row,
        local_path=local_path,
        artifact_type=artifact_type,
    )
    try:
        client.confirm_upload_size(drive_file_id, expected_bytes)
    except Exception as exc:
        _emit_event(
            event="upload_fail",
            stage="upload",
            severity="error",
            code="E_UPLOAD_VERIFY",
            message="artifact upload verification failed",
            artifact_type=artifact_type,
            file=local_path,
            drive_file_id=drive_file_id,
            expected_bytes=int(expected_bytes),
            exception=str(exc),
        )
        raise

    row["drive_file_id"] = drive_file_id
    row["drive_folder_id"] = drive_folder_id
    try:
        append_outputs_index_rows(gw, control_sheet_id, [row])
    except Exception:
        _delete_drive_file_best_effort(
            client,
            drive_file_id=drive_file_id,
            reason=(
                f"rollback_failed_outputs_index_append run_id={row.get('run_id')} "
                f"artifact_type={row.get('artifact_type')}"
            ),
        )
        _emit_event(
            event="upload_fail",
            stage="upload",
            severity="error",
            code="E_SHEETS_APPEND",
            message="outputs index append failed after upload",
            artifact_type=artifact_type,
            file=local_path,
            drive_file_id=drive_file_id,
        )
        raise

    if not _artifact_is_mutable_for_upload_verify(artifact_type):
        _delete_local_file_best_effort(local_path)
    _emit_event(
        event="upload_end",
        stage="upload",
        message="artifact upload complete",
        artifact_type=artifact_type,
        file=local_path,
        drive_file_id=drive_file_id,
        bytes=int(expected_bytes),
    )
    return row


def _run_to_log(
    cmd: list[str],
    log_path: str,
    *,
    stage: str,
    cut_idx: int = 0,
    retries: int = 0,
    retry_delay_seconds: float = 1.0,
    output_probe_path: str = "",
) -> None:
    attempts = max(1, int(retries) + 1)
    delay = max(0.0, float(retry_delay_seconds))
    heartbeat_s = _pipeline_stage_heartbeat_seconds()
    timeout_s = _pipeline_stage_timeout_seconds()
    log_file = Path(log_path)
    for attempt in range(1, attempts + 1):
        mode = "wb" if attempt == 1 else "ab"
        _EVENT_SUMMARY["stages_started"] = int(_EVENT_SUMMARY.get("stages_started", 0)) + 1
        cmd_text = shlex.join([str(x) for x in cmd])
        _emit_event(
            event="stage_start",
            stage=stage,
            cut_idx=cut_idx,
            attempt=attempt,
            message="stage subprocess start",
            cmd=cmd_text,
            log_path=str(log_file),
            timeout_s=float(timeout_s),
            heartbeat_s=float(heartbeat_s),
        )
        with log_file.open(mode) as log_f:
            if attempt > 1:
                log_f.write(f"\n--- retry attempt {attempt}/{attempts} stage={stage} ---\n".encode("utf-8"))
            proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
            started = time.monotonic()
            next_heartbeat = started + max(1.0, heartbeat_s) if heartbeat_s > 0 else 0.0
            timed_out = False
            while True:
                rc = proc.poll()
                now = time.monotonic()
                elapsed = now - started
                if rc is not None:
                    break
                if timeout_s > 0 and elapsed >= timeout_s:
                    timed_out = True
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc.wait(timeout=5)
                    _EVENT_SUMMARY["stages_failed"] = int(_EVENT_SUMMARY.get("stages_failed", 0)) + 1
                    _emit_event(
                        event="stage_fail",
                        stage=stage,
                        cut_idx=cut_idx,
                        attempt=attempt,
                        severity="error",
                        code="E_STAGE_TIMEOUT",
                        message="stage timeout exceeded",
                        duration_ms=int(round(elapsed * 1000.0)),
                        timeout_s=float(timeout_s),
                        cmd=cmd_text,
                        log_path=str(log_file),
                    )
                    raise RuntimeError(
                        f"stage={stage} timed out after {timeout_s:.1f}s attempt={attempt}/{attempts} log_path={log_path}"
                    )
                if heartbeat_s > 0 and now >= next_heartbeat:
                    probe_bytes = 0
                    if output_probe_path:
                        try:
                            probe_bytes = int(Path(output_probe_path).stat().st_size)
                        except Exception:
                            probe_bytes = 0
                    _emit_event(
                        event="stage_heartbeat",
                        stage=stage,
                        cut_idx=cut_idx,
                        attempt=attempt,
                        message="stage still running",
                        duration_ms=int(round(elapsed * 1000.0)),
                        output_probe_path=output_probe_path or "",
                        output_probe_bytes=probe_bytes,
                        log_bytes=int(log_file.stat().st_size) if log_file.exists() else 0,
                    )
                    next_heartbeat = now + max(1.0, heartbeat_s)
                time.sleep(0.25)
            if timed_out:
                continue
            return_code = int(proc.returncode or 0)
        elapsed_ms = int(round((time.monotonic() - started) * 1000.0))
        if return_code == 0:
            _EVENT_SUMMARY["stages_succeeded"] = int(_EVENT_SUMMARY.get("stages_succeeded", 0)) + 1
            _emit_event(
                event="stage_end",
                stage=stage,
                cut_idx=cut_idx,
                attempt=attempt,
                message="stage subprocess completed",
                duration_ms=elapsed_ms,
                log_path=str(log_file),
                output_probe_path=output_probe_path or "",
            )
            return
        _EVENT_SUMMARY["stages_failed"] = int(_EVENT_SUMMARY.get("stages_failed", 0)) + 1
        if attempt < attempts:
            _EVENT_SUMMARY["stage_retries"] = int(_EVENT_SUMMARY.get("stage_retries", 0)) + 1
            _emit_event(
                event="stage_retry",
                stage=stage,
                cut_idx=cut_idx,
                attempt=attempt,
                severity="warning",
                code=_classify_error_code(f"stage={stage} exit_code={return_code}"),
                message="stage failed, retry scheduled",
                exit_code=return_code,
                duration_ms=elapsed_ms,
                retry_in_s=float(delay),
                log_path=str(log_file),
            )
            time.sleep(delay)
            continue
        tail = _log_tail_excerpt(log_path).replace("\n", " | ")
        code = _classify_error_code(tail)
        _emit_event(
            event="stage_fail",
            stage=stage,
            cut_idx=cut_idx,
            attempt=attempt,
            severity="error",
            code=code,
            message="stage failed after retries",
            exit_code=return_code,
            duration_ms=elapsed_ms,
            log_path=str(log_file),
            log_tail=tail,
        )
        raise RuntimeError(
            f"stage={stage} failed exit_code={return_code} attempts={attempts} log_path={log_path} log_tail={tail}"
        )


def _build_forensics_bundle(
    *,
    run_id: str,
    render_job_id: str,
    run_root: str,
    error_message: str,
) -> tuple[str, str]:
    script = REPO_ROOT / "scripts" / "render_forensics_bundle.py"
    if not script.exists():
        return "", ""
    out_dir = Path(run_root) / "diagnostics" / run_id
    cmd = [
        sys.executable,
        str(script),
        "--run-dir",
        str(run_root),
        "--run-id",
        str(run_id),
        "--render-job-id",
        str(render_job_id),
        "--error",
        str(error_message or ""),
        "--output-dir",
        str(out_dir),
        "--frame-samples",
        str(_pipeline_forensics_frame_samples()),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        _emit_event(
            event="forensics_fail",
            stage="forensics",
            severity="warning",
            code="E_UNKNOWN",
            message="forensics bundle command failed",
            command=shlex.join(cmd),
            stderr=str(proc.stderr or "").strip(),
        )
        return "", ""
    summary_path = str(out_dir / "summary.json")
    bundle_path = str(out_dir / "bundle.zip")
    _emit_event(
        event="forensics_done",
        stage="forensics",
        message="forensics bundle generated",
        summary_path=summary_path,
        bundle_path=bundle_path,
    )
    return summary_path if Path(summary_path).exists() else "", bundle_path if Path(bundle_path).exists() else ""


def _upload_failure_diagnostics(
    *,
    gw: SheetsGateway,
    control_sheet_id: str,
    client: Any | None,
    drive_folder_id: str,
    run_id: str,
    render_job_id: str,
    campaign_id: str,
    verify_mode: str,
    summary_path: str,
    bundle_path: str,
) -> int:
    if client is None or not drive_folder_id:
        return 0
    created = _utc_now_iso()
    rows: list[dict[str, Any]] = []
    if summary_path and Path(summary_path).exists():
        rows.append(
            _build_output_row_from_file(
                run_id=run_id,
                render_job_id=render_job_id,
                campaign_id=campaign_id,
                artifact_type="pipeline_diag_summary_json",
                file_path=summary_path,
                created_utc=created,
            )
        )
    if bundle_path and Path(bundle_path).exists():
        rows.append(
            _build_output_row_from_file(
                run_id=run_id,
                render_job_id=render_job_id,
                campaign_id=campaign_id,
                artifact_type="pipeline_diag_bundle",
                file_path=bundle_path,
                created_utc=created,
            )
        )
    written = 0
    for row in rows:
        out = _upload_confirm_record_and_delete(
            gw=gw,
            control_sheet_id=control_sheet_id,
            client=client,
            artifact_row=row,
            drive_folder_id=drive_folder_id,
            verify_mode=verify_mode,
        )
        if out:
            written += 1
    return written


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
                print(f"clip_download_source=drive file_id={drive_file_id} via=drive_api reason={reason}")
                return uploader.download_file(drive_file_id, dst_path)
            except DriveUploadError as up_exc:
                raise RuntimeError(str(up_exc)) from up_exc
    try:
        print(f"clip_download_source=drive file_id={drive_file_id} via=proxy")
        return proxy.download_file(drive_file_id, dst_path)
    except DriveProxyError as exc:
        reason = _proxy_error_reason(exc)
        print(f"drive_download_fallback=drive_api file_id={drive_file_id} reason={reason}")
        uploader = upload_client or drive_upload_from_env()
        try:
            print(f"clip_download_source=drive file_id={drive_file_id} via=drive_api reason={reason}")
            return uploader.download_file(drive_file_id, dst_path)
        except DriveUploadError as up_exc:
            raise RuntimeError(str(up_exc)) from up_exc


_GCS_CLIENT_CACHE: dict[str, GcsStorageClient] = {}


def _download_gcs_uri(gcs_uri: str, dst_path: str) -> str:
    bucket, obj = parse_gs_uri(gcs_uri)
    client = _GCS_CLIENT_CACHE.get(bucket)
    if client is None:
        try:
            client = GcsStorageClient.from_adc(bucket)
        except GcsStorageError as exc:
            raise RuntimeError(str(exc)) from exc
        _GCS_CLIENT_CACHE[bucket] = client
    try:
        print(f"clip_download_source=gcs bucket={bucket} object={obj}")
        return client.download_to_path(obj, dst_path)
    except GcsStorageError as exc:
        raise RuntimeError(str(exc)) from exc


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
    clip_specs_out: list[dict[str, str]] = []
    clip_specs = list(resolved_ids.get("clips") or [])
    for clip in clip_specs:
        drive_id = str(clip.get("drive_file_id") or "").strip()
        gcs_uri = str(clip.get("gcs_uri") or "").strip()
        label = str(clip.get("label") or drive_id).strip()
        category = _normalize_category_key(clip.get("category") or "")
        collection_num = _normalize_collection_num(clip.get("collection_num"))
        if not drive_id and not gcs_uri:
            continue
        ext = ".mp4"
        if gcs_uri:
            try:
                _, obj = parse_gs_uri(gcs_uri)
                ext = Path(obj).suffix or ".mp4"
            except Exception:
                ext = ".mp4"
        clip_path = str(Path(paths["clips"]) / build_clip_filename(label, drive_id or "clip", ext=ext))
        if gcs_uri:
            _download_gcs_uri(gcs_uri, clip_path)
        else:
            _download_with_fallback(drive_id, clip_path)
        clip_paths.append(clip_path)
        clip_spec_row = {"path": clip_path, "drive_file_id": drive_id, "label": label}
        if category:
            clip_spec_row["category"] = category
        if collection_num:
            clip_spec_row["collection_num"] = collection_num
        if gcs_uri:
            clip_spec_row["gcs_uri"] = gcs_uri
        gcs_object = str(clip.get("gcs_object") or "").strip()
        if gcs_object:
            clip_spec_row["gcs_object"] = gcs_object
        clip_specs_out.append(clip_spec_row)

    return {
        "paths": paths,
        "manifest": manifest,
        "manifest_path": manifest_local_path,
        "audio_path": audio_path,
        "lyrics_srt_path": lyrics_srt_path,
        "copy_srt_path": copy_srt_path,
        "clip_paths": clip_paths,
        "clip_specs": clip_specs_out,
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
    _init_event_logger(run_id=run_id, render_job_id=render_job_id)
    dedupe_key = f"render:{render_job_id}:{run_id}"
    run_row = _new_render_run(run_id, render_job_id, RENDER_STATUS_QUEUED, now, dedupe_key=dedupe_key)
    upsert_render_run(gw, control_sheet_id, run_row)
    pipeline_paths_for_cleanup: dict[str, str] | None = None
    pipeline_run_succeeded = False
    pipeline_diag_client: Any | None = None
    pipeline_diag_drive_folder_id = ""
    pipeline_diag_verify_mode = _pipeline_upload_verify_mode()
    pipeline_diag_campaign_id = str(job.get("campaign_id") or "")
    _emit_event(event="run_start", stage="run", message="render run started", renderer=str(job.get("renderer") or "noop"))

    try:
        started = _utc_now_iso()
        mark_running(gw, control_sheet_id, render_job_id, run_id, now_utc=started)
        run_row["status"] = RENDER_STATUS_RUNNING
        run_row["started_utc"] = started
        run_row["updated_utc"] = started
        upsert_render_run(gw, control_sheet_id, run_row)

        renderer = str(job.get("renderer") or "noop").strip().lower()
        _emit_event(event="renderer_selected", stage="run", message="renderer selected", renderer=renderer)
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
            srt_path = _require_job_field(job, "input_srt_path", renderer=renderer)
            _require_job_field(job, "preset", renderer=renderer)
            _require_job_field(job, "fps", renderer=renderer)
            _require_job_field(job, "aspect", renderer=renderer)
            _require_job_field(job, "bg_mode", renderer=renderer)
            output_mp4_path = _require_job_field(job, "output_local_path", renderer=renderer)
            cue_count = _require_nonempty_srt(srt_path, label="renderer=vse_text input_srt_path")
            print(f"lyrics_srt_cues={cue_count} label=vse_text path={srt_path}")

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
            resolved = resolve_and_download_pipeline_v1_inputs(job, run_id)
            paths = resolved["paths"]
            events_jsonl = str(Path(paths["logs"]) / "events.jsonl")
            _rebind_event_logger(events_jsonl)
            paths["events"] = events_jsonl
            paths["diagnostics"] = str(Path(paths["root"]) / "diagnostics")
            pipeline_paths_for_cleanup = dict(paths)
            manifest = resolved["manifest"]
            edit_mode = resolve_pipeline_edit_mode(manifest, job)
            song_title_overlay = resolve_song_title_overlay(manifest, job)
            stage_plan = plan_pipeline_v1_stages(manifest, resolved)
            main_lyrics_cues = _require_nonempty_srt(
                str(resolved["lyrics_srt_path"]),
                label="renderer=pipeline_v1 lyrics_srt_path",
            )
            print(f"lyrics_srt_cues={main_lyrics_cues} label=pipeline_v1_main path={resolved['lyrics_srt_path']}")
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
            output_mode = str(job.get("output_mode") or "local").strip().lower()
            if output_mode not in {"local", "drive"}:
                raise RuntimeError(f"Unsupported output_mode: {output_mode}")
            is_drive_output = output_mode == "drive"
            verify_mode = _pipeline_upload_verify_mode()
            artifacts_written = 0
            drive_folder_id = ""
            client: Any | None = None
            existing_rows: list[dict[str, Any]] = []
            run_ids_with_shorts: set[str] = set()
            uploaded_short_rows: list[dict[str, Any]] = []

            if is_drive_output:
                dst_parent = _require_job_field(job, "output_drive_folder_id", renderer=renderer)
                client = drive_upload_from_env()
                campaign_id = str(job.get("campaign_id") or "")
                folder_name = build_pipeline_subfolder_name(campaign_id, run_id)
                if folder_name:
                    drive_folder_id = client.find_folder_by_name(folder_name, dst_parent) or client.create_folder(
                        folder_name, dst_parent
                    )
                else:
                    drive_folder_id = dst_parent
                existing_rows = read_outputs_index_all(gw, control_sheet_id)
                run_ids_with_shorts = _pipeline_run_ids_with_shorts(existing_rows)
                run_ids_with_shorts.add(run_id)
                pipeline_diag_client = client
                pipeline_diag_drive_folder_id = drive_folder_id
                pipeline_diag_campaign_id = campaign_id
                pipeline_diag_verify_mode = verify_mode

            if edit_mode == EDIT_MODE_PERFORMANCE:
                _require_job_field(job, "preset", renderer=renderer)
                _require_job_field(job, "fps", renderer=renderer)
                _require_job_field(job, "aspect", renderer=renderer)
                _require_job_field(job, "bg_mode", renderer=renderer)
                performance_fps = max(1, _to_int(job.get("fps"), 30))
                performance_aspect = str(job.get("aspect") or "9:16").strip() or "9:16"
                if performance_aspect not in {"9:16", "9x16", "vertical"}:
                    print(f"performance_aspect_override from={performance_aspect} to=9:16")
                    performance_aspect = "9:16"
                performance_cfg = manifest.get("performance", {}) if isinstance(manifest, dict) else {}
                if not isinstance(performance_cfg, dict):
                    performance_cfg = {}
                performance_mode = resolve_pipeline_performance_mode(manifest, job)
                audio_policy = str(performance_cfg.get("audio_policy") or "master_only").strip().lower()
                if audio_policy not in {"master_only", ""}:
                    raise RuntimeError(f"Unsupported performance audio_policy={audio_policy!r}; expected master_only.")

                performance_clip_paths: dict[str, str] = {}
                if performance_mode == PERFORMANCE_MODE_SIMPLE:
                    master_clip_id = str(performance_cfg.get("master_clip_drive_file_id") or "").strip()
                    if not master_clip_id:
                        raise RuntimeError("performance simple_sync_cut mode requires master_clip_drive_file_id.")
                    master_raw_path = str(Path(paths["clips"]) / "performance_master_raw.mp4")
                    master_path = str(Path(paths["clips"]) / "performance_master.mp4")
                    _download_with_fallback(master_clip_id, master_raw_path)
                    performance_clip_paths["a"] = _normalize_source_clip_if_needed(
                        ffmpeg_bin=ffmpeg_bin,
                        source_path=master_raw_path,
                        output_path=master_path,
                        fps=performance_fps,
                        aspect=performance_aspect,
                    )
                else:
                    clip_ids = performance_cfg.get("clips", {}) if isinstance(performance_cfg.get("clips"), dict) else {}
                    for label in ("a", "b", "c", "d", "e", "f"):
                        clip_id = str(clip_ids.get(label) or "").strip()
                        if not clip_id:
                            continue
                        clip_raw_path = str(Path(paths["clips"]) / f"performance_{label}_raw.mp4")
                        clip_path = str(Path(paths["clips"]) / f"performance_{label}.mp4")
                        _download_with_fallback(clip_id, clip_raw_path)
                        performance_clip_paths[label] = _normalize_source_clip_if_needed(
                            ffmpeg_bin=ffmpeg_bin,
                            source_path=clip_raw_path,
                            output_path=clip_path,
                            fps=performance_fps,
                            aspect=performance_aspect,
                        )
                    if not (2 <= len(performance_clip_paths) <= 6):
                        raise RuntimeError("performance preset mode requires between 2 and 6 labeled clips (a-f).")

                cut_logs: list[str] = []
                local_shorts: list[str] = []
                artifacts_written = 0
                if not normalized_cuts:
                    raise RuntimeError("pipeline_v1 performance mode requires at least one valid manifest cut.")
                performance_sync_mode = _performance_preset_sync_mode()
                performance_timing_mode = _normalize_performance_preset_timing_mode(
                    str(performance_cfg.get("timing_mode") or _performance_preset_timing_mode())
                )
                performance_bpm = str(
                    performance_cfg.get("bpm")
                    or manifest.get("stitch", {}).get("bpm")
                    or job.get("bpm")
                    or ""
                ).strip()
                if not performance_bpm:
                    performance_bpm = os.environ.get("PIPELINE_STITCH_BPM", "120").strip() or "120"
                if _to_float(performance_bpm, 0.0) <= 0.0:
                    raise RuntimeError(
                        "performance preset mode requires a positive bpm "
                        f"(resolved value={performance_bpm!r})."
                    )
                performance_short_clip_policy = _performance_preset_short_clip_policy()

                for i, cut in enumerate(normalized_cuts, start=1):
                    start = str(cut.get("start") or "").strip()
                    end = str(cut.get("end") or "").strip()
                    filename = str(cut.get("filename") or f"short_{i:03d}.mp4").strip()
                    if not start or not end:
                        raise RuntimeError(f"performance cut missing start/end at index={i}")
                    start_s = _parse_ffmpeg_timestamp_to_seconds(start)
                    end_s = _parse_ffmpeg_timestamp_to_seconds(end)
                    if end_s <= start_s:
                        raise RuntimeError(f"performance cut end <= start for filename={filename}: {start} -> {end}")
                    cut_duration = max(0.01, end_s - start_s)
                    cut_expected_frames = max(1, int(round(cut_duration * float(job.get("fps") or 30))))
                    cut_prefix = f"cut_{i:02d}"
                    cut_audio_path = str(Path(paths["outputs"]) / f"{cut_prefix}_audio.wav")
                    cut_base_mp4 = str(Path(paths["outputs"]) / f"{cut_prefix}_base.mp4")
                    cut_lyrics_mp4 = str(Path(paths["outputs"]) / f"{cut_prefix}_lyrics.mp4")
                    cut_final_mp4 = str(Path(paths["outputs"]) / f"{cut_prefix}_final.mp4")
                    out_short = str(Path(paths["outputs"]) / filename)
                    cut_audio_log = str(Path(paths["logs"]) / f"{cut_prefix}_audio.log")
                    cut_base_log = str(Path(paths["logs"]) / f"{cut_prefix}_performance.log")
                    cut_lyrics_log = str(Path(paths["logs"]) / f"{cut_prefix}_lyrics.log")
                    cut_copy_log = str(Path(paths["logs"]) / f"{cut_prefix}_copy.log")
                    cut_normalize_log = str(Path(paths["logs"]) / f"{cut_prefix}_normalize.log")
                    cut_lyrics_srt = str(Path(paths["inputs"]) / f"{cut_prefix}_lyrics.srt")
                    cut_copy_srt = str(Path(paths["inputs"]) / f"{cut_prefix}_copy.srt")
                    cut_normalized_mp4 = str(Path(paths["outputs"]) / f"{cut_prefix}_normalized.mp4")

                    _run_to_log(
                        build_ffmpeg_audio_trim_command(
                            ffmpeg_bin=ffmpeg_bin,
                            input_path=resolved["audio_path"],
                            start=start,
                            end=end,
                            output_path=cut_audio_path,
                        ),
                        cut_audio_log,
                        stage=f"perf_audio:{filename}",
                        cut_idx=i,
                    )

                    if performance_mode == PERFORMANCE_MODE_SIMPLE:
                        master_path = str(performance_clip_paths.get("a") or "").strip()
                        if not master_path:
                            raise RuntimeError("Missing downloaded master clip for simple performance mode.")
                        _run_to_log(
                            build_ffmpeg_master_audio_cut_command(
                                ffmpeg_bin=ffmpeg_bin,
                                video_input_path=master_path,
                                audio_input_path=resolved["audio_path"],
                                start=start,
                                end=end,
                                output_path=cut_base_mp4,
                                fps=int(_to_int(cut.get("fps"), performance_fps)),
                                aspect=str(cut.get("aspect") or performance_aspect),
                            ),
                            cut_base_log,
                            stage=f"performance_simple:{filename}",
                            cut_idx=i,
                            output_probe_path=cut_base_mp4,
                        )
                    else:
                        cut_preset = (
                            str(cut.get("performance_preset") or "").strip()
                            or str(performance_cfg.get("preset") or "").strip()
                            or str(job.get("performance_preset") or "").strip()
                        )
                        parsed_tokens = parse_performance_preset_tokens(cut_preset)
                        required_labels = {str(t.get("clip") or "").strip().lower() for t in parsed_tokens}
                        missing = sorted([x for x in required_labels if x and x not in performance_clip_paths])
                        if missing:
                            raise RuntimeError(
                                f"performance preset references missing clip labels={missing}; "
                                f"available={sorted(list(performance_clip_paths.keys()))}"
                            )
                        cut_job = {
                            "input_audio_path": cut_audio_path,
                            "fps": str(cut.get("fps") or job.get("fps") or ""),
                            "aspect": str(cut.get("aspect") or job.get("aspect") or ""),
                            "performance_preset": cut_preset,
                            "bpm": performance_bpm,
                            "seed": str(cut.get("seed") or job.get("seed") or "1"),
                            "timing_mode": performance_timing_mode,
                            "timeline_offset_seconds": f"{start_s:.6f}",
                            "sync_mode": performance_sync_mode,
                            "short_clip_policy": performance_short_clip_policy,
                            "output_local_path": cut_base_mp4,
                            "render_end_seconds": cut_duration,
                            "clip_map": performance_clip_paths,
                        }
                        _run_to_log(
                            build_vse_performance_command(
                                cut_job,
                                blender_bin=blender_bin,
                                script_path=VSE_PERFORMANCE_SCRIPT_PATH,
                            ),
                            cut_base_log,
                            stage=f"performance_preset:{filename}",
                            cut_idx=i,
                            retries=blender_stage_retries,
                            retry_delay_seconds=blender_retry_delay_s,
                            output_probe_path=cut_base_mp4,
                        )
                    _emit_media_summary_event(path=cut_base_mp4, stage=f"performance_base:{filename}", cut_idx=i)

                    _slice_and_shift_srt_file(
                        input_path=resolved["lyrics_srt_path"],
                        output_path=cut_lyrics_srt,
                        start_s=start_s,
                        end_s=end_s,
                    )
                    cut_text_preset = str(cut.get("text_preset") or job.get("preset") or "").strip()
                    cut_bg_mode = str(cut.get("bg_mode") or job.get("bg_mode") or "").strip()
                    lyrics_cues = _count_srt_cues(cut_lyrics_srt)
                    if lyrics_cues <= 0 and main_lyrics_cues > 0 and not _allow_empty_lyrics_srt():
                        raise RuntimeError(
                            "performance cut has zero lyric cues after slicing "
                            f"(filename={filename} start={start} end={end} main_lyrics_cues={main_lyrics_cues})."
                        )
                    selected_source = cut_base_mp4
                    selected_log = cut_lyrics_log
                    if lyrics_cues > 0:
                        lyrics_job = {
                            "input_video_path": cut_base_mp4,
                            "input_srt_path": cut_lyrics_srt,
                            "input_audio_path": cut_audio_path,
                            "fps": str(cut.get("fps") or job.get("fps") or ""),
                            "aspect": str(cut.get("aspect") or job.get("aspect") or ""),
                            "preset": cut_text_preset,
                            "bg_mode": cut_bg_mode,
                            "output_local_path": cut_lyrics_mp4,
                            "render_end_seconds": cut_duration,
                            "display_song_name": song_title_overlay["enabled"],
                            "song_name": song_title_overlay["text"],
                        }
                        _run_to_log(
                            build_vse_text_command(lyrics_job, blender_bin=blender_bin, script_path=VSE_TEXT_SCRIPT_PATH),
                            cut_lyrics_log,
                            stage=f"performance_lyrics:{filename}",
                            cut_idx=i,
                            retries=blender_stage_retries,
                            retry_delay_seconds=blender_retry_delay_s,
                            output_probe_path=cut_lyrics_mp4,
                        )
                        selected_source = cut_lyrics_mp4
                    else:
                        shutil.copy2(cut_base_mp4, cut_lyrics_mp4)
                        Path(cut_lyrics_log).write_text("lyrics_fallback=copy_base reason=empty_lyrics_srt\n", encoding="utf-8")
                        selected_source = cut_lyrics_mp4
                    _emit_media_summary_event(path=selected_source, stage=f"performance_lyrics:{filename}", cut_idx=i)

                    has_copy = "copy" in stage_plan and str(resolved.get("copy_srt_path") or "").strip()
                    if has_copy:
                        _slice_and_shift_srt_file(
                            input_path=str(resolved.get("copy_srt_path") or ""),
                            output_path=cut_copy_srt,
                            start_s=start_s,
                            end_s=end_s,
                        )
                        copy_cues = _count_srt_cues(cut_copy_srt)
                        if copy_cues > 0:
                            copy_job = {
                                "input_video_path": selected_source,
                                "input_srt_path": cut_copy_srt,
                                "input_audio_path": cut_audio_path,
                                "fps": str(cut.get("fps") or job.get("fps") or ""),
                                "aspect": str(cut.get("aspect") or job.get("aspect") or ""),
                                "preset": cut_text_preset,
                                "bg_mode": cut_bg_mode,
                                "output_local_path": cut_final_mp4,
                                "render_end_seconds": cut_duration,
                            }
                            _run_to_log(
                                build_vse_text_command(copy_job, blender_bin=blender_bin, script_path=VSE_TEXT_SCRIPT_PATH),
                                cut_copy_log,
                                stage=f"performance_copy:{filename}",
                                cut_idx=i,
                                retries=blender_stage_retries,
                                retry_delay_seconds=blender_retry_delay_s,
                                output_probe_path=cut_final_mp4,
                            )
                            selected_source = cut_final_mp4
                            selected_log = cut_copy_log

                    source_label = "copy" if (selected_source == cut_final_mp4 and has_copy) else "lyrics"
                    selected_for_duration = _pick_cut_source_video(
                        base_mp4=cut_base_mp4,
                        candidate_mp4=selected_source,
                        max_cut_end_s=cut_duration,
                        candidate_label=source_label,
                        candidate_log_path=selected_log,
                    )
                    if selected_for_duration != selected_source:
                        with Path(selected_log).open("a", encoding="utf-8") as lf:
                            lf.write("\n")
                            lf.write(
                                "video_fallback=use_base_for_short_overlay_output "
                                f"required_cut_end_s={cut_duration:.3f}\n"
                            )
                        selected_source = selected_for_duration

                    if selected_source == cut_base_mp4:
                        # Master-cut outputs can keep source cadence (for example 24fps) while
                        # job/display fps is 30. When we intentionally fall back to base, gate
                        # against actual base frames to avoid false short-frame failures.
                        cut_expected_frames = max(1, _probe_media_video_frame_count(cut_base_mp4))

                    _run_to_log(
                        build_ffmpeg_instagram_normalize_command(
                            ffmpeg_bin=ffmpeg_bin,
                            input_path=selected_source,
                            output_path=cut_normalized_mp4,
                            fps=int(_to_int(cut.get("fps"), performance_fps)),
                            aspect=str(cut.get("aspect") or performance_aspect),
                            include_audio=True,
                        ),
                        cut_normalize_log,
                        stage=f"performance_normalize:{filename}",
                        cut_idx=i,
                        output_probe_path=cut_normalized_mp4,
                    )
                    selected_source = cut_normalized_mp4
                    selected_log = cut_normalize_log
                    if selected_source != out_short:
                        shutil.copy2(selected_source, out_short)
                    _assert_expected_cut_output(
                        output_path=out_short,
                        expected_duration_s=cut_duration,
                        expected_frames=cut_expected_frames,
                        stage=f"performance_short:{filename}",
                        cut_idx=i,
                    )
                    _emit_media_summary_event(path=out_short, stage=f"performance_short:{filename}", cut_idx=i)
                    cut_logs.append(selected_log)
                    _EVENT_SUMMARY["cuts_attempted"] = int(_EVENT_SUMMARY.get("cuts_attempted", 0)) + 1
                    _EVENT_SUMMARY["cuts_succeeded"] = int(_EVENT_SUMMARY.get("cuts_succeeded", 0)) + 1

                    if is_drive_output:
                        if client is None:
                            raise RuntimeError("Drive client is required for output_mode=drive")
                        short_row = _build_output_row_from_file(
                            run_id=run_id,
                            render_job_id=render_job_id,
                            campaign_id=str(job.get("campaign_id") or ""),
                            artifact_type=f"pipeline_short_{i:02d}",
                            file_path=out_short,
                            created_utc=_utc_now_iso(),
                        )
                        short_artifact_type = str(short_row.get("artifact_type") or "")
                        short_filename = str(short_row.get("filename") or "")
                        if should_upload_artifact(
                            existing_rows,
                            run_id=run_id,
                            artifact_type=short_artifact_type,
                            filename=short_filename,
                        ):
                            short_row = _upload_confirm_record_and_delete(
                                gw=gw,
                                control_sheet_id=control_sheet_id,
                                client=client,
                                artifact_row=short_row,
                                drive_folder_id=drive_folder_id,
                                verify_mode=verify_mode,
                            )
                        else:
                            existing = _find_existing_uploaded_artifact(
                                existing_rows,
                                run_id=run_id,
                                artifact_type=short_artifact_type,
                                filename=short_filename,
                            )
                            if existing:
                                short_row["drive_file_id"] = str(existing.get("drive_file_id") or "")
                                short_row["drive_folder_id"] = str(existing.get("drive_folder_id") or drive_folder_id)
                            append_outputs_index_rows(gw, control_sheet_id, [short_row])
                            _delete_local_file_best_effort(out_short)
                        artifacts_written += 1
                        existing_rows.append(short_row)
                    else:
                        local_shorts.append(out_short)

                    for transient in (
                        cut_audio_path,
                        cut_lyrics_srt,
                        cut_copy_srt,
                        cut_base_mp4,
                        cut_lyrics_mp4,
                        cut_final_mp4,
                        cut_normalized_mp4,
                    ):
                        if transient and transient != out_short:
                            _delete_local_file_best_effort(transient)

                created = _utc_now_iso()
                artifact_rows: list[dict[str, Any]] = []
                if not is_drive_output:
                    for i, short_path in enumerate(local_shorts, start=1):
                        artifact_rows.append(
                            _build_output_row_from_file(
                                run_id=run_id,
                                render_job_id=render_job_id,
                                campaign_id=str(job.get("campaign_id") or ""),
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
                                campaign_id=str(job.get("campaign_id") or ""),
                                artifact_type=f"pipeline_cut_log_{i:02d}",
                                file_path=log_path,
                                created_utc=created,
                            )
                        )
                events_path = _event_log_path()
                if events_path and Path(events_path).exists():
                    events_row = _build_output_row_from_file(
                        run_id=run_id,
                        render_job_id=render_job_id,
                        campaign_id=str(job.get("campaign_id") or ""),
                        artifact_type="pipeline_events_jsonl",
                        file_path=events_path,
                        created_utc=created,
                    )
                    if is_drive_output and client is not None:
                        _upload_confirm_record_and_delete(
                            gw=gw,
                            control_sheet_id=control_sheet_id,
                            client=client,
                            artifact_row=events_row,
                            drive_folder_id=drive_folder_id,
                            verify_mode=verify_mode,
                        )
                        artifacts_written += 1
                    else:
                        artifact_rows.append(events_row)
                if artifact_rows:
                    artifacts_written += append_outputs_index_rows(gw, control_sheet_id, artifact_rows)
                pipeline_run_succeeded = True

                finished = _utc_now_iso()
                mark_success(gw, control_sheet_id, render_job_id, run_id, now_utc=finished)
                run_row["status"] = RENDER_STATUS_SUCCESS
                run_row["finished_utc"] = finished
                run_row["updated_utc"] = finished
                run_row["artifacts_written"] = artifacts_written
                upsert_render_run(gw, control_sheet_id, run_row)
                _emit_event(
                    event="run_end",
                    stage="run",
                    message="render run succeeded",
                    code="",
                    artifacts_written=int(artifacts_written),
                    summary=dict(_EVENT_SUMMARY),
                )
                print(
                    f"Render run success: run_id={run_id} render_job_id={render_job_id} "
                    f"artifacts_written={artifacts_written}"
                )
                return 0

            _require_job_field(job, "stitch_preset", renderer=renderer)
            _require_job_field(job, "seed", renderer=renderer)
            _require_job_field(job, "preset", renderer=renderer)
            _require_job_field(job, "fps", renderer=renderer)
            _require_job_field(job, "aspect", renderer=renderer)
            _require_job_field(job, "bg_mode", renderer=renderer)

            per_cut_render = _pipeline_per_cut_render_enabled()

            def _run_pipeline_full_length() -> tuple[str, str, str, str, str, str, list[str], list[str]]:
                base_mp4_local = str(Path(paths["outputs"]) / "base.mp4")
                lyrics_mp4_local = str(Path(paths["outputs"]) / "lyrics.mp4")
                final_mp4_local = lyrics_mp4_local
                stitch_log_local = str(Path(paths["logs"]) / "stitch.log")
                lyrics_log_local = str(Path(paths["logs"]) / "lyrics.log")
                copy_log_local = ""

                stitch_job = {
                    "media_root_path": _build_stitcher_media_root(
                        list(resolved.get("clip_specs") or resolved.get("clip_paths") or []),
                        paths["clips"],
                        stitch_preset=str(job.get("stitch_preset") or ""),
                    ),
                    "input_audio_path": resolved["audio_path"],
                    "fps": str(job.get("fps") or ""),
                    "aspect": str(job.get("aspect") or ""),
                    "stitch_preset": str(job.get("stitch_preset") or ""),
                    "bpm": str(job.get("bpm") or manifest.get("stitch", {}).get("bpm") or ""),
                    "seed": str(job.get("seed") or ""),
                    "output_local_path": base_mp4_local,
                    "render_end_seconds": render_end_seconds,
                }
                stitch_cmd = build_vse_stitcher_command(stitch_job, blender_bin=blender_bin, script_path=VSE_STITCHER_SCRIPT_PATH)
                try:
                    _run_to_log(
                        stitch_cmd,
                        stitch_log_local,
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
                    shutil.copy2(resolved["clip_paths"][0], base_mp4_local)
                    with Path(stitch_log_local).open("a", encoding="utf-8") as lf:
                        lf.write("\n")
                        lf.write(f"stitch_fallback=copy_first_clip reason={stitch_exc}\n")
                if not Path(base_mp4_local).exists() and resolved["clip_paths"]:
                    shutil.copy2(resolved["clip_paths"][0], base_mp4_local)
                    with Path(stitch_log_local).open("a", encoding="utf-8") as lf:
                        lf.write("\n")
                        lf.write("stitch_fallback=copy_first_clip reason=missing_base_output\n")
                _emit_media_summary_event(path=base_mp4_local, stage="stitch", cut_idx=0)

                lyrics_job = {
                    "input_video_path": base_mp4_local,
                    "input_srt_path": resolved["lyrics_srt_path"],
                    "input_audio_path": resolved["audio_path"],
                    "fps": str(job.get("fps") or ""),
                    "aspect": str(job.get("aspect") or ""),
                    "preset": str(job.get("preset") or ""),
                    "bg_mode": str(job.get("bg_mode") or ""),
                    "output_local_path": lyrics_mp4_local,
                    "render_end_seconds": render_end_seconds,
                    "display_song_name": song_title_overlay["enabled"],
                    "song_name": song_title_overlay["text"],
                }
                lyrics_cmd = build_vse_text_command(lyrics_job, blender_bin=blender_bin, script_path=VSE_TEXT_SCRIPT_PATH)
                _run_to_log(
                    lyrics_cmd,
                    lyrics_log_local,
                    stage="lyrics",
                    retries=blender_stage_retries,
                    retry_delay_seconds=blender_retry_delay_s,
                )
                if not Path(lyrics_mp4_local).exists():
                    if main_lyrics_cues > 0 and not _allow_empty_lyrics_srt():
                        raise RuntimeError(
                            "pipeline_v1 legacy full-length lyrics stage produced no output despite non-empty lyrics SRT."
                        )
                    shutil.copy2(base_mp4_local, lyrics_mp4_local)
                    with Path(lyrics_log_local).open("a", encoding="utf-8") as lf:
                        lf.write("\n")
                        lf.write("lyrics_fallback=copy_base reason=missing_lyrics_output\n")
                _emit_media_summary_event(path=lyrics_mp4_local, stage="lyrics", cut_idx=0)

                if "copy" in stage_plan:
                    final_mp4_local = str(Path(paths["outputs"]) / "final.mp4")
                    copy_job = {
                        "input_video_path": lyrics_mp4_local,
                        "input_srt_path": resolved["copy_srt_path"],
                        "input_audio_path": resolved["audio_path"],
                        "fps": str(job.get("fps") or ""),
                        "aspect": str(job.get("aspect") or ""),
                        "preset": str(job.get("preset") or ""),
                        "bg_mode": str(job.get("bg_mode") or ""),
                        "output_local_path": final_mp4_local,
                        "render_end_seconds": render_end_seconds,
                    }
                    copy_log_local = str(Path(paths["logs"]) / "copy.log")
                    copy_cmd = build_vse_text_command(copy_job, blender_bin=blender_bin, script_path=VSE_TEXT_SCRIPT_PATH)
                    _run_to_log(
                        copy_cmd,
                        copy_log_local,
                        stage="copy",
                        retries=blender_stage_retries,
                        retry_delay_seconds=blender_retry_delay_s,
                    )
                    if Path(final_mp4_local).exists():
                        _emit_media_summary_event(path=final_mp4_local, stage="copy", cut_idx=0)

                # Some preset/script combinations can report success while not materializing lyrics output.
                # Keep pipeline moving by falling back to stitch output for cut extraction.
                if not Path(final_mp4_local).exists():
                    if Path(lyrics_mp4_local).exists():
                        final_mp4_local = lyrics_mp4_local
                    elif main_lyrics_cues > 0 and not _allow_empty_lyrics_srt():
                        raise RuntimeError(
                            "pipeline_v1 legacy full-length pipeline produced no usable lyrics/copy overlay output."
                        )
                    else:
                        final_mp4_local = base_mp4_local
                source_label = "copy" if copy_log_local else "lyrics"
                source_log = copy_log_local or lyrics_log_local
                selected_source = _pick_cut_source_video(
                    base_mp4=base_mp4_local,
                    candidate_mp4=final_mp4_local,
                    max_cut_end_s=max_cut_end_s,
                    candidate_label=source_label,
                    candidate_log_path=source_log,
                )
                if selected_source != final_mp4_local:
                    with Path(source_log).open("a", encoding="utf-8") as lf:
                        lf.write("\n")
                        lf.write(
                            "video_fallback=use_base_for_cuts reason=short_overlay_output "
                            f"required_cut_end_s={max_cut_end_s:.3f}\n"
                        )
                    if main_lyrics_cues > 0 and not _allow_empty_lyrics_srt():
                        raise RuntimeError(
                            "pipeline_v1 legacy full-length overlay output was shorter than required cut horizon "
                            "despite non-empty lyrics cues."
                        )
                    final_mp4_local = selected_source

                cut_logs_local: list[str] = []
                shorts_local: list[str] = []
                if isinstance(cuts, list):
                    if normalized_cuts:
                        source_duration = _probe_media_duration_seconds(final_mp4_local)
                        # If source media is unexpectedly short, fail fast instead of silently producing one-frame outputs.
                        if max_cut_end_s > (source_duration + 0.05):
                            raise RuntimeError(
                                "pipeline_v1 cut range exceeds rendered media duration "
                                f"(duration_s={source_duration:.3f} max_cut_end_s={max_cut_end_s:.3f} source={final_mp4_local})."
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
                            input_path=final_mp4_local,
                            start=start_tc,
                            end=end_tc,
                            output_path=out_short,
                        )
                        _run_to_log(cut_cmd, cut_log, stage=f"cut:{filename}")
                        shorts_local.append(out_short)
                        cut_logs_local.append(cut_log)
                return (
                    base_mp4_local,
                    lyrics_mp4_local,
                    final_mp4_local,
                    stitch_log_local,
                    lyrics_log_local,
                    copy_log_local,
                    shorts_local,
                    cut_logs_local,
                )

            cut_logs: list[str] = []
            shorts: list[str] = []

            # Exposed as top-level artifacts for backward compatibility.
            base_mp4 = str(Path(paths["outputs"]) / "base.mp4")
            lyrics_mp4 = str(Path(paths["outputs"]) / "lyrics.mp4")
            final_mp4 = str(Path(paths["outputs"]) / "final.mp4")
            stitch_log = str(Path(paths["logs"]) / "stitch.log")
            lyrics_log = str(Path(paths["logs"]) / "lyrics.log")
            copy_log = str(Path(paths["logs"]) / "copy.log") if "copy" in stage_plan else ""

            if per_cut_render and normalized_cuts:
                print("pipeline_v1_mode=per_cut")
                media_root_cache: dict[str, str] = {}
                have_primary = False
                cuts_with_lyrics = 0
                shared_auto_baseline_wps = 0.0
                effects_pass2_enabled = _pipeline_effects_pass2_enabled()
                effects_pass2_strict = _pipeline_effects_pass2_strict()
                keep_effects_plan_on_success = _pipeline_effects_keep_plan_on_success()

                for i, cut in enumerate(normalized_cuts, start=1):
                    start_tc = str(cut.get("start") or "").strip()
                    end_tc = str(cut.get("end") or "").strip()
                    filename = str(cut.get("filename") or f"short_{i:02d}.mp4").strip()
                    if not start_tc or not end_tc or not filename:
                        continue
                    _EVENT_SUMMARY["cuts_attempted"] = int(_EVENT_SUMMARY.get("cuts_attempted", 0)) + 1
                    start_s = _parse_ffmpeg_timestamp_to_seconds(start_tc)
                    end_s = _parse_ffmpeg_timestamp_to_seconds(end_tc)
                    if end_s <= start_s:
                        raise RuntimeError(f"Invalid cut range for {filename}: start={start_tc} end={end_tc}")
                    cut_duration_s = end_s - start_s

                    cut_stitch_preset = str(cut.get("stitch_preset") or job.get("stitch_preset") or "").strip()
                    cut_text_preset = str(cut.get("text_preset") or job.get("preset") or "").strip()
                    cut_seed = str(cut.get("seed") or job.get("seed") or "").strip()
                    cut_fps = str(cut.get("fps") or job.get("fps") or "").strip()
                    cut_aspect = str(cut.get("aspect") or job.get("aspect") or "").strip()
                    cut_bg_mode = str(cut.get("bg_mode") or job.get("bg_mode") or "").strip()
                    cut_bpm = str(job.get("bpm") or manifest.get("stitch", {}).get("bpm") or "").strip()
                    cut_collection_num = _normalize_collection_num(cut.get("collection_num"))

                    cut_clip_sources = list(resolved.get("clip_specs") or resolved.get("clip_paths") or [])
                    if cut_collection_num:
                        filtered_sources = [
                            item
                            for item in cut_clip_sources
                            if isinstance(item, dict)
                            and _normalize_collection_num(item.get("collection_num")) == cut_collection_num
                        ]
                        if not filtered_sources:
                            raise RuntimeError(
                                f"No clips available for cut collection_num={cut_collection_num} filename={filename}."
                            )
                        cut_clip_sources = filtered_sources

                    media_key = re.sub(r"[^a-z0-9]+", "_", cut_stitch_preset.lower()) or "default"
                    if cut_collection_num:
                        media_key = f"{media_key}__collection_{cut_collection_num}"
                    media_root = media_root_cache.get(media_key, "")
                    if not media_root:
                        media_root = _build_stitcher_media_root(
                            cut_clip_sources,
                            str(Path(paths["clips"]) / f"preset_{media_key}"),
                            stitch_preset=cut_stitch_preset,
                        )
                        media_root_cache[media_key] = media_root

                    cut_prefix = f"cut_{i:02d}"
                    cut_audio_path = str(Path(paths["inputs"]) / f"{cut_prefix}_audio.wav")
                    cut_lyrics_srt_path = str(Path(paths["inputs"]) / f"{cut_prefix}_lyrics.srt")
                    cut_copy_srt_path = str(Path(paths["inputs"]) / f"{cut_prefix}_copy.srt")

                    cut_audio_log = str(Path(paths["logs"]) / f"{cut_prefix}_audio.log")
                    cut_audio_cmd = build_ffmpeg_audio_trim_command(
                        ffmpeg_bin=ffmpeg_bin,
                        input_path=resolved["audio_path"],
                        start=start_tc,
                        end=end_tc,
                        output_path=cut_audio_path,
                    )
                    _run_to_log(cut_audio_cmd, cut_audio_log, stage=f"cut_audio:{filename}", cut_idx=i)

                    _slice_and_shift_srt_file(
                        input_path=resolved["lyrics_srt_path"],
                        output_path=cut_lyrics_srt_path,
                        start_s=start_s,
                        end_s=end_s,
                    )
                    cut_lyrics_cues = _count_srt_cues(cut_lyrics_srt_path)
                    has_lyrics_for_cut = cut_lyrics_cues > 0
                    print(
                        f"per_cut_lyrics_srt cut_idx={i} filename={filename} "
                        f"cue_count={cut_lyrics_cues} start_s={start_s:.3f} end_s={end_s:.3f}"
                    )
                    if has_lyrics_for_cut:
                        cuts_with_lyrics += 1
                    has_copy_for_cut = False
                    if "copy" in stage_plan and resolved.get("copy_srt_path"):
                        has_copy_for_cut = _slice_and_shift_srt_file(
                            input_path=str(resolved["copy_srt_path"]),
                            output_path=cut_copy_srt_path,
                            start_s=start_s,
                            end_s=end_s,
                        )

                    # Per-cut architecture: render each short directly to the cut duration.
                    cut_render_end = max(0.001, cut_duration_s)
                    cut_expected_frames = max(1, int(round(cut_render_end * float(cut_fps))))
                    out_short = str(Path(paths["outputs"]) / filename)
                    cut_base_mp4 = str(Path(paths["outputs"]) / f"{cut_prefix}_base.mp4")
                    cut_effects_plan = str(Path(paths["outputs"]) / f"{cut_prefix}_effects_plan.json")
                    cut_effects_mp4 = str(Path(paths["outputs"]) / f"{cut_prefix}_effects.mp4")
                    cut_lyrics_mp4 = (
                        str(Path(paths["outputs"]) / f"{cut_prefix}_lyrics.mp4")
                        if ("copy" in stage_plan and has_copy_for_cut)
                        else out_short
                    )
                    cut_final_stage_mp4 = out_short
                    cut_stitch_log = str(Path(paths["logs"]) / f"{cut_prefix}_stitch.log")
                    cut_effects_log = str(Path(paths["logs"]) / f"{cut_prefix}_effects.log")
                    cut_lyrics_log = str(Path(paths["logs"]) / f"{cut_prefix}_lyrics.log")
                    cut_copy_log = str(Path(paths["logs"]) / f"{cut_prefix}_copy.log")

                    stitch_job = {
                        "media_root_path": media_root,
                        "input_audio_path": cut_audio_path,
                        "fps": cut_fps,
                        "aspect": cut_aspect,
                        "stitch_preset": cut_stitch_preset,
                        "bpm": cut_bpm,
                        "seed": cut_seed,
                        "output_local_path": cut_base_mp4,
                        "render_end_seconds": cut_render_end,
                        "effects_pass1_mode": "base_only" if effects_pass2_enabled else "legacy",
                        "effects_plan_out": cut_effects_plan if effects_pass2_enabled else "",
                        "run_id": run_id,
                        "cut_idx": i,
                    }
                    stitch_cmd = build_vse_stitcher_command(
                        stitch_job,
                        blender_bin=blender_bin,
                        script_path=VSE_STITCHER_SCRIPT_PATH,
                    )
                    _assert_dir_exists(media_root, label=f"cut_idx={i} stitch media_root")
                    _assert_file_exists(cut_audio_path, label=f"cut_idx={i} stitch audio")
                    try:
                        _run_to_log(
                            stitch_cmd,
                            cut_stitch_log,
                            stage=f"stitch:{filename}",
                            cut_idx=i,
                            retries=blender_stage_retries,
                            retry_delay_seconds=blender_retry_delay_s,
                            output_probe_path=cut_base_mp4,
                        )
                    except Exception as stitch_exc:
                        if effects_pass2_enabled:
                            raise RuntimeError(
                                f"effects_contract_fail reason=stitch_stage_failed_with_pass2 cut_idx={i} filename={filename} error={stitch_exc}"
                            ) from stitch_exc
                        allow_stitch_fallback = os.environ.get("PIPELINE_STITCH_FALLBACK_COPY_FIRST", "1").strip() == "1"
                        if not allow_stitch_fallback:
                            raise
                        if not resolved["clip_paths"]:
                            raise
                        shutil.copy2(resolved["clip_paths"][0], cut_base_mp4)
                        with Path(cut_stitch_log).open("a", encoding="utf-8") as lf:
                            lf.write("\n")
                            lf.write(f"stitch_fallback=copy_first_clip reason={stitch_exc}\n")
                    if not Path(cut_base_mp4).exists() and resolved["clip_paths"]:
                        if effects_pass2_enabled:
                            raise RuntimeError(
                                f"effects_contract_fail reason=missing_base_output_with_pass2 cut_idx={i} filename={filename}"
                            )
                        shutil.copy2(resolved["clip_paths"][0], cut_base_mp4)
                        with Path(cut_stitch_log).open("a", encoding="utf-8") as lf:
                            lf.write("\n")
                            lf.write("stitch_fallback=copy_first_clip reason=missing_base_output\n")
                    _emit_media_summary_event(path=cut_base_mp4, stage=f"stitch:{filename}", cut_idx=i)

                    source_for_lyrics = cut_base_mp4
                    if effects_pass2_enabled:
                        if not Path(cut_effects_plan).exists():
                            raise RuntimeError(
                                f"effects_contract_fail reason=missing_effects_plan cut_idx={i} filename={filename} "
                                f"path={cut_effects_plan}"
                            )
                        else:
                            plan_sha = hashlib.sha1(Path(cut_effects_plan).read_bytes()).hexdigest()[:12]
                            print(
                                "effects_plan_written "
                                f"cut_idx={i} filename={filename} path={cut_effects_plan} sha1={plan_sha}"
                            )
                            effects_cmd = build_vse_effects_command(
                                {
                                    "input_video_path": cut_base_mp4,
                                    "input_audio_path": cut_audio_path,
                                    "effects_plan_path": cut_effects_plan,
                                    "fps": cut_fps,
                                    "aspect": cut_aspect,
                                    "output_local_path": cut_effects_mp4,
                                    "render_end_seconds": cut_render_end,
                                },
                                blender_bin=blender_bin,
                                script_path=VSE_EFFECTS_SCRIPT_PATH,
                            )
                            _assert_file_exists(cut_base_mp4, label=f"cut_idx={i} effects base_video")
                            _assert_file_exists(cut_effects_plan, label=f"cut_idx={i} effects plan")
                            _run_to_log(
                                effects_cmd,
                                cut_effects_log,
                                stage=f"effects:{filename}",
                                cut_idx=i,
                                retries=blender_stage_retries,
                                retry_delay_seconds=blender_retry_delay_s,
                                output_probe_path=cut_effects_mp4,
                            )
                            if not Path(cut_effects_mp4).exists():
                                raise RuntimeError(
                                    f"effects_contract_fail reason=missing_effects_output cut_idx={i} filename={filename}"
                                )
                            _assert_expected_cut_output(
                                output_path=cut_effects_mp4,
                                expected_duration_s=float(cut_render_end),
                                expected_frames=int(cut_expected_frames),
                                stage=f"effects:{filename}",
                                cut_idx=i,
                            )
                            _emit_media_summary_event(path=cut_effects_mp4, stage=f"effects:{filename}", cut_idx=i)
                            print(
                                "effects_apply_done "
                                f"cut_idx={i} filename={filename} output={cut_effects_mp4}"
                            )
                            source_for_lyrics = cut_effects_mp4

                    lyrics_job = {
                        "input_video_path": source_for_lyrics,
                        "input_srt_path": cut_lyrics_srt_path,
                        "input_audio_path": cut_audio_path,
                        "fps": cut_fps,
                        "aspect": cut_aspect,
                        "preset": cut_text_preset,
                        "bg_mode": cut_bg_mode,
                        "output_local_path": cut_lyrics_mp4,
                        "render_end_seconds": cut_render_end,
                        "display_song_name": song_title_overlay["enabled"],
                        "song_name": song_title_overlay["text"],
                    }
                    if shared_auto_baseline_wps > 0.0:
                        lyrics_job["auto_baseline_wps"] = shared_auto_baseline_wps
                    if has_lyrics_for_cut or song_title_overlay["enabled"]:
                        lyrics_cmd = build_vse_text_command(
                            lyrics_job,
                            blender_bin=blender_bin,
                            script_path=VSE_TEXT_SCRIPT_PATH,
                        )
                        _assert_file_exists(source_for_lyrics, label=f"cut_idx={i} lyrics source_video")
                        _assert_file_exists(cut_audio_path, label=f"cut_idx={i} lyrics audio")
                        _assert_file_exists(cut_lyrics_srt_path, label=f"cut_idx={i} lyrics srt")
                        _run_to_log(
                            lyrics_cmd,
                            cut_lyrics_log,
                            stage=f"lyrics:{filename}",
                            cut_idx=i,
                            retries=blender_stage_retries,
                            retry_delay_seconds=blender_retry_delay_s,
                            output_probe_path=cut_lyrics_mp4,
                        )
                        _materialize_video_fallback_output(
                            output_path=cut_lyrics_mp4,
                            source_path=source_for_lyrics,
                            log_path=cut_lyrics_log,
                            note="lyrics_fallback=copy_source reason=missing_or_empty_lyrics_output",
                        )
                        if shared_auto_baseline_wps <= 0.0:
                            shared_auto_baseline_wps = _extract_reveal_auto_speed_baseline_from_log(cut_lyrics_log)
                            if shared_auto_baseline_wps > 0.0:
                                print(
                                    "per_cut_reveal_auto_anchor "
                                    f"cut_idx={i} filename={filename} baseline_wps={shared_auto_baseline_wps:.3f}"
                                )
                        try:
                            _emit_media_summary_event(path=cut_lyrics_mp4, stage=f"lyrics:{filename}", cut_idx=i)
                        except RuntimeError as exc:
                            msg = str(exc)
                            if "ffprobe failed for" not in msg:
                                raise
                            _materialize_video_fallback_output(
                                output_path=cut_lyrics_mp4,
                                source_path=source_for_lyrics,
                                log_path=cut_lyrics_log,
                                note="lyrics_fallback=copy_source reason=ffprobe_probe_failed",
                            )
                            _emit_media_summary_event(path=cut_lyrics_mp4, stage=f"lyrics:{filename}", cut_idx=i)
                    else:
                        _materialize_video_fallback_output(
                            output_path=cut_lyrics_mp4,
                            source_path=source_for_lyrics,
                            log_path=cut_lyrics_log,
                            note="lyrics_fallback=copy_source reason=empty_lyrics_srt",
                        )
                        _emit_media_summary_event(path=cut_lyrics_mp4, stage=f"lyrics:{filename}", cut_idx=i)

                    selected_cut_source = cut_lyrics_mp4
                    selected_copy_log = ""
                    if "copy" in stage_plan and has_copy_for_cut:
                        copy_job = {
                            "input_video_path": cut_lyrics_mp4,
                            "input_srt_path": cut_copy_srt_path,
                            "input_audio_path": cut_audio_path,
                            "fps": cut_fps,
                            "aspect": cut_aspect,
                            "preset": cut_text_preset,
                            "bg_mode": cut_bg_mode,
                            "output_local_path": cut_final_stage_mp4,
                            "render_end_seconds": cut_render_end,
                        }
                        copy_cmd = build_vse_text_command(
                            copy_job,
                            blender_bin=blender_bin,
                            script_path=VSE_TEXT_SCRIPT_PATH,
                        )
                        _assert_file_exists(cut_lyrics_mp4, label=f"cut_idx={i} copy source_video")
                        _assert_file_exists(cut_audio_path, label=f"cut_idx={i} copy audio")
                        _assert_file_exists(cut_copy_srt_path, label=f"cut_idx={i} copy srt")
                        _run_to_log(
                            copy_cmd,
                            cut_copy_log,
                            stage=f"copy:{filename}",
                            cut_idx=i,
                            retries=blender_stage_retries,
                            retry_delay_seconds=blender_retry_delay_s,
                            output_probe_path=cut_final_stage_mp4,
                        )
                        _materialize_video_fallback_output(
                            output_path=cut_final_stage_mp4,
                            source_path=cut_lyrics_mp4,
                            log_path=cut_copy_log,
                            note="copy_fallback=use_lyrics reason=missing_or_empty_copy_output",
                        )
                        _emit_media_summary_event(path=cut_final_stage_mp4, stage=f"copy:{filename}", cut_idx=i)
                        selected_cut_source = cut_final_stage_mp4
                        selected_copy_log = cut_copy_log

                    if selected_cut_source != out_short:
                        shutil.copy2(selected_cut_source, out_short)
                    _assert_expected_cut_output(
                        output_path=out_short,
                        expected_duration_s=float(cut_render_end),
                        expected_frames=int(cut_expected_frames),
                        stage=f"short:{filename}",
                        cut_idx=i,
                    )
                    _emit_media_summary_event(path=out_short, stage=f"short:{filename}", cut_idx=i)
                    _EVENT_SUMMARY["cuts_succeeded"] = int(_EVENT_SUMMARY.get("cuts_succeeded", 0)) + 1

                    cut_log = selected_copy_log or cut_lyrics_log
                    cut_logs.append(cut_log)

                    if is_drive_output:
                        if client is None:
                            raise RuntimeError("Drive client is required for output_mode=drive")
                        short_row = _build_output_row_from_file(
                            run_id=run_id,
                            render_job_id=render_job_id,
                            campaign_id=str(job.get("campaign_id") or ""),
                            artifact_type=f"pipeline_short_{i:02d}",
                            file_path=out_short,
                            created_utc=_utc_now_iso(),
                        )
                        short_artifact_type = str(short_row.get("artifact_type") or "")
                        short_filename = str(short_row.get("filename") or "")
                        if should_upload_artifact(
                            existing_rows,
                            run_id=run_id,
                            artifact_type=short_artifact_type,
                            filename=short_filename,
                        ):
                            short_row = _upload_confirm_record_and_delete(
                                gw=gw,
                                control_sheet_id=control_sheet_id,
                                client=client,
                                artifact_row=short_row,
                                drive_folder_id=drive_folder_id,
                                verify_mode=verify_mode,
                            )
                        else:
                            existing = _find_existing_uploaded_artifact(
                                existing_rows,
                                run_id=run_id,
                                artifact_type=short_artifact_type,
                                filename=short_filename,
                            )
                            if existing:
                                short_row["drive_file_id"] = str(existing.get("drive_file_id") or "")
                                short_row["drive_folder_id"] = str(existing.get("drive_folder_id") or drive_folder_id)
                            append_outputs_index_rows(gw, control_sheet_id, [short_row])
                            _delete_local_file_best_effort(out_short)
                        artifacts_written += 1
                        existing_rows.append(short_row)
                        uploaded_short_rows.append(short_row)
                    else:
                        shorts.append(out_short)

                    preserve_primary_files = (not have_primary) and (not is_drive_output)
                    # Free per-cut intermediates once the short result is safely persisted.
                    for transient in (cut_audio_path, cut_lyrics_srt_path, cut_copy_srt_path):
                        _delete_local_file_best_effort(transient)
                    if not preserve_primary_files:
                        for transient in (cut_base_mp4, cut_effects_mp4, cut_lyrics_mp4):
                            _delete_local_file_best_effort(transient)
                        if effects_pass2_enabled and (not keep_effects_plan_on_success):
                            _delete_local_file_best_effort(cut_effects_plan)
                        if selected_copy_log and cut_final_stage_mp4 != out_short:
                            _delete_local_file_best_effort(cut_final_stage_mp4)

                    if not have_primary:
                        have_primary = True
                        base_mp4 = cut_base_mp4
                        lyrics_mp4 = cut_lyrics_mp4
                        final_mp4 = selected_cut_source
                        stitch_log = cut_stitch_log
                        lyrics_log = cut_lyrics_log
                        copy_log = selected_copy_log

                if not have_primary:
                    raise RuntimeError("pipeline_v1 per-cut mode found no usable cuts to render.")
                if _should_fallback_to_legacy_full_render(
                    main_lyrics_cues=main_lyrics_cues,
                    cuts_with_lyrics=cuts_with_lyrics,
                ):
                    raise RuntimeError(
                        "pipeline_v1 per-cut mode produced zero lyric-bearing cuts "
                        f"despite non-empty lyrics SRT (main_lyrics_cues={main_lyrics_cues})."
                    )
            else:
                if not per_cut_render:
                    raise RuntimeError(
                        "pipeline_v1 legacy full-length mode is disabled in this build. "
                        "Set PIPELINE_PER_CUT_RENDER=1."
                    )
                raise RuntimeError("pipeline_v1 per-cut mode requires at least one valid manifest cut.")

            created = _utc_now_iso()
            campaign_id = str(job.get("campaign_id") or "")
            artifact_rows: list[dict[str, Any]] = []
            if not (is_drive_output and uploaded_short_rows):
                artifact_rows.extend(
                    [
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
                    ]
                )
            artifact_rows.extend(
                [
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
            )
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
            if not is_drive_output:
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
            events_path = _event_log_path()
            if events_path and Path(events_path).exists():
                artifact_rows.append(
                    _build_output_row_from_file(
                        run_id=run_id,
                        render_job_id=render_job_id,
                        campaign_id=campaign_id,
                        artifact_type="pipeline_events_jsonl",
                        file_path=events_path,
                        created_utc=created,
                    )
                )

            if is_drive_output:
                if client is None:
                    raise RuntimeError("Drive client is required for output_mode=drive")
                for row in artifact_rows:
                    artifact_type = str(row.get("artifact_type") or "")
                    filename = str(row.get("filename") or "")
                    if not _is_pipeline_drive_upload_artifact(
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
                        if verify_mode == "size":
                            expected_bytes = _expected_upload_verify_bytes(
                                row,
                                local_path=local_path,
                                artifact_type=artifact_type,
                            )
                            client.confirm_upload_size(drive_file_id, expected_bytes)
                        else:
                            raise RuntimeError(f"Unsupported PIPELINE_UPLOAD_VERIFY_MODE: {verify_mode}")
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
            artifacts_written += append_outputs_index_rows(gw, control_sheet_id, artifact_rows)
            pipeline_run_succeeded = True
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
        _emit_event(
            event="run_end",
            stage="run",
            message="render run succeeded",
            code="",
            artifacts_written=int(artifacts_written),
            summary=dict(_EVENT_SUMMARY),
        )
        print(f"Render run success: run_id={run_id} render_job_id={render_job_id} artifacts_written={artifacts_written}")
        return 0
    except Exception as e:
        if isinstance(e, DriveUploadError):
            e = RuntimeError(str(e))
        err_text = str(e)
        err_code = _classify_error_code(err_text)
        finished = _utc_now_iso()
        mark_failed(gw, control_sheet_id, render_job_id, run_id, str(e), now_utc=finished)
        run_row["status"] = RENDER_STATUS_FAILED
        run_row["finished_utc"] = finished
        run_row["updated_utc"] = finished
        run_row["error_msg"] = str(e)
        upsert_render_run(gw, control_sheet_id, run_row)
        _emit_event(
            event="run_fail",
            stage="run",
            severity="error",
            code=err_code,
            message="render run failed",
            exception=err_text,
            summary=dict(_EVENT_SUMMARY),
        )
        if pipeline_paths_for_cleanup is not None and _pipeline_forensics_on_failure():
            summary_path, bundle_path = _build_forensics_bundle(
                run_id=run_id,
                render_job_id=render_job_id,
                run_root=str(pipeline_paths_for_cleanup.get("root") or ""),
                error_message=err_text,
            )
            if summary_path:
                pipeline_paths_for_cleanup["diagnostics"] = str(Path(summary_path).parent.parent)
            if _pipeline_forensics_upload_enabled():
                try:
                    uploaded = _upload_failure_diagnostics(
                        gw=gw,
                        control_sheet_id=control_sheet_id,
                        client=pipeline_diag_client,
                        drive_folder_id=pipeline_diag_drive_folder_id,
                        run_id=run_id,
                        render_job_id=render_job_id,
                        campaign_id=pipeline_diag_campaign_id,
                        verify_mode=pipeline_diag_verify_mode,
                        summary_path=summary_path,
                        bundle_path=bundle_path,
                    )
                    if uploaded > 0:
                        run_row["artifacts_written"] = int(run_row.get("artifacts_written") or 0) + int(uploaded)
                        run_row["updated_utc"] = _utc_now_iso()
                        upsert_render_run(gw, control_sheet_id, run_row)
                except Exception as diag_exc:
                    _emit_event(
                        event="forensics_upload_fail",
                        stage="forensics",
                        severity="warning",
                        code=_classify_error_code(str(diag_exc)),
                        message="forensics upload failed",
                        exception=str(diag_exc),
                    )
        print(f"Render run failed: run_id={run_id} render_job_id={render_job_id} error={e}", file=sys.stderr)
        return 1
    finally:
        if pipeline_paths_for_cleanup is not None:
            _cleanup_pipeline_local_artifacts(
                pipeline_paths_for_cleanup,
                success=bool(pipeline_run_succeeded),
            )


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

#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.drive_upload import DriveUploadError, from_env as drive_upload_from_env  # noqa: E402
from so_sync.gateway import SheetsGateway  # noqa: E402
from so_sync.gcs_storage import GcsStorageError, GcsStorageClient  # noqa: E402
from so_sync.render_repo import find_render_job_by_id, upsert_render_job  # noqa: E402

CONFIG_SHEET_ID_DEFAULT = "YOUR_GOOGLE_SHEET_ID"
CONFIG_TAB_DEFAULT = "mode"
CONFIG_RANGE_DEFAULT = "A1:ZZ2"
CLIPS_LIBRARY_FOLDER_ID_DEFAULT = "YOUR_GOOGLE_SHEET_ID"
OUTPUT_PARENT_FOLDER_ID_DEFAULT = "YOUR_GOOGLE_SHEET_ID"
PERFORMANCE_SOURCE_FOLDER_ID_DEFAULT = "YOUR_GOOGLE_SHEET_ID"
DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
COLLECTION_FOLDER_NAME_RE = re.compile(r"^collection-(\d+)$", flags=re.IGNORECASE)
_CATEGORY_TOKEN_RE = re.compile(r"[a-z]+")
_CATEGORY_KEYWORDS: Mapping[str, str] = {
    "people": "P",
    "person": "P",
    "cars": "C",
    "car": "C",
    "fireworks": "F",
    "firework": "F",
    "urban": "U",
    "city": "U",
    "money": "M",
    "cash": "M",
    "travel": "T",
    "trip": "T",
}

EXEC_PRESET_MAP: Mapping[str, Dict[str, Any]] = {
    "exec_preset_1": {
        "stitch_preset": "4a4b4c",
        "text_preset": "brat_words_manual_lightgrey_narrow_pixelate_v007",
        "performance_preset": "4a4b4c",
        "fps": 30,
        "aspect": "9:16",
        "bg_mode": "none",
        "seed": 42,
    }
}

EDIT_MODE_FOUND_FOOTAGE = "found_footage"
EDIT_MODE_PERFORMANCE = "performance"
PERFORMANCE_MODE_SIMPLE = "simple_sync_cut"
PERFORMANCE_MODE_PRESET = "preset"
PERFORMANCE_PRESET_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)([a-fA-F])([a-zA-Z]*)")
PERFORMANCE_PRESET_ALLOWED_MODIFIERS = {"r", "b", "w"}
PERFORMANCE_PRESET_TIMING_MODE_BEAT_CUTS = "beat_cuts"
PERFORMANCE_PRESET_TIMING_MODE_NORMALIZED = "normalized"
PERFORMANCE_PRESET_FIELD_NAMES = [
    "Performance Preset",
    "Performance Presets",
    "performance_preset_dsl",
    "performance_preset",
    "performance_presets",
]
TEXT_PRESET_FIELD_NAMES = [
    "Lyric Preset Override",
    "Text Preset Override",
    "Lyric Preset",
    "Lyric Presets",
    "Text Preset",
    "Text Presets",
    "text_preset",
    "text preset",
    "lyric preset",
]
OUTPUT_PARENT_FOLDER_FIELD_NAMES = [
    "Output Parent Folder ID",
    "output_parent_folder_id",
    "Output Drive Folder ID",
    "output_drive_folder_id",
    "Output Folder ID",
    "output_folder_id",
    "Output Folder",
    "output_folder",
    "Video Output Folder ID",
    "video_output_folder_id",
    "Video Output Folder",
    "video_output_folder",
]
EDIT_MODE_FF_ONLY = "FF_ONLY"
EDIT_MODE_FF_PLUS_PERF = "FF_PLUS_PERF"
FOUND_FOOTAGE_MODE_TAB_DEFAULT = "found-footage-mode"
PERFORMANCE_MODE_TAB_DEFAULT = "performance-footage-mode"
HYBRID_MODE_TAB_DEFAULT = "hybrid-mode"
MODE_CONTROL_TAB_DEFAULT = "mode"
LEGACY_MODE_CONTROL_TAB = "api-read"


def ensure_drive_upload_credentials() -> str:
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if creds_path:
        return creds_path
    try:
        import google.auth

        google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
    except Exception as exc:
        raise DriveUploadError(
            "Drive auth preflight failed for compiler Drive listing/upload. "
            "Set GOOGLE_APPLICATION_CREDENTIALS to a Drive-enabled service account key JSON path, "
            "or run on attached service account credentials (ADC)."
        ) from exc
    return creds_path


def build_render_job_upsert_payload(
    existing: Mapping[str, Any],
    *,
    render_job_id: str,
    naming_convention: str,
    clips_library_folder_id: str,
    manifest_drive_file_id: str,
    audio_id: str,
    lyrics_id: str,
    copy_id: str,
    preset: Mapping[str, Any],
    bpm: str = "",
    edit_mode: str = EDIT_MODE_FOUND_FOOTAGE,
    performance_mode: str = "",
    performance_preset: str = "",
    output_parent_folder_id: str,
    now_iso: str,
) -> Dict[str, Any]:
    row = dict(existing)
    row.update(
        {
            "render_job_id": render_job_id,
            "enabled": "TRUE",
            "priority": str(existing.get("priority") or "10"),
            "status": "queued",
            "campaign_id": naming_convention,
            "input_mode": "drive",
            "input_drive_folder_id": clips_library_folder_id,
            "input_manifest_drive_file_id": manifest_drive_file_id,
            "audio_drive_file_id": audio_id,
            "srt_drive_file_id": lyrics_id,
            "copy_srt_drive_file_id": copy_id or "",
            "renderer": "pipeline_v1",
            "stitch_preset": preset["stitch_preset"],
            "bpm": str(bpm or existing.get("bpm") or ""),
            "preset": preset["text_preset"],
            "seed": int(preset["seed"]),
            "fps": int(preset["fps"]),
            "aspect": str(preset["aspect"]),
            "bg_mode": preset["bg_mode"],
            "edit_mode": str(edit_mode or EDIT_MODE_FOUND_FOOTAGE),
            "performance_mode": str(performance_mode or ""),
            "performance_preset": str(performance_preset or ""),
            "output_mode": "drive",
            "output_drive_folder_id": output_parent_folder_id,
            "output_filename": str(existing.get("output_filename") or ""),
            "output_local_path": "",
            "input_video_path": "",
            "input_audio_path": "",
            "input_srt_path": "",
            "lease_owner": "",
            "lease_until_utc": "",
            "run_id": "",
            "error_msg": "",
            "logs_drive_file_id": "",
            # Every compile run represents a fresh queueing cycle for this render_job_id.
            # Reset attempts so per-job retry logic starts from zero for the new batch.
            "attempts": "0",
            "max_attempts": str(existing.get("max_attempts") or "3"),
            "last_error_utc": "",
            "created_utc": str(existing.get("created_utc") or now_iso),
            "updated_utc": now_iso,
        }
    )
    return row


def _quote_tab(tab_name: str) -> str:
    escaped = str(tab_name).replace("'", "''")
    return f"'{escaped}'"


def _extract_first_values_matrix(data: Dict[str, Any]) -> list[list[Any]]:
    if "valueRanges" in data:
        ranges = data.get("valueRanges") or []
        if not ranges:
            return []
        return (ranges[0] or {}).get("values", []) or []
    return data.get("values", []) or []


def normalize_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"true", "1", "yes", "y"}


def normalize_edit_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return EDIT_MODE_FOUND_FOOTAGE
    if raw in {EDIT_MODE_FOUND_FOOTAGE, EDIT_MODE_PERFORMANCE}:
        return raw
    raise RuntimeError(
        f"Invalid Render Mode value {raw!r}; expected one of: {EDIT_MODE_FOUND_FOOTAGE}, {EDIT_MODE_PERFORMANCE}."
    )


def normalize_performance_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {PERFORMANCE_MODE_SIMPLE, PERFORMANCE_MODE_PRESET}:
        return raw
    raise RuntimeError(
        f"Invalid Performance Mode value {raw!r}; expected one of: {PERFORMANCE_MODE_SIMPLE}, {PERFORMANCE_MODE_PRESET}."
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
        modifiers = str(modifiers_raw or "").lower()
        for ch in modifiers:
            if ch not in PERFORMANCE_PRESET_ALLOWED_MODIFIERS:
                raise RuntimeError(
                    f"Unsupported performance preset modifier {ch!r} in {raw!r}; "
                    "allowed modifiers are: r, b, w."
                )
        out.append({"duration": duration, "clip": clip_label.lower(), "modifiers": modifiers})
        idx = m.end()
    if not out:
        raise RuntimeError(f"Performance preset has no valid tokens: {raw!r}")
    return out


def normalize_performance_preset(value: Any) -> str:
    raw = str(value or "").strip()
    _ = parse_performance_preset_tokens(raw)
    return raw


def resolve_performance_preset_timing_mode() -> str:
    raw = str(
        os.environ.get("PERFORMANCE_PRESET_TIMING_MODE", PERFORMANCE_PRESET_TIMING_MODE_BEAT_CUTS)
        or PERFORMANCE_PRESET_TIMING_MODE_BEAT_CUTS
    ).strip().lower()
    if raw not in {PERFORMANCE_PRESET_TIMING_MODE_BEAT_CUTS, PERFORMANCE_PRESET_TIMING_MODE_NORMALIZED}:
        raise RuntimeError(
            "Invalid PERFORMANCE_PRESET_TIMING_MODE; expected one of: beat_cuts, normalized "
            f"(got {raw!r})."
        )
    return raw


def _extract_drive_file_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if DRIVE_ID_RE.match(raw):
        return raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    path = str(parsed.path or "")
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", path)
    if m:
        return str(m.group(1) or "")
    q = parse_qs(str(parsed.query or ""))
    q_id = str((q.get("id") or [""])[0] or "").strip()
    if DRIVE_ID_RE.match(q_id):
        return q_id
    return ""


def _performance_clip_name_resolution_enabled() -> bool:
    raw = str(os.environ.get("PERFORMANCE_CLIP_NAME_RESOLUTION", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _resolve_render_mode_from_cfg(cfg: Mapping[str, Any]) -> str:
    render_mode_raw = str(
        _cfg_value_by_names(
            cfg,
            ["Render Mode", "render_mode"],
            default="",
        )
        or ""
    ).strip()
    if render_mode_raw:
        return normalize_edit_mode(render_mode_raw)
    edit_mode = str(_cfg_value_by_names(cfg, ["Edit Mode", "edit_mode"], default="") or "").strip().upper()
    if edit_mode == EDIT_MODE_FF_ONLY:
        return EDIT_MODE_FOUND_FOOTAGE
    if edit_mode == EDIT_MODE_FF_PLUS_PERF:
        return EDIT_MODE_PERFORMANCE
    mode_value = str(_cfg_value_by_names(cfg, ["Mode", "mode"], default="") or "").strip()
    normalized_mode = _normalize_mode_label(mode_value)
    if "performance" in normalized_mode or "hybrid" in normalized_mode:
        return EDIT_MODE_PERFORMANCE
    if "found" in normalized_mode and "footage" in normalized_mode:
        return EDIT_MODE_FOUND_FOOTAGE
    return EDIT_MODE_FOUND_FOOTAGE


def _resolve_performance_mode_from_cfg(cfg: Mapping[str, Any], *, render_mode: str) -> str:
    if render_mode != EDIT_MODE_PERFORMANCE:
        return ""
    override = str(os.environ.get("PERFORMANCE_MODE_OVERRIDE", "") or "").strip()
    if override:
        return normalize_performance_mode(override)
    raw = str(
        _cfg_value_by_names(
            cfg,
            ["Performance Mode", "performance_mode"],
            default="",
        )
        or ""
    ).strip()
    if not raw:
        return PERFORMANCE_MODE_SIMPLE
    return normalize_performance_mode(raw)


def _has_explicit_performance_mode(cfg: Mapping[str, Any]) -> bool:
    raw = str(
        _cfg_value_by_names(
            cfg,
            ["Performance Mode", "performance_mode"],
            default="",
        )
        or ""
    ).strip()
    return bool(raw)


def _has_explicit_performance_clip_ids(cfg: Mapping[str, Any]) -> bool:
    for label in ("A", "B", "C", "D", "E", "F"):
        key = f"Performance Clip {label} File ID"
        alt = f"performance_clip_{label.lower()}_drive_file_id"
        raw = str(_cfg_value_by_names(cfg, [key, alt], default="") or "").strip()
        if raw:
            return True
    return False


def _resolve_performance_audio_policy_from_cfg(cfg: Mapping[str, Any], *, render_mode: str) -> str:
    if render_mode != EDIT_MODE_PERFORMANCE:
        return ""
    raw = str(
        _cfg_value_by_names(
            cfg,
            ["Performance Audio Policy", "performance_audio_policy"],
            default="",
        )
        or ""
    ).strip().lower()
    if not raw:
        return "master_only"
    if raw != "master_only":
        raise RuntimeError(
            f"Invalid performance audio policy {raw!r}; expected master_only."
        )
    return raw


def _resolve_performance_source_folder_id(cfg: Mapping[str, Any]) -> str:
    env_val = str(os.environ.get("PERFORMANCE_SOURCE_FOLDER_ID", "") or "").strip()
    cfg_val = str(
        _cfg_value_by_names(
            cfg,
            [
                "Performance Source Folder ID",
                "performance_source_folder_id",
                "Performance Clips Folder ID",
                "performance_clips_folder_id",
            ],
            default="",
        )
        or ""
    ).strip()
    chosen = cfg_val or env_val or PERFORMANCE_SOURCE_FOLDER_ID_DEFAULT
    folder_id = _extract_drive_file_id(chosen)
    if not folder_id:
        m = re.search(r"/folders/([A-Za-z0-9_-]{20,})", str(chosen))
        if m:
            folder_id = str(m.group(1) or "")
    if folder_id:
        return folder_id
    if DRIVE_ID_RE.match(chosen):
        return chosen
    raise RuntimeError(
        f"Invalid performance source folder value {chosen!r}; expected Drive folder id or URL."
    )


def _resolve_output_parent_folder_id(cfg: Mapping[str, Any]) -> str:
    folder_id, _, _ = _resolve_output_parent_folder(cfg)
    return folder_id


def _resolve_output_parent_folder(cfg: Mapping[str, Any]) -> tuple[str, str, str]:
    env_val = str(os.environ.get("OUTPUT_PARENT_FOLDER_ID", "") or "").strip()
    cfg_val = str(
        _cfg_value_by_names(
            cfg,
            OUTPUT_PARENT_FOLDER_FIELD_NAMES,
            default="",
        )
        or ""
    ).strip()
    source = "config" if cfg_val else ("env" if env_val else "default")
    chosen = cfg_val or env_val or OUTPUT_PARENT_FOLDER_ID_DEFAULT
    folder_id = _extract_drive_file_id(chosen)
    if not folder_id:
        m = re.search(r"/folders/([A-Za-z0-9_-]{20,})", str(chosen))
        if m:
            folder_id = str(m.group(1) or "")
    if folder_id:
        return folder_id, source, chosen
    if DRIVE_ID_RE.match(chosen):
        return chosen, source, chosen
    raise RuntimeError(
        f"Invalid output parent folder value {chosen!r}; expected Drive folder id or URL."
    )


def _list_folder_files_cached(
    *,
    drive_client: Any,
    folder_id: str,
    listing_cache: dict[str, list[dict[str, str]]],
) -> list[dict[str, str]]:
    fid = str(folder_id or "").strip()
    if not fid:
        raise RuntimeError("Performance source folder id is required for name-based clip resolution.")
    if fid in listing_cache:
        return list(listing_cache[fid])
    rows = list(drive_client.list_files_in_folder(fid))
    listing_cache[fid] = rows
    return rows


def _resolve_performance_drive_file_ref(
    *,
    value: Any,
    label: str,
    drive_client: Any | None,
    source_folder_id: str,
    listing_cache: dict[str, list[dict[str, str]]] | None,
) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    direct_id = _extract_drive_file_id(raw)
    if direct_id:
        return direct_id
    if not _performance_clip_name_resolution_enabled():
        raise RuntimeError(
            f"{label} must be a Drive file id (or Drive URL) when PERFORMANCE_CLIP_NAME_RESOLUTION=0."
        )
    if drive_client is None or listing_cache is None:
        raise RuntimeError(f"{label} must be a Drive file id (name search unavailable in this path).")
    rows = _list_folder_files_cached(
        drive_client=drive_client,
        folder_id=source_folder_id,
        listing_cache=listing_cache,
    )
    exact = [r for r in rows if str(r.get("name") or "") == raw]
    if not exact:
        target = raw.lower()
        exact = [r for r in rows if str(r.get("name") or "").lower() == target]
    if not exact:
        raise RuntimeError(
            f"{label} name not found in performance source folder {source_folder_id}: {raw!r}"
        )
    ids = sorted({str(r.get("id") or "").strip() for r in exact if str(r.get("id") or "").strip()})
    if len(ids) != 1:
        raise RuntimeError(
            f"{label} name is ambiguous in performance source folder {source_folder_id}: {raw!r} ids={ids}"
        )
    return ids[0]


def _cfg_value_by_names(cfg: Mapping[str, Any], names: Iterable[str], default: Any = "") -> Any:
    for name in names:
        if name in cfg:
            return cfg.get(name)
    by_lower: dict[str, Any] = {str(k).strip().lower(): v for k, v in cfg.items()}
    for name in names:
        hit = by_lower.get(str(name).strip().lower())
        if hit is not None:
            return hit
    return default


def _normalize_mode_label(value: Any) -> str:
    out = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", out).strip()


def _mode_to_tab_name(mode_value: Any) -> str:
    normalized = _normalize_mode_label(mode_value)
    if not normalized:
        return ""
    if "hybrid" in normalized:
        return str(os.environ.get("HYBRID_MODE_TAB", HYBRID_MODE_TAB_DEFAULT)).strip() or HYBRID_MODE_TAB_DEFAULT
    if "performance" in normalized:
        return (
            str(os.environ.get("PERFORMANCE_MODE_TAB", PERFORMANCE_MODE_TAB_DEFAULT)).strip()
            or PERFORMANCE_MODE_TAB_DEFAULT
        )
    if "found" in normalized and "footage" in normalized:
        return (
            str(os.environ.get("FOUND_FOOTAGE_MODE_TAB", FOUND_FOOTAGE_MODE_TAB_DEFAULT)).strip()
            or FOUND_FOOTAGE_MODE_TAB_DEFAULT
        )
    return ""


def _is_mode_control_tab(tab_name: Any) -> bool:
    normalized = str(tab_name or "").strip().lower()
    if not normalized:
        return False
    configured = str(os.environ.get("MODE_CONTROL_TAB", MODE_CONTROL_TAB_DEFAULT) or "").strip().lower()
    if not configured:
        configured = MODE_CONTROL_TAB_DEFAULT
    return normalized in {configured, LEGACY_MODE_CONTROL_TAB}


def resolve_song_title_overlay(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    override_raw = str(os.environ.get("DISPLAY_SONG_NAME_OVERRIDE", "") or "").strip()
    if override_raw:
        override_enabled = normalize_bool(override_raw)
        if not override_enabled:
            return {"enabled": False, "text": ""}
        override_text = str(os.environ.get("SONG_NAME_OVERRIDE", "") or "").strip()
        song_name = override_text or str(_cfg_value_by_names(cfg, ["Song Name"], default="") or "").strip()
        if not song_name:
            raise RuntimeError("Display Song Name? override is enabled but Song Name is empty.")
        return {"enabled": True, "text": song_name}
    enabled = normalize_bool(
        _cfg_value_by_names(
            cfg,
            ["Display Song Name?", "Display Song Name"],
            default=False,
        )
    )
    if not enabled:
        return {"enabled": False, "text": ""}
    song_name = str(_cfg_value_by_names(cfg, ["Song Name"], default="") or "").strip()
    if not song_name:
        raise RuntimeError("Display Song Name? is enabled but Song Name is empty.")
    return {"enabled": True, "text": song_name}


def resolve_stitch_bpm(cfg: Mapping[str, Any]) -> float:
    candidates = [
        "Song BPM",
        "BPM",
        "Stitch BPM",
        "song bpm",
        "bpm",
    ]
    raw = ""
    for key in candidates:
        value = str(cfg.get(key) or "").strip()
        if value:
            raw = value
            break
    if not raw:
        raw = os.environ.get("PIPELINE_STITCH_BPM", "120").strip() or "120"
    try:
        bpm = float(raw)
    except Exception as exc:
        raise RuntimeError(f"Invalid BPM value: {raw!r}") from exc
    if bpm <= 0:
        raise RuntimeError(f"Invalid BPM value (must be > 0): {raw!r}")
    return bpm


def drive_reference_kind(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "empty"
    return "id" if DRIVE_ID_RE.match(raw) else "name"


def parse_hhmmssff_to_ms(value: str, *, fps: int = 30) -> int:
    m = re.match(r"^\s*(\d{2}):(\d{2}):(\d{2}):(\d{2})\s*$", str(value or ""))
    if not m:
        raise ValueError(f"Invalid HH:MM:SS:FF value: {value!r}")
    hh, mm, ss, ff = [int(x) for x in m.groups()]
    ff_mode = str(os.environ.get("TIMECODE_FF_MODE", "centiseconds") or "centiseconds").strip().lower()
    if ff_mode == "frames":
        fps_int = int(fps or 0)
        if fps_int <= 0:
            raise ValueError(f"Invalid fps for HH:MM:SS:FF parsing: {fps!r}")
        if ff >= fps_int:
            raise ValueError(f"Invalid FF value for fps={fps_int} (must be 00-{fps_int-1:02d}): {value!r}")
        ff_ms = int(round((ff / float(fps_int)) * 1000.0))
    else:
        if ff > 99:
            raise ValueError(f"Invalid FF value (must be 00-99): {value!r}")
        ff_ms = ff * 10
    total_ms = ((hh * 3600 + mm * 60 + ss) * 1000) + ff_ms
    return total_ms


def parse_hhmmssff_to_ffmpeg_ts(value: str, *, fps: int = 30) -> str:
    total_ms = parse_hhmmssff_to_ms(value, fps=fps)
    hh = total_ms // 3_600_000
    rem = total_ms % 3_600_000
    mm = rem // 60_000
    rem = rem % 60_000
    ss = rem // 1000
    ms = rem % 1000
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"


def trailing_width(text: str) -> int:
    m = re.search(r"(\d+)$", str(text or ""))
    return len(m.group(1)) if m else 3


def video_filename_from_naming(naming_convention: str, index: int, ext: str = ".mp4") -> str:
    raw = str(naming_convention or "").strip()
    if not raw:
        return f"video_{index:03d}{ext}"
    m = re.search(r"(\d+)$", raw)
    if not m:
        return f"{raw}_{index:03d}{ext}"
    start, end = m.span(1)
    width = end - start
    prefix = raw[:start]
    return f"{prefix}{index:0{width}d}{ext}"


def short_filename_from_naming(naming_convention: str, index: int) -> str:
    width = trailing_width(naming_convention)
    return f"short_{index:0{width}d}.mp4"


def _header_map(headers: Iterable[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i, h in enumerate(headers):
        key = str(h or "").strip().lower()
        if key:
            out[key] = i
    return out


def _cell(row: list[Any], idx: int | None) -> Any:
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def _cfg_get(cfg: Mapping[str, Any], key: str, default: Any = "") -> Any:
    return cfg.get(key, default)


def build_cuts_from_config(
    cfg: Mapping[str, Any],
    *,
    naming_convention: str,
    start_index: int = 1,
    fps: int = 30,
) -> list[dict[str, str]]:
    cuts: list[dict[str, str]] = []
    if not normalize_bool(_cfg_get(cfg, "Cut Clips?", False)):
        return cuts
    out_idx = 0
    for slot in range(1, 7):
        enabled = normalize_bool(_cfg_get(cfg, f"Clip {slot}", False))
        if not enabled:
            continue
        start = str(
            _cfg_get(cfg, f"Clip {slot} Start", _cfg_get(cfg, f"Clip {slot} - start", _cfg_get(cfg, f"Clip {slot} start", "")))
        ).strip()
        end = str(
            _cfg_get(cfg, f"Clip {slot} End", _cfg_get(cfg, f"Clip {slot} - end", _cfg_get(cfg, f"Clip {slot} end", "")))
        ).strip()
        if not start or not end:
            continue
        start_ms = parse_hhmmssff_to_ms(start, fps=fps)
        end_ms = parse_hhmmssff_to_ms(end, fps=fps)
        if end_ms <= start_ms:
            raise ValueError(f"Clip {slot} has end <= start: {start} -> {end}")
        cuts.append(
            {
                "id": f"clip_{slot}",
                "start": parse_hhmmssff_to_ffmpeg_ts(start, fps=fps),
                "end": parse_hhmmssff_to_ffmpeg_ts(end, fps=fps),
                "filename": video_filename_from_naming(naming_convention, start_index + out_idx),
            }
        )
        out_idx += 1
    return cuts


def _is_video_like(name: str, mime_type: str) -> bool:
    mime = str(mime_type or "").strip().lower()
    if mime.startswith("video/"):
        return True
    lower_name = str(name or "").strip().lower()
    return lower_name.endswith(".mp4") or lower_name.endswith(".mov")


def _normalize_category_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for ch in text:
        if ch.isalpha():
            return ch.upper()
    return ""


def _infer_category_from_name(name: str) -> str:
    lowered = str(name or "").strip().lower()
    if not lowered:
        return ""
    tokens = _CATEGORY_TOKEN_RE.findall(lowered)
    if not tokens:
        return ""
    for token in tokens:
        mapped = _CATEGORY_KEYWORDS.get(token)
        if mapped:
            return mapped
    first = tokens[0]
    if first and first[0].isalpha():
        return first[0].upper()
    return ""


def build_manifest_clips(
    files: list[dict[str, str]],
    *,
    seed: int,
    max_clips: int = 80,
) -> list[dict[str, str]]:
    filtered = [
        {
            "drive_file_id": str(f.get("id") or "").strip(),
            "name": str(f.get("name") or ""),
            "mimeType": str(f.get("mimeType") or ""),
            "category": _normalize_category_key(f.get("category") or ""),
            "collection_num": str(f.get("collection_num") or "").strip(),
            "gcs_uri": str(f.get("gcs_uri") or "").strip(),
            "gcs_object": str(f.get("gcs_object") or "").strip(),
        }
        for f in files
        if str(f.get("id") or "").strip() and _is_video_like(str(f.get("name") or ""), str(f.get("mimeType") or ""))
    ]
    filtered.sort(key=lambda x: (x["name"], x["drive_file_id"]))
    rng = random.Random(int(seed))
    rng.shuffle(filtered)
    selected = filtered[: max(0, int(max_clips))]
    out: list[dict[str, str]] = []
    for i, row in enumerate(selected, start=1):
        clip_row = {"drive_file_id": row["drive_file_id"], "label": f"c{i:03d}"}
        cat = str(row.get("category") or "").strip()
        if cat:
            clip_row["category"] = cat
        collection_num = str(row.get("collection_num") or "").strip()
        if collection_num:
            clip_row["collection_num"] = collection_num
        gcs_uri = str(row.get("gcs_uri") or "").strip()
        gcs_object = str(row.get("gcs_object") or "").strip()
        if gcs_uri:
            clip_row["gcs_uri"] = gcs_uri
        if gcs_object:
            clip_row["gcs_object"] = gcs_object
        out.append(clip_row)
    return out


def collect_files_from_folder_tree(
    drive_client: Any,
    root_folder_id: str,
    *,
    max_depth: int = 3,
) -> list[dict[str, str]]:
    root = str(root_folder_id or "").strip()
    if not root:
        return []
    capped_depth = max(0, int(max_depth))
    queue: list[tuple[str, int, str]] = [(root, 0, "")]
    seen_folders: set[str] = {root}
    files: list[dict[str, str]] = []
    while queue:
        folder_id, depth, inherited_category = queue.pop(0)
        rows = drive_client.list_files_in_folder(folder_id)
        for row in rows:
            file_id = str(row.get("id") or "").strip()
            mime = str(row.get("mimeType") or "").strip()
            name = str(row.get("name") or "")
            if not file_id:
                continue
            if mime == DRIVE_FOLDER_MIME and depth < capped_depth:
                child_category = _normalize_category_key(inherited_category) or _infer_category_from_name(name)
                if file_id not in seen_folders:
                    seen_folders.add(file_id)
                    queue.append((file_id, depth + 1, child_category))
                continue
            inferred_category = _normalize_category_key(inherited_category) or _infer_category_from_name(name)
            files.append(
                {
                    "id": file_id,
                    "name": name,
                    "mimeType": mime,
                    "size": str(row.get("size") or ""),
                    "category": inferred_category,
                }
            )
    return files


def _collection_num_from_folder_name(name: str) -> int | None:
    raw = str(name or "").strip()
    if not raw:
        return None
    m = COLLECTION_FOLDER_NAME_RE.match(raw)
    if not m:
        return None
    return int(m.group(1))


def resolve_collection_folder_ids(
    cfg: Mapping[str, Any],
    *,
    drive_client: Any,
    root_folder_id: str,
) -> list[dict[str, str]]:
    _ = cfg  # Selection is inferred from Drive folder names only.
    direct_children = drive_client.list_files_in_folder(root_folder_id)
    folder_rows = [row for row in direct_children if str(row.get("mimeType") or "").strip() == DRIVE_FOLDER_MIME]
    grouped: dict[int, list[tuple[str, str]]] = {}
    for row in folder_rows:
        fid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        num = _collection_num_from_folder_name(name)
        if num is None:
            continue
        if not fid:
            continue
        grouped.setdefault(num, []).append((name, fid))

    duplicates = {num: rows for num, rows in grouped.items() if len(rows) > 1}
    if duplicates:
        parts = []
        for num in sorted(duplicates.keys()):
            rows = duplicates[num]
            ids = ",".join(str(fid) for _, fid in rows)
            names = ",".join(str(name) for name, _ in rows)
            parts.append(f"collection-{num} names=[{names}] ids=[{ids}]")
        raise RuntimeError(
            "Duplicate collection folder numbers found under clips root. "
            "Each collection-<number> must be unique. "
            f"duplicates={parts}"
        )

    found: list[tuple[int, str]] = [(num, rows[0][1]) for num, rows in grouped.items()]
    found.sort(key=lambda row: row[0])
    if not found:
        available = sorted(str(row.get("name") or "").strip() for row in folder_rows if str(row.get("name") or "").strip())
        raise RuntimeError(
            "No valid collection folders found under clips root. "
            f"Expected direct subfolders named 'collection-<number>' under {root_folder_id}. "
            f"available_subfolders={available}"
        )
    resolved: list[dict[str, str]] = [{"collection_num": str(num), "folder_id": fid} for num, fid in found]
    return resolved


def _clips_source_mode() -> str:
    explicit = str(os.environ.get("CLIPS_LIBRARY_SOURCE", "")).strip().lower()
    if explicit in {"drive", "gcs_mirror"}:
        return explicit
    return "drive"


def _clips_mirror_index_object(prefix: str) -> str:
    explicit = str(os.environ.get("CLIPS_LIBRARY_MIRROR_INDEX_OBJECT", "")).strip()
    if explicit:
        return explicit
    pre = str(prefix or "").strip().strip("/")
    if not pre:
        pre = "library-mirror"
    return f"{pre}/indexes/library_mirror_index.json"


def _mirror_warn_stale_hours() -> float:
    raw = str(os.environ.get("CLIPS_LIBRARY_MIRROR_WARN_STALE_HOURS", "24")).strip()
    try:
        return max(0.0, float(raw))
    except Exception:
        return 24.0


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


def _collection_category_counts(files: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in files:
        collection_num = str(row.get("collection_num") or "").strip()
        if not collection_num:
            continue
        cat = _normalize_category_key(row.get("category") or "")
        if not cat:
            continue
        bucket = out.setdefault(collection_num, {})
        bucket[cat] = int(bucket.get(cat, 0)) + 1
    return out


def _load_collection_pools_from_gcs_mirror(
    *,
    bucket: str,
    prefix: str,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    bucket_name = str(bucket or "").strip()
    if not bucket_name:
        raise RuntimeError("CLIPS_LIBRARY_MIRROR_BUCKET is required when CLIPS_LIBRARY_SOURCE=gcs_mirror")
    mirror_prefix = str(prefix or "").strip().strip("/") or "library-mirror"
    index_object = _clips_mirror_index_object(mirror_prefix)
    try:
        gcs = GcsStorageClient.from_adc(bucket_name)
        raw = gcs.download_text(index_object)
    except GcsStorageError as exc:
        raise RuntimeError(f"Failed to load mirror index from bucket={bucket_name} object={index_object}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"Mirror index is not valid JSON: gs://{bucket_name}/{index_object}") from exc

    clips_raw = payload.get("clips") if isinstance(payload, dict) else None
    if not isinstance(clips_raw, list) or not clips_raw:
        raise RuntimeError(f"Mirror index has no clips: gs://{bucket_name}/{index_object}")

    grouped: dict[str, list[dict[str, str]]] = {}
    for item in clips_raw:
        if not isinstance(item, dict):
            continue
        drive_file_id = str(item.get("drive_file_id") or item.get("id") or "").strip()
        collection_num = str(item.get("collection_num") or "").strip()
        if not drive_file_id or not collection_num:
            continue
        row = {
            "id": drive_file_id,
            "name": str(item.get("name") or ""),
            "mimeType": str(item.get("mimeType") or "video/mp4"),
            "size": str(item.get("size") or ""),
            "category": _normalize_category_key(item.get("category") or ""),
            "collection_num": collection_num,
            "gcs_uri": str(item.get("gcs_uri") or ""),
            "gcs_object": str(item.get("gcs_object") or ""),
        }
        grouped.setdefault(collection_num, []).append(row)

    pools: list[dict[str, Any]] = []
    for collection_num in sorted(grouped.keys(), key=lambda x: int(x) if str(x).isdigit() else 999999):
        files = grouped[collection_num]
        video_files = [f for f in files if _is_video_like(str(f.get("name") or ""), str(f.get("mimeType") or ""))]
        if not video_files:
            continue
        pools.append(
            {
                "collection_num": collection_num,
                "folder_id": "",
                "files": files,
            }
        )
    if not pools:
        raise RuntimeError(f"Mirror index has no usable collection pools: gs://{bucket_name}/{index_object}")
    total_clips_raw = payload.get("total_clips") if isinstance(payload, dict) else 0
    try:
        total_clips = int(total_clips_raw or 0)
    except Exception:
        total_clips = 0
    meta = {
        "generated_utc": str(payload.get("generated_utc") or "").strip() if isinstance(payload, dict) else "",
        "total_clips": total_clips,
    }
    return pools, index_object, meta


def resolve_drive_file_id(value: str, *, label: str, assets_folder_id: str, clips_library_folder_id: str) -> str:
    kind = drive_reference_kind(value)
    raw = str(value or "").strip()
    if kind == "empty":
        return ""
    if kind == "id":
        return raw
    raise RuntimeError(
        f"{label} must be a Drive file id (name search not supported by current drive-proxy). "
        "Paste the file id into the config sheet cell."
    )


def select_asset_drive_file_id(
    cfg: Mapping[str, Any],
    *,
    id_key: str,
    chip_key: str,
    label: str,
    optional: bool,
    assets_folder_id: str,
    clips_library_folder_id: str,
) -> str:
    id_val = str(cfg.get(id_key) or "").strip()
    chip_val = str(cfg.get(chip_key) or "").strip()
    if drive_reference_kind(id_val) == "id":
        return id_val
    if drive_reference_kind(chip_val) == "id":
        return chip_val
    if optional and not id_val and not chip_val:
        return ""
    if not optional and not id_val and not chip_val:
        raise RuntimeError(
            f"{label} must be a Drive file id (name search not supported by current drive-proxy). "
            "Paste the file id into the config sheet cell."
        )
    to_validate = id_val or chip_val
    return resolve_drive_file_id(
        to_validate,
        label=label,
        assets_folder_id=assets_folder_id,
        clips_library_folder_id=clips_library_folder_id,
    )


def _read_config_row(gateway: Any, config_sheet_id: str, config_tab: str, config_range: str) -> Dict[str, Any]:
    def _read_tab_as_row(tab_name: str) -> Dict[str, Any]:
        rng_local = f"{_quote_tab(tab_name)}!{config_range}"
        data_local = gateway.read(config_sheet_id, [rng_local])
        values_local = _extract_first_values_matrix(data_local)
        if len(values_local) < 2:
            raise RuntimeError(f"Expected header row + data row from {config_sheet_id} {rng_local}")
        header_local = values_local[0]
        row_local = values_local[1]
        idx_local = _header_map(header_local)
        out_local: Dict[str, Any] = {}
        for h, i in idx_local.items():
            out_local[str(header_local[i]).strip()] = _cell(row_local, i)
        return out_local

    control = _read_tab_as_row(config_tab)
    if not _is_mode_control_tab(config_tab):
        return control

    mode_value = str(control.get("Mode") or "").strip()
    if not mode_value:
        raise RuntimeError(f"{config_tab} config row is missing required Mode value.")
    selected_tab = _mode_to_tab_name(mode_value)
    if not selected_tab:
        raise RuntimeError(
            f"Unsupported Mode value {mode_value!r}; expected found footage, performance, or hybrid."
        )
    routed = _read_tab_as_row(selected_tab)
    merged: Dict[str, Any] = dict(control)
    for key, val in routed.items():
        if str(val or "").strip():
            merged[key] = val
        elif key not in merged:
            merged[key] = val
    merged["Mode"] = mode_value
    merged["_mode_source_tab"] = selected_tab
    return merged


def _manifest_fingerprint(manifest: Dict[str, Any]) -> str:
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _ensure_manifest_folder_id(name: str, parent_folder_id: str) -> str:
    client = drive_upload_from_env()
    existing = client.find_folder_by_name(name, parent_folder_id)
    if existing:
        return existing
    return client.create_folder(name, parent_folder_id)


def _upload_manifest(manifest: Dict[str, Any], *, naming_convention: str, output_parent_folder_id: str) -> str:
    fingerprint = _manifest_fingerprint(manifest)
    filename = f"manifest_{fingerprint[:12]}.json"
    folder_id = _ensure_manifest_folder_id(f"{naming_convention}__manifests", output_parent_folder_id)
    path = Path(tempfile.gettempdir()) / filename
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    client = drive_upload_from_env()
    try:
        return client.upload_file(str(path), folder_id, "application/json")
    except DriveUploadError:
        raise
    except Exception as exc:
        raise DriveUploadError(f"manifest upload failed: {exc}") from exc


def _render_job_id(config_sheet_id: str, config_tab: str, row_index: int = 2) -> str:
    seed = f"{config_sheet_id}:{config_tab}:{row_index}".encode("utf-8")
    return f"cfg_{hashlib.sha256(seed).hexdigest()[:12]}"


def _render_job_id_for_iteration(base_job_id: str, iteration: int, total_runs: int) -> str:
    if total_runs <= 1:
        return base_job_id
    return f"{base_job_id}_r{iteration:02d}"


def _required_run_count(number_of_videos: int, clip_count: int) -> int:
    if number_of_videos <= 0:
        raise RuntimeError(f"Cannot compute run count: Number of Videos must be >= 1 (got {number_of_videos})")
    if clip_count <= 0:
        raise RuntimeError("Cannot compute run count: enabled clip count is zero")
    return max(1, math.ceil(number_of_videos / clip_count))


def _collection_pool_for_run(collection_pools: list[Mapping[str, Any]], run_index: int) -> Mapping[str, Any]:
    if not collection_pools:
        raise RuntimeError("No collection pools available")
    if run_index < 0:
        raise ValueError(f"run_index must be >= 0, got {run_index}")
    return collection_pools[run_index % len(collection_pools)]


def _select_random_collection_pools_for_cuts(
    collection_pools: list[Mapping[str, Any]],
    *,
    cut_count: int,
    rng: random.Random,
    previous_collection_num: str = "",
) -> tuple[list[Mapping[str, Any]], str]:
    if not collection_pools:
        raise RuntimeError("No collection pools available")
    if cut_count <= 0:
        return [], str(previous_collection_num or "")

    previous = str(previous_collection_num or "")
    unique_nums = {str(pool.get("collection_num") or "").strip() for pool in collection_pools}
    unique_nums.discard("")
    if len(unique_nums) <= 1 and cut_count > 1:
        raise RuntimeError(
            "At least two 'collection-<number>' folders are required for non-consecutive per-clip selection."
        )

    out: list[Mapping[str, Any]] = []
    for _ in range(cut_count):
        candidates = [pool for pool in collection_pools if str(pool.get("collection_num") or "").strip() != previous]
        if not candidates:
            candidates = list(collection_pools)
        picked = candidates[int(rng.randrange(0, len(candidates)))]
        out.append(picked)
        previous = str(picked.get("collection_num") or "").strip()
    return out, previous


def _normalize_text_preset_name(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.endswith(".py"):
        text = text[:-3]
    prefix = "text_preset_"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    return text


def _resolve_text_preset_override_from_cfg(cfg: Mapping[str, Any]) -> tuple[str, str]:
    env_raw = str(os.environ.get("TEXT_PRESET_OVERRIDE", "") or "").strip()
    env_norm = _normalize_text_preset_name(env_raw)
    if env_norm:
        return env_norm, "env:TEXT_PRESET_OVERRIDE"
    cfg_raw = str(_cfg_value_by_names(cfg, TEXT_PRESET_FIELD_NAMES, default="") or "").strip()
    cfg_norm = _normalize_text_preset_name(cfg_raw)
    if cfg_norm:
        return cfg_norm, "config:lyric_text_preset_override"
    return "", ""


def _apply_text_preset_override(
    *,
    dynamic_preset_rows: list[dict[str, str]],
    preset: dict[str, Any],
    text_preset_override: str,
    source: str,
) -> None:
    override = str(text_preset_override or "").strip()
    if not override:
        return
    preset["text_preset"] = override
    for row in dynamic_preset_rows:
        row["text_preset"] = override
    print(
        f"text_preset_override_applied source={source or 'unknown'} "
        f"text_preset={override} rows_updated={len(dynamic_preset_rows)}"
    )


def _parse_performance_clip_map(
    cfg: Mapping[str, Any],
    *,
    drive_client: Any | None = None,
    source_folder_id: str = "",
    listing_cache: dict[str, list[dict[str, str]]] | None = None,
    auto_fill: bool | None = None,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for label in ("A", "B", "C", "D", "E", "F"):
        key = f"Performance Clip {label} File ID"
        alt = f"performance_clip_{label.lower()}_drive_file_id"
        raw = str(_cfg_value_by_names(cfg, [key, alt], default="") or "").strip()
        if not raw:
            continue
        out[label.lower()] = _resolve_performance_drive_file_ref(
            value=raw,
            label=key,
            drive_client=drive_client,
            source_folder_id=source_folder_id,
            listing_cache=listing_cache,
        )
    if out:
        return out
    if auto_fill is None:
        auto_raw = str(os.environ.get("PERFORMANCE_PRESET_AUTO_CLIPS", "0") or "0").strip().lower()
        auto_fill = auto_raw not in {"0", "false", "no", "off"}
    if not auto_fill:
        return out
    if drive_client is None or listing_cache is None or not str(source_folder_id or "").strip():
        return out
    rows = _list_folder_files_cached(
        drive_client=drive_client,
        folder_id=source_folder_id,
        listing_cache=listing_cache,
    )
    video_rows = [
        row
        for row in rows
        if str(row.get("id") or "").strip()
        and _is_video_like(str(row.get("name") or ""), str(row.get("mimeType") or ""))
    ]
    video_rows.sort(key=lambda row: (str(row.get("name") or "").lower(), str(row.get("id") or "")))
    for idx, row in enumerate(video_rows[:6]):
        out[chr(ord("a") + idx)] = str(row.get("id") or "").strip()
    if len(out) >= 2:
        print(
            "performance_clip_map_auto_resolved "
            f"source_folder_id={source_folder_id} "
            f"labels={','.join(sorted(out.keys()))}"
        )
    else:
        out = {}
    return out


def _resolve_performance_master_clip_id(
    cfg: Mapping[str, Any],
    *,
    drive_client: Any | None = None,
    source_folder_id: str = "",
    listing_cache: dict[str, list[dict[str, str]]] | None = None,
) -> str:
    key = "Performance Master Clip File ID"
    raw = str(_cfg_value_by_names(cfg, [key, "performance_master_clip_drive_file_id"], default="") or "").strip()
    if not raw:
        raw = str(os.environ.get("PERFORMANCE_MASTER_CLIP_FILE_ID", "") or "").strip()
    if not raw:
        # Operator-friendly fallback: pick first deterministic video file from source folder
        # when explicit master clip is omitted.
        if drive_client is not None and listing_cache is not None and str(source_folder_id or "").strip():
            rows = _list_folder_files_cached(
                drive_client=drive_client,
                folder_id=source_folder_id,
                listing_cache=listing_cache,
            )
            video_rows = [
                r
                for r in rows
                if str(r.get("id") or "").strip()
                and _is_video_like(str(r.get("name") or ""), str(r.get("mimeType") or ""))
            ]
            video_rows.sort(key=lambda r: (str(r.get("name") or "").lower(), str(r.get("id") or "")))
            if video_rows:
                chosen = str(video_rows[0].get("id") or "").strip()
                if chosen:
                    print(
                        "performance_master_clip_auto_resolved "
                        f"source_folder_id={source_folder_id} "
                        f"file_id={chosen} name={str(video_rows[0].get('name') or '')!r}"
                    )
                    return chosen
        raise RuntimeError(
            f"Missing required config field for simple performance mode: {key} "
            f"(and no video files found in performance source folder {source_folder_id})."
        )
    return _resolve_performance_drive_file_ref(
        value=raw,
        label=key,
        drive_client=drive_client,
        source_folder_id=source_folder_id,
        listing_cache=listing_cache,
    )


def _read_preset_rows(gateway: SheetsGateway, preset_sheet_id: str, tab: str = "Sheet1") -> list[dict[str, str]]:
    rng = f"{_quote_tab(tab)}!A1:Z200"
    data = gateway.read(preset_sheet_id, [rng])
    values = _extract_first_values_matrix(data)
    if len(values) < 2:
        return []
    rows: list[dict[str, str]] = []
    last_lyric = ""
    last_performance = ""
    for raw in values[1:]:
        if not any(str(cell or "").strip() for cell in raw):
            continue
        ff = str(raw[0]).strip() if len(raw) > 0 else ""
        performance = str(raw[1]).strip() if len(raw) > 1 else ""
        lyric = str(raw[2]).strip() if len(raw) > 2 else ""
        lyric_norm = _normalize_text_preset_name(lyric)
        if lyric_norm:
            last_lyric = lyric_norm
        elif last_lyric:
            lyric_norm = last_lyric
        if performance:
            last_performance = performance
        elif last_performance:
            performance = last_performance
        if not ff and not lyric and not performance:
            continue
        rows.append(
            {
                "stitch_preset": ff,
                "text_preset": lyric_norm,
                "performance_preset": performance,
            }
        )
    return rows


def _performance_preset_autofill_enabled() -> bool:
    raw = str(os.environ.get("PERFORMANCE_PRESET_AUTOFILL", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _a1_col_from_index(idx: int) -> str:
    if idx < 0:
        raise ValueError(f"Column index must be >= 0, got {idx}")
    out = ""
    value = int(idx) + 1
    while value > 0:
        value, rem = divmod(value - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _autofill_performance_presets_column(
    gateway: SheetsGateway,
    preset_sheet_id: str,
    *,
    tab: str,
    fill_value: str,
) -> int:
    value = str(fill_value or "").strip()
    if not value:
        return 0
    value = normalize_performance_preset(value)
    rng = f"{_quote_tab(tab)}!A1:Z200"
    data = gateway.read(preset_sheet_id, [rng])
    rows = _extract_first_values_matrix(data)
    if len(rows) < 2:
        return 0

    header = rows[0]
    idx = _header_map(header)
    perf_idx = idx.get("performance presets")
    if perf_idx is None:
        perf_idx = idx.get("performance preset")
    if perf_idx is None:
        perf_idx = idx.get("performance_preset")
    if perf_idx is None:
        return 0

    ff_idx = idx.get("ff presets")
    if ff_idx is None:
        ff_idx = idx.get("found footage presets")
    if ff_idx is None:
        ff_idx = 0

    data_rows = rows[1:]
    has_any_performance = any(str(_cell(r, perf_idx)).strip() for r in data_rows)
    if has_any_performance:
        return 0

    updates: list[dict[str, Any]] = []
    perf_col = _a1_col_from_index(perf_idx)
    for row_num, row in enumerate(data_rows, start=2):
        ff_value = str(_cell(row, ff_idx)).strip()
        perf_value = str(_cell(row, perf_idx)).strip()
        if not ff_value or perf_value:
            continue
        updates.append(
            {
                "range": f"{_quote_tab(tab)}!{perf_col}{row_num}",
                "values": [[value]],
            }
        )
    if not updates:
        return 0
    gateway.write(preset_sheet_id, updates)
    return len(updates)


def main() -> int:
    control_sheet_id = os.environ.get("CONTROL_SHEET_ID", "").strip()
    if not control_sheet_id:
        raise RuntimeError("Missing required env var: CONTROL_SHEET_ID")
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app").strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    gw = SheetsGateway(base_url=base_url, api_key=api_key)

    config_sheet_id = os.environ.get("CONFIG_SHEET_ID", CONFIG_SHEET_ID_DEFAULT).strip()
    config_tab = os.environ.get("CONFIG_TAB", CONFIG_TAB_DEFAULT).strip()
    config_range = os.environ.get("CONFIG_RANGE", CONFIG_RANGE_DEFAULT).strip()
    clips_library_folder_id = os.environ.get("CLIPS_LIBRARY_FOLDER_ID", CLIPS_LIBRARY_FOLDER_ID_DEFAULT).strip()
    drive_proxy_base = os.environ.get("DRIVE_PROXY_BASE_URL", "").strip()
    print(f"drive_proxy_base_url={drive_proxy_base or '(unset)'}")

    ensure_drive_upload_credentials()

    cfg = _read_config_row(gw, config_sheet_id, config_tab, config_range)
    output_parent_folder_id, output_parent_source, _ = _resolve_output_parent_folder(cfg)
    assets_folder_id = os.environ.get("ASSETS_FOLDER_ID", "").strip() or output_parent_folder_id
    mode_value = str(cfg.get("Mode") or "").strip()
    source_tab = str(cfg.get("_mode_source_tab") or config_tab).strip() or config_tab
    print(f"config_mode={mode_value or '(unset)'} config_source_tab={source_tab}")
    print(
        f"output_parent_folder_id={output_parent_folder_id} "
        f"output_parent_source={output_parent_source}"
    )
    print("preflight_asset_ids:")
    preflight_fields = [
        ("Audio File", "Audio File ID", False),
        ("lyrics .srt", "lyrics .srt File ID", False),
        ("copy .srt", "copy. srt File ID", True),
    ]
    for chip_field, id_field, optional in preflight_fields:
        chip_val = str(cfg.get(chip_field) or "").strip()
        id_val = str(cfg.get(id_field) or "").strip()
        id_kind = drive_reference_kind(id_val)
        status = "OK" if id_kind == "id" or (optional and id_kind == "empty") else "REPLACE_WITH_DRIVE_ID"
        print(f"- {chip_field}: chip={chip_val!r} id={id_val!r} status={status}")
        if status == "REPLACE_WITH_DRIVE_ID":
            print(f"  Replace with Drive file id: {id_field}")

    naming_convention = str(cfg.get("Naming Convention") or "").strip()
    if not naming_convention:
        raise RuntimeError("Missing required config field: Naming Convention")
    song_title_overlay = resolve_song_title_overlay(cfg)
    raw_number_of_videos = str(cfg.get("Number of Videos") or "").strip()
    try:
        number_of_videos = int(raw_number_of_videos)
    except Exception as exc:
        raise RuntimeError(
            f"Invalid Number of Videos value {raw_number_of_videos!r}; expected integer >= 1."
        ) from exc
    if number_of_videos <= 0:
        raise RuntimeError(f"Number of Videos must be >= 1 (got {number_of_videos}).")
    exec_preset_name = str(cfg.get("Executive Preset") or "").strip()
    preset_map = EXEC_PRESET_MAP.get(exec_preset_name)
    if not preset_map:
        raise RuntimeError(
            f"Unknown Executive Preset '{exec_preset_name}'. Known presets={sorted(EXEC_PRESET_MAP.keys())}"
        )
    preset = dict(preset_map)
    render_mode = _resolve_render_mode_from_cfg(cfg)
    performance_mode = _resolve_performance_mode_from_cfg(cfg, render_mode=render_mode)
    performance_audio_policy = _resolve_performance_audio_policy_from_cfg(cfg, render_mode=render_mode)
    performance_source_folder_id = _resolve_performance_source_folder_id(cfg)
    performance_name_resolution = "enabled" if _performance_clip_name_resolution_enabled() else "disabled"
    print(
        f"performance_source_folder_id={performance_source_folder_id} "
        f"performance_clip_name_resolution={performance_name_resolution}"
    )
    performance_preset_fallback = str(
        _cfg_value_by_names(
            cfg,
            PERFORMANCE_PRESET_FIELD_NAMES,
            default="",
        )
        or ""
    ).strip()
    if performance_preset_fallback:
        performance_preset_fallback = normalize_performance_preset(performance_preset_fallback)

    audio_id = select_asset_drive_file_id(
        cfg,
        id_key="Audio File ID",
        chip_key="Audio File",
        label="Audio File",
        optional=False,
        assets_folder_id=assets_folder_id,
        clips_library_folder_id=clips_library_folder_id,
    )
    lyrics_id = select_asset_drive_file_id(
        cfg,
        id_key="lyrics .srt File ID",
        chip_key="lyrics .srt",
        label="lyrics .srt",
        optional=False,
        assets_folder_id=assets_folder_id,
        clips_library_folder_id=clips_library_folder_id,
    )
    copy_id = select_asset_drive_file_id(
        cfg,
        id_key="copy. srt File ID",
        chip_key="copy .srt",
        label="copy .srt",
        optional=True,
        assets_folder_id=assets_folder_id,
        clips_library_folder_id=clips_library_folder_id,
    )
    stitch_bpm = resolve_stitch_bpm(cfg)
    timeline_fps = int(preset["fps"])
    performance_aspect = str(os.environ.get("PERFORMANCE_ASPECT", "9:16") or "9:16").strip() or "9:16"
    performance_timing_mode = resolve_performance_preset_timing_mode()
    text_preset_override, text_preset_override_source = _resolve_text_preset_override_from_cfg(cfg)

    base_cuts = build_cuts_from_config(
        cfg,
        naming_convention=naming_convention,
        start_index=0,
        fps=timeline_fps,
    )
    clip_count = len(base_cuts)
    required_runs = _required_run_count(number_of_videos, clip_count)
    preset_sheet_id = str(cfg.get("Executive Preset File ID") or "").strip()
    dynamic_preset_rows: list[dict[str, str]] = []
    if preset_sheet_id and drive_reference_kind(preset_sheet_id) == "id":
        if render_mode == EDIT_MODE_PERFORMANCE and _performance_preset_autofill_enabled():
            autofill_value = performance_preset_fallback or str(preset.get("performance_preset") or "").strip()
            filled = _autofill_performance_presets_column(
                gw,
                preset_sheet_id,
                tab=os.environ.get("EXEC_PRESET_TAB", "Sheet1"),
                fill_value=autofill_value,
            )
            if filled > 0:
                print(
                    f"performance_presets_autofill sheet_id={preset_sheet_id} "
                    f"tab={os.environ.get('EXEC_PRESET_TAB', 'Sheet1')} cells_filled={filled}"
                )
        dynamic_preset_rows = _read_preset_rows(gw, preset_sheet_id, tab=os.environ.get("EXEC_PRESET_TAB", "Sheet1"))
    if not dynamic_preset_rows:
        dynamic_preset_rows = [
            {
                "stitch_preset": str(preset["stitch_preset"]),
                "text_preset": str(preset["text_preset"]),
                "performance_preset": performance_preset_fallback,
            }
        ]
    _apply_text_preset_override(
        dynamic_preset_rows=dynamic_preset_rows,
        preset=preset,
        text_preset_override=text_preset_override,
        source=text_preset_override_source,
    )
    if (
        render_mode == EDIT_MODE_PERFORMANCE
        and performance_mode == PERFORMANCE_MODE_SIMPLE
        and not _has_explicit_performance_mode(cfg)
    ):
        has_performance_preset = bool(performance_preset_fallback) or any(
            str(row.get("performance_preset") or "").strip() for row in dynamic_preset_rows
        )
        if has_performance_preset:
            performance_mode = PERFORMANCE_MODE_PRESET
            print(
                "performance_mode_auto_inferred "
                "mode=preset reason=performance_preset_detected "
                "source=executive_preset_sheet_or_config"
            )

    if render_mode == EDIT_MODE_PERFORMANCE:
        base_job_id = _render_job_id(config_sheet_id, config_tab, row_index=2)
        queued_job_ids: list[str] = []
        manifest_ids: list[str] = []
        rng = random.SystemRandom()
        drive_client = drive_upload_from_env()
        performance_listing_cache: dict[str, list[dict[str, str]]] = {}

        performance_master_clip_id = ""
        performance_clips: dict[str, str] = {}
        if performance_mode == PERFORMANCE_MODE_SIMPLE:
            performance_master_clip_id = _resolve_performance_master_clip_id(
                cfg,
                drive_client=drive_client,
                source_folder_id=performance_source_folder_id,
                listing_cache=performance_listing_cache,
            )
        else:
            auto_clip_fill = not _has_explicit_performance_clip_ids(cfg)
            performance_clips = _parse_performance_clip_map(
                cfg,
                drive_client=drive_client,
                source_folder_id=performance_source_folder_id,
                listing_cache=performance_listing_cache,
                auto_fill=auto_clip_fill,
            )
            if not (2 <= len(performance_clips) <= 6):
                if auto_clip_fill:
                    raise RuntimeError(
                        "Performance preset mode requires at least 2 video clips in "
                        f"performance source folder {performance_source_folder_id}."
                    )
                raise RuntimeError(
                    "Performance preset mode requires 2-6 clip ids via "
                    "Performance Clip A/B/C/D/E/F File ID fields."
                )

        performance_target_videos = min(number_of_videos, clip_count)
        if number_of_videos > clip_count:
            print(
                "performance_target_videos_clamped "
                f"requested={number_of_videos} enabled_cuts={clip_count} effective={performance_target_videos}"
            )
        required_runs = 1

        for i in range(required_runs):
            run_cuts = build_cuts_from_config(
                cfg,
                naming_convention=naming_convention,
                start_index=(i * clip_count) + 1,
                fps=timeline_fps,
            )
            remaining = max(0, performance_target_videos - (i * clip_count))
            if remaining > 0 and len(run_cuts) > remaining:
                run_cuts = run_cuts[:remaining]

            run_cuts_with_exec: list[dict[str, Any]] = []
            selected_performance_preset = ""
            for cut_idx, cut in enumerate(run_cuts):
                output_idx = (i * clip_count) + cut_idx
                cut_preset_row = dynamic_preset_rows[output_idx % len(dynamic_preset_rows)]
                cut_text_preset = str(cut_preset_row.get("text_preset") or preset["text_preset"]).strip()
                if not cut_text_preset:
                    cut_text_preset = str(preset["text_preset"])
                cut_seed = int(rng.randrange(1, 2_147_483_647))
                cut_row = dict(cut)
                cut_row.update(
                        {
                            "text_preset": cut_text_preset,
                            "seed": cut_seed,
                            "fps": int(preset["fps"]),
                            "aspect": performance_aspect,
                            "bg_mode": str(preset["bg_mode"]),
                        }
                    )
                if performance_mode == PERFORMANCE_MODE_PRESET:
                    cut_performance_preset = str(
                        cut_preset_row.get("performance_preset") or performance_preset_fallback or ""
                    ).strip()
                    if not cut_performance_preset:
                        raise RuntimeError(
                            "Performance preset mode requires a preset DSL value from "
                            "Executive Preset File column B or config field Performance Preset(s)."
                        )
                    cut_performance_preset = normalize_performance_preset(cut_performance_preset)
                    cut_row["performance_preset"] = cut_performance_preset
                    if not selected_performance_preset:
                        selected_performance_preset = cut_performance_preset
                run_cuts_with_exec.append(cut_row)

            manifest: Dict[str, Any] = {
                "schema_version": "pipeline_v1@2",
                "edit_mode": EDIT_MODE_PERFORMANCE,
                "campaign_id": naming_convention,
                "inputs": {
                    "audio_drive_file_id": audio_id,
                    "lyrics_srt_drive_file_id": lyrics_id,
                    "copy_srt_drive_file_id": copy_id or "",
                },
                "text": {
                    "preset": str(preset["text_preset"]),
                    "bg_mode": str(preset["bg_mode"]),
                    "fps": int(preset["fps"]),
                    "aspect": performance_aspect,
                    "song_title": {
                        "enabled": bool(song_title_overlay.get("enabled")),
                        "text": str(song_title_overlay.get("text") or ""),
                    },
                },
                "performance": {
                    "mode": performance_mode,
                    "audio_policy": performance_audio_policy or "master_only",
                    "master_clip_drive_file_id": performance_master_clip_id,
                    "clips": performance_clips,
                    "preset": selected_performance_preset if performance_mode == PERFORMANCE_MODE_PRESET else "",
                    "timing_mode": performance_timing_mode,
                    "bpm": stitch_bpm,
                    "source_folder_id": performance_source_folder_id,
                },
                "cuts": run_cuts_with_exec,
            }
            manifest_drive_file_id = _upload_manifest(
                manifest,
                naming_convention=naming_convention,
                output_parent_folder_id=output_parent_folder_id,
            )
            manifest_ids.append(manifest_drive_file_id)

            render_job_id = _render_job_id_for_iteration(base_job_id, i + 1, required_runs)
            existing = find_render_job_by_id(gw, control_sheet_id, render_job_id) or {}
            now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
            row = build_render_job_upsert_payload(
                existing,
                render_job_id=render_job_id,
                naming_convention=naming_convention,
                clips_library_folder_id=performance_source_folder_id,
                manifest_drive_file_id=manifest_drive_file_id,
                audio_id=audio_id,
                lyrics_id=lyrics_id,
                copy_id=copy_id,
                preset=preset,
                bpm=f"{stitch_bpm:g}",
                edit_mode=EDIT_MODE_PERFORMANCE,
                performance_mode=performance_mode,
                performance_preset=selected_performance_preset,
                output_parent_folder_id=output_parent_folder_id,
                now_iso=now_iso,
            )
            upsert_render_job(gw, control_sheet_id, row)
            queued_job_ids.append(render_job_id)
            print(
                f"queued_render_job_id={render_job_id} "
                f"manifest_drive_file_id={manifest_drive_file_id} "
                f"edit_mode={EDIT_MODE_PERFORMANCE} performance_mode={performance_mode} "
                f"performance_preset={selected_performance_preset}"
            )

        print(f"required_runs={required_runs}")
        print(f"clip_count={clip_count}")
        print(f"number_of_videos={number_of_videos}")
        print(f"cuts_generated={len(base_cuts)}")
        print(f"audio_drive_file_id={audio_id}")
        print(f"display_song_name={1 if song_title_overlay.get('enabled') else 0}")
        print(f"song_name={song_title_overlay.get('text') or ''}")
        print(f"lyrics_srt_drive_file_id={lyrics_id}")
        print(f"copy_srt_drive_file_id={copy_id or ''}")
        print(f"queued_jobs={json.dumps(queued_job_ids)}")
        print(f"manifest_ids={json.dumps(manifest_ids)}")
        return 0

    drive_client = drive_upload_from_env()
    clips_source_mode = _clips_source_mode()
    mirror_bucket = str(os.environ.get("CLIPS_LIBRARY_MIRROR_BUCKET", "")).strip()
    mirror_prefix = str(os.environ.get("CLIPS_LIBRARY_MIRROR_PREFIX", "library-mirror")).strip().strip("/") or "library-mirror"
    try:
        clips_tree_depth = int(str(os.environ.get("CLIPS_LIBRARY_MAX_DEPTH", "4")).strip())
    except Exception:
        clips_tree_depth = 4
    collection_pools: list[dict[str, Any]] = []
    mirror_index_object = ""
    mirror_meta: dict[str, Any] = {}
    if clips_source_mode == "gcs_mirror":
        collection_pools, mirror_index_object, mirror_meta = _load_collection_pools_from_gcs_mirror(
            bucket=mirror_bucket,
            prefix=mirror_prefix,
        )
    else:
        collection_folders = resolve_collection_folder_ids(
            cfg,
            drive_client=drive_client,
            root_folder_id=clips_library_folder_id,
        )
        if not collection_folders:
            raise RuntimeError("No collection folders found. Add direct subfolders named `collection-<number>`.")
        for row in collection_folders:
            folder_id = str(row.get("folder_id") or "").strip()
            collection_num = str(row.get("collection_num") or "")
            files = collect_files_from_folder_tree(
                drive_client,
                folder_id,
                max_depth=clips_tree_depth,
            )
            video_files = [
                f
                for f in files
                if str(f.get("id") or "").strip() and _is_video_like(str(f.get("name") or ""), str(f.get("mimeType") or ""))
            ]
            if not video_files:
                raise RuntimeError(
                    f"collection-{collection_num} resolved to folder {folder_id}, but no video clips were found."
                )
            collection_pools.append(
                {
                    "collection_num": collection_num,
                    "folder_id": folder_id,
                    "files": files,
                }
            )
    print(
        f"clips_source_mode={clips_source_mode} "
        f"mirror_bucket={mirror_bucket or '(unset)'} "
        f"mirror_prefix={mirror_prefix or '(unset)'} "
        f"mirror_index_object={mirror_index_object or '(none)'}"
    )
    if clips_source_mode == "gcs_mirror":
        generated_utc = str(mirror_meta.get("generated_utc") or "").strip()
        index_total_clips = int(mirror_meta.get("total_clips") or 0)
        mirror_pool_counts = {
            str(pool.get("collection_num") or ""): len(
                [
                    f
                    for f in list(pool.get("files") or [])
                    if str(f.get("id") or "").strip()
                    and _is_video_like(str(f.get("name") or ""), str(f.get("mimeType") or ""))
                ]
            )
            for pool in collection_pools
        }
        print(
            f"mirror_inventory_counts index_total_clips={index_total_clips} "
            f"resolved_pool_total={sum(int(v) for v in mirror_pool_counts.values())} "
            f"by_collection={json.dumps(mirror_pool_counts, sort_keys=True)}"
        )
        stale_warn_hours = _mirror_warn_stale_hours()
        if generated_utc:
            try:
                generated_dt = dt.datetime.fromisoformat(generated_utc.replace("Z", "+00:00"))
                if generated_dt.tzinfo is None:
                    generated_dt = generated_dt.replace(tzinfo=dt.timezone.utc)
                age_h = max(0.0, (dt.datetime.now(dt.timezone.utc) - generated_dt.astimezone(dt.timezone.utc)).total_seconds() / 3600.0)
                print(
                    f"mirror_freshness index_object=gs://{mirror_bucket}/{mirror_index_object} "
                    f"generated_utc={generated_utc} age_hours={age_h:.2f} warn_hours={stale_warn_hours:.2f}"
                )
                if stale_warn_hours > 0.0 and age_h > stale_warn_hours:
                    print(
                        f"mirror_freshness_warn index_object=gs://{mirror_bucket}/{mirror_index_object} "
                        f"age_hours={age_h:.2f} threshold_hours={stale_warn_hours:.2f}"
                    )
            except Exception:
                print(
                    f"mirror_freshness_warn index_object=gs://{mirror_bucket}/{mirror_index_object} "
                    f"reason=invalid_generated_utc value={generated_utc!r}"
                )
    clip_count = len(base_cuts)
    required_runs = _required_run_count(number_of_videos, clip_count)
    preset_sheet_id = str(cfg.get("Executive Preset File ID") or "").strip()
    dynamic_preset_rows: list[dict[str, str]] = []
    if preset_sheet_id and drive_reference_kind(preset_sheet_id) == "id":
        if render_mode == EDIT_MODE_PERFORMANCE and _performance_preset_autofill_enabled():
            autofill_value = performance_preset_fallback or str(preset.get("performance_preset") or "").strip()
            filled = _autofill_performance_presets_column(
                gw,
                preset_sheet_id,
                tab=os.environ.get("EXEC_PRESET_TAB", "Sheet1"),
                fill_value=autofill_value,
            )
            if filled > 0:
                print(
                    f"performance_presets_autofill sheet_id={preset_sheet_id} "
                    f"tab={os.environ.get('EXEC_PRESET_TAB', 'Sheet1')} cells_filled={filled}"
                )
        dynamic_preset_rows = _read_preset_rows(gw, preset_sheet_id, tab=os.environ.get("EXEC_PRESET_TAB", "Sheet1"))
    if not dynamic_preset_rows:
        dynamic_preset_rows = [
            {
                "stitch_preset": str(preset["stitch_preset"]),
                "text_preset": str(preset["text_preset"]),
                "performance_preset": performance_preset_fallback,
            }
        ]
    _apply_text_preset_override(
        dynamic_preset_rows=dynamic_preset_rows,
        preset=preset,
        text_preset_override=text_preset_override,
        source=text_preset_override_source,
    )

    base_job_id = _render_job_id(config_sheet_id, config_tab, row_index=2)
    queued_job_ids: list[str] = []
    manifest_ids: list[str] = []
    rng = random.SystemRandom()
    previous_collection_num = ""
    for i in range(required_runs):
        preset_row = dynamic_preset_rows[i % len(dynamic_preset_rows)]
        effective_preset = dict(preset)
        if str(preset_row.get("stitch_preset") or "").strip():
            effective_preset["stitch_preset"] = str(preset_row["stitch_preset"]).strip()
        if str(preset_row.get("text_preset") or "").strip():
            effective_preset["text_preset"] = str(preset_row["text_preset"]).strip()
        # Stochastic per-run seed remains the fallback/default for job-level fields.
        run_seed = int(rng.randrange(1, 2_147_483_647))
        effective_preset["seed"] = run_seed
        run_cuts = build_cuts_from_config(
            cfg,
            naming_convention=naming_convention,
            start_index=(i * clip_count) + 1,
            fps=timeline_fps,
        )
        remaining = max(0, number_of_videos - (i * clip_count))
        if remaining > 0 and len(run_cuts) > remaining:
            run_cuts = run_cuts[:remaining]
        selected_pools, previous_collection_num = _select_random_collection_pools_for_cuts(
            collection_pools,
            cut_count=len(run_cuts),
            rng=rng,
            previous_collection_num=previous_collection_num,
        )
        run_collection_num = ",".join(str(pool.get("collection_num") or "") for pool in selected_pools)
        if clips_source_mode == "gcs_mirror":
            run_collection_folder_id = f"gs://{mirror_bucket}/{mirror_prefix}"
        else:
            run_collection_folder_id = clips_library_folder_id
        run_files: list[dict[str, str]] = []
        seen_run_files: set[tuple[str, str]] = set()
        for pool in selected_pools:
            selected_num = str(pool.get("collection_num") or "").strip()
            for item in list(pool.get("files") or []):
                file_id = str(item.get("id") or "").strip()
                key = (selected_num, file_id)
                if not file_id or key in seen_run_files:
                    continue
                seen_run_files.add(key)
                row = dict(item)
                row["collection_num"] = selected_num
                run_files.append(row)

        clips: list[dict[str, str]] = []
        clips = build_manifest_clips(
            run_files,
            seed=run_seed,
            max_clips=80,
        )
        run_cuts_with_exec: list[dict[str, Any]] = []
        for cut_idx, cut in enumerate(run_cuts):
            output_idx = (i * clip_count) + cut_idx
            cut_preset_row = dynamic_preset_rows[output_idx % len(dynamic_preset_rows)]
            cut_preset = dict(preset)
            if str(cut_preset_row.get("stitch_preset") or "").strip():
                cut_preset["stitch_preset"] = str(cut_preset_row["stitch_preset"]).strip()
            if str(cut_preset_row.get("text_preset") or "").strip():
                cut_preset["text_preset"] = str(cut_preset_row["text_preset"]).strip()
            # Per-cut seed ensures deterministic behavior per short while avoiding duplicates.
            cut_preset_seed = int(rng.randrange(1, 2_147_483_647))
            cut_preset["seed"] = cut_preset_seed
            cut_row = dict(cut)
            cut_row.update(
                {
                    "stitch_preset": str(cut_preset["stitch_preset"]),
                    "text_preset": str(cut_preset["text_preset"]),
                    "seed": int(cut_preset["seed"]),
                    "fps": int(cut_preset["fps"]),
                    "aspect": str(cut_preset["aspect"]),
                    "bg_mode": str(cut_preset["bg_mode"]),
                    "collection_num": str(selected_pools[cut_idx].get("collection_num") or ""),
                }
            )
            run_cuts_with_exec.append(cut_row)

        pool_counts_by_collection: dict[str, int] = {}
        for row in run_files:
            coll = str(row.get("collection_num") or "").strip()
            if not coll:
                continue
            pool_counts_by_collection[coll] = int(pool_counts_by_collection.get(coll, 0)) + 1
        manifest_counts_by_collection: dict[str, int] = {}
        for row in clips:
            coll = str(row.get("collection_num") or "").strip()
            if not coll:
                continue
            manifest_counts_by_collection[coll] = int(manifest_counts_by_collection.get(coll, 0)) + 1
        print(
            f"mirror_selection_metrics run_index={i+1}/{required_runs} "
            f"pool_clip_count={len(run_files)} manifest_clip_count={len(clips)} "
            f"pool_by_collection={json.dumps(pool_counts_by_collection, sort_keys=True)} "
            f"manifest_by_collection={json.dumps(manifest_counts_by_collection, sort_keys=True)}"
        )

        manifest_category_counts = _collection_category_counts(clips)
        missing_events: list[str] = []
        for cut_idx, cut_row in enumerate(run_cuts_with_exec, start=1):
            collection_num = str(cut_row.get("collection_num") or "").strip()
            stitch_preset = str(cut_row.get("stitch_preset") or "").strip()
            required_categories = _extract_stitch_categories(stitch_preset)
            available = set((manifest_category_counts.get(collection_num) or {}).keys())
            missing = [cat for cat in required_categories if cat not in available]
            if missing:
                missing_events.append(
                    f"cut={cut_idx} filename={cut_row.get('filename')} collection={collection_num} "
                    f"required={required_categories} available={sorted(list(available))} missing={missing}"
                )
        print(
            f"mirror_category_metrics run_index={i+1}/{required_runs} "
            f"missing_events={len(missing_events)} category_counts={json.dumps(manifest_category_counts, sort_keys=True)}"
        )
        if clips_source_mode == "gcs_mirror" and missing_events:
            raise RuntimeError(
                "Mirror category coverage check failed before queueing render job: "
                + " | ".join(missing_events)
            )

        if run_cuts_with_exec:
            first_cut = run_cuts_with_exec[0]
            effective_preset["stitch_preset"] = str(first_cut.get("stitch_preset") or effective_preset["stitch_preset"])
            effective_preset["text_preset"] = str(first_cut.get("text_preset") or effective_preset["text_preset"])
            try:
                effective_preset["seed"] = int(first_cut.get("seed") or effective_preset["seed"])
            except Exception:
                pass

        manifest: Dict[str, Any] = {
            "schema_version": "pipeline_v1@2",
            "edit_mode": EDIT_MODE_FOUND_FOOTAGE,
            "campaign_id": naming_convention,
            "inputs": {
                "clips_drive_folder_id": run_collection_folder_id,
                "clips": clips,
                "audio_drive_file_id": audio_id,
                "lyrics_srt_drive_file_id": lyrics_id,
                "copy_srt_drive_file_id": copy_id or "",
                "clips_source": {
                    "mode": clips_source_mode,
                    "mirror_bucket": mirror_bucket if clips_source_mode == "gcs_mirror" else "",
                    "mirror_prefix": mirror_prefix if clips_source_mode == "gcs_mirror" else "",
                    "mirror_index_object": mirror_index_object if clips_source_mode == "gcs_mirror" else "",
                },
            },
            "stitch": {
                "preset": effective_preset["stitch_preset"],
                "bpm": stitch_bpm,
                "seed": int(effective_preset["seed"]),
                "fps": int(effective_preset["fps"]),
                "aspect": str(effective_preset["aspect"]),
            },
            "text": {
                "preset": effective_preset["text_preset"],
                "bg_mode": effective_preset["bg_mode"],
                "fps": int(effective_preset["fps"]),
                "aspect": str(effective_preset["aspect"]),
                "song_title": {
                    "enabled": bool(song_title_overlay.get("enabled")),
                    "text": str(song_title_overlay.get("text") or ""),
                },
            },
            "performance": {
                "mode": "",
                "audio_policy": "",
                "master_clip_drive_file_id": "",
                "clips": {},
                "preset": "",
                "timing_mode": performance_timing_mode,
                "bpm": stitch_bpm,
                "source_folder_id": "",
            },
            "cuts": run_cuts_with_exec,
        }
        manifest_drive_file_id = _upload_manifest(
            manifest,
            naming_convention=naming_convention,
            output_parent_folder_id=output_parent_folder_id,
        )
        manifest_ids.append(manifest_drive_file_id)

        render_job_id = _render_job_id_for_iteration(base_job_id, i + 1, required_runs)
        existing = find_render_job_by_id(gw, control_sheet_id, render_job_id) or {}
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        row = build_render_job_upsert_payload(
            existing,
            render_job_id=render_job_id,
            naming_convention=naming_convention,
            clips_library_folder_id=clips_library_folder_id,
            manifest_drive_file_id=manifest_drive_file_id,
            audio_id=audio_id,
            lyrics_id=lyrics_id,
            copy_id=copy_id,
            preset=effective_preset,
            bpm=f"{stitch_bpm:g}",
            edit_mode=EDIT_MODE_FOUND_FOOTAGE,
            performance_mode="",
            performance_preset="",
            output_parent_folder_id=output_parent_folder_id,
            now_iso=now_iso,
        )
        upsert_render_job(gw, control_sheet_id, row)
        queued_job_ids.append(render_job_id)
        print(
            f"queued_render_job_id={render_job_id} "
            f"manifest_drive_file_id={manifest_drive_file_id} "
            f"stitch_preset={effective_preset['stitch_preset']} "
            f"bpm={stitch_bpm:g} "
            f"text_preset={effective_preset['text_preset']} "
            f"seed={effective_preset['seed']} "
            f"collection={run_collection_num} "
            f"clips_drive_folder_id={run_collection_folder_id}"
        )

    print(f"required_runs={required_runs}")
    print(f"clip_count={clip_count}")
    print(f"number_of_videos={number_of_videos}")
    print(f"cuts_generated={len(base_cuts)}")
    print(f"audio_drive_file_id={audio_id}")
    print(f"stitch_bpm={stitch_bpm:g}")
    print(f"display_song_name={1 if song_title_overlay.get('enabled') else 0}")
    print(f"song_name={song_title_overlay.get('text') or ''}")
    print(f"lyrics_srt_drive_file_id={lyrics_id}")
    print(f"copy_srt_drive_file_id={copy_id or ''}")
    print(f"queued_jobs={json.dumps(queued_job_ids)}")
    print(f"manifest_ids={json.dumps(manifest_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

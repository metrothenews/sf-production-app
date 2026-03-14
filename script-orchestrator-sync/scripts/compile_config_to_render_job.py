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

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.drive_upload import DriveUploadError, from_env as drive_upload_from_env  # noqa: E402
from so_sync.gateway import SheetsGateway  # noqa: E402
from so_sync.render_repo import find_render_job_by_id, upsert_render_job  # noqa: E402

CONFIG_SHEET_ID_DEFAULT = "YOUR_GOOGLE_SHEET_ID"
CONFIG_TAB_DEFAULT = "Sheet1"
CONFIG_RANGE_DEFAULT = "A1:AZ2"
CLIPS_LIBRARY_FOLDER_ID_DEFAULT = "YOUR_GOOGLE_SHEET_ID"
OUTPUT_PARENT_FOLDER_ID_DEFAULT = "YOUR_GOOGLE_SHEET_ID"
DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
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
        "fps": 30,
        "aspect": "9:16",
        "bg_mode": "none",
        "seed": 42,
    }
}


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


def parse_hhmmssff_to_ms(value: str) -> int:
    m = re.match(r"^\s*(\d{2}):(\d{2}):(\d{2}):(\d{2})\s*$", str(value or ""))
    if not m:
        raise ValueError(f"Invalid HH:MM:SS:FF value: {value!r}")
    hh, mm, ss, ff = [int(x) for x in m.groups()]
    if ff > 99:
        raise ValueError(f"Invalid FF value (must be 00-99): {value!r}")
    total_ms = ((hh * 3600 + mm * 60 + ss) * 1000) + (ff * 10)
    return total_ms


def parse_hhmmssff_to_ffmpeg_ts(value: str) -> str:
    total_ms = parse_hhmmssff_to_ms(value)
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
        start_ms = parse_hhmmssff_to_ms(start)
        end_ms = parse_hhmmssff_to_ms(end)
        if end_ms <= start_ms:
            raise ValueError(f"Clip {slot} has end <= start: {start} -> {end}")
        cuts.append(
            {
                "id": f"clip_{slot}",
                "start": parse_hhmmssff_to_ffmpeg_ts(start),
                "end": parse_hhmmssff_to_ffmpeg_ts(end),
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


def _enabled_collection_numbers(cfg: Mapping[str, Any]) -> list[int]:
    out: list[int] = []
    for key, raw in cfg.items():
        m = re.match(r"^V Collection\s+(\d+)$", str(key or "").strip(), flags=re.IGNORECASE)
        if not m:
            continue
        if not normalize_bool(raw):
            continue
        num = int(m.group(1))
        if num not in out:
            out.append(num)
    out.sort()
    return out


def _normalized_collection_folder_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").strip().lower())


def _collection_label_aliases(collection_num: int) -> list[str]:
    n = int(collection_num)
    return [
        _normalized_collection_folder_name(f"V Collection {n}"),
        _normalized_collection_folder_name(f"Collection {n}"),
        _normalized_collection_folder_name(f"VCollection{n}"),
        _normalized_collection_folder_name(f"Collection{n}"),
    ]


def _read_collection_folder_id_from_cfg(cfg: Mapping[str, Any], collection_num: int) -> str:
    n = int(collection_num)
    candidates = [
        f"V Collection {n} Folder ID",
        f"V Collection {n} Drive Folder ID",
        f"V Collection {n} ID",
        f"Collection {n} Folder ID",
        f"Collection {n} Drive Folder ID",
        f"Collection {n} ID",
    ]
    for key in candidates:
        value = str(cfg.get(key) or "").strip()
        if drive_reference_kind(value) == "id":
            return value
    return ""


def resolve_collection_folder_ids(
    cfg: Mapping[str, Any],
    *,
    drive_client: Any,
    root_folder_id: str,
) -> list[dict[str, str]]:
    enabled = _enabled_collection_numbers(cfg)
    if not enabled:
        return []

    direct_children = drive_client.list_files_in_folder(root_folder_id)
    folder_rows = [row for row in direct_children if str(row.get("mimeType") or "").strip() == DRIVE_FOLDER_MIME]
    by_norm_name: Dict[str, str] = {}
    for row in folder_rows:
        fid = str(row.get("id") or "").strip()
        if not fid:
            continue
        norm_name = _normalized_collection_folder_name(str(row.get("name") or ""))
        if norm_name and norm_name not in by_norm_name:
            by_norm_name[norm_name] = fid

    resolved: list[dict[str, str]] = []
    for num in enabled:
        explicit_id = _read_collection_folder_id_from_cfg(cfg, num)
        if explicit_id:
            resolved.append({"collection_num": str(num), "folder_id": explicit_id})
            continue
        found_id = ""
        for alias in _collection_label_aliases(num):
            hit = by_norm_name.get(alias, "")
            if hit:
                found_id = hit
                break
        if not found_id and num == 1 and len(folder_rows) == 1:
            found_id = str(folder_rows[0].get("id") or "").strip()
        if not found_id:
            available = sorted(str(row.get("name") or "").strip() for row in folder_rows if str(row.get("name") or "").strip())
            raise RuntimeError(
                f"Unable to resolve folder for V Collection {num}. "
                f"Expected a direct subfolder under clips root ({root_folder_id}) named like "
                f"'V Collection {num}' or 'Collection {num}', or set an explicit folder id column. "
                f"available_subfolders={available}"
            )
        resolved.append({"collection_num": str(num), "folder_id": found_id})
    return resolved


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
    rng = f"{_quote_tab(config_tab)}!{config_range}"
    data = gateway.read(config_sheet_id, [rng])
    values = _extract_first_values_matrix(data)
    if len(values) < 2:
        raise RuntimeError(f"Expected header row + data row from {config_sheet_id} {rng}")
    header = values[0]
    row = values[1]
    idx = _header_map(header)
    out: Dict[str, Any] = {}
    for h, i in idx.items():
        out[str(header[i]).strip()] = _cell(row, i)
    return out


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
        return 1
    if clip_count <= 0:
        raise RuntimeError("Cannot compute run count: enabled clip count is zero")
    return max(1, math.ceil(number_of_videos / clip_count))


def _collection_pool_for_run(collection_pools: list[Mapping[str, Any]], run_index: int) -> Mapping[str, Any]:
    if not collection_pools:
        raise RuntimeError("No collection pools available")
    if run_index < 0:
        raise ValueError(f"run_index must be >= 0, got {run_index}")
    return collection_pools[run_index % len(collection_pools)]


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


def _read_preset_rows(gateway: SheetsGateway, preset_sheet_id: str, tab: str = "Sheet1") -> list[dict[str, str]]:
    rng = f"{_quote_tab(tab)}!A1:Z200"
    data = gateway.read(preset_sheet_id, [rng])
    values = _extract_first_values_matrix(data)
    if len(values) < 2:
        return []
    rows: list[dict[str, str]] = []
    last_lyric = ""
    for raw in values[1:]:
        ff = str(raw[0]).strip() if len(raw) > 0 else ""
        lyric = str(raw[2]).strip() if len(raw) > 2 else ""
        lyric_norm = _normalize_text_preset_name(lyric)
        if lyric_norm:
            last_lyric = lyric_norm
        elif last_lyric:
            lyric_norm = last_lyric
        if not ff and not lyric:
            continue
        rows.append({"stitch_preset": ff, "text_preset": lyric_norm})
    return rows


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
    output_parent_folder_id = os.environ.get("OUTPUT_PARENT_FOLDER_ID", OUTPUT_PARENT_FOLDER_ID_DEFAULT).strip()
    assets_folder_id = os.environ.get("ASSETS_FOLDER_ID", "").strip() or output_parent_folder_id
    drive_proxy_base = os.environ.get("DRIVE_PROXY_BASE_URL", "").strip()
    print(f"drive_proxy_base_url={drive_proxy_base or '(unset)'}")

    ensure_drive_upload_credentials()

    cfg = _read_config_row(gw, config_sheet_id, config_tab, config_range)
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
    number_of_videos = int(str(cfg.get("Number of Videos") or "0").strip())
    exec_preset_name = str(cfg.get("Executive Preset") or "").strip()
    preset = EXEC_PRESET_MAP.get(exec_preset_name)
    if not preset:
        raise RuntimeError(
            f"Unknown Executive Preset '{exec_preset_name}'. Known presets={sorted(EXEC_PRESET_MAP.keys())}"
        )

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

    base_cuts = build_cuts_from_config(cfg, naming_convention=naming_convention, start_index=0)
    drive_client = drive_upload_from_env()
    try:
        clips_tree_depth = int(str(os.environ.get("CLIPS_LIBRARY_MAX_DEPTH", "4")).strip())
    except Exception:
        clips_tree_depth = 4
    collection_folders = resolve_collection_folder_ids(
        cfg,
        drive_client=drive_client,
        root_folder_id=clips_library_folder_id,
    )
    if not collection_folders:
        raise RuntimeError("No enabled video collections found. Set one or more `V Collection N` toggles to TRUE.")
    collection_pools: list[dict[str, Any]] = []
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
                f"V Collection {collection_num} resolved to folder {folder_id}, but no video clips were found."
            )
        collection_pools.append(
            {
                "collection_num": collection_num,
                "folder_id": folder_id,
                "files": files,
            }
        )
    clip_count = len(base_cuts)
    required_runs = _required_run_count(number_of_videos, clip_count)
    preset_sheet_id = str(cfg.get("Executive Preset File ID") or "").strip()
    dynamic_preset_rows: list[dict[str, str]] = []
    if preset_sheet_id and drive_reference_kind(preset_sheet_id) == "id":
        dynamic_preset_rows = _read_preset_rows(gw, preset_sheet_id, tab=os.environ.get("EXEC_PRESET_TAB", "Sheet1"))
    if not dynamic_preset_rows:
        dynamic_preset_rows = [{"stitch_preset": str(preset["stitch_preset"]), "text_preset": str(preset["text_preset"])}]

    base_job_id = _render_job_id(config_sheet_id, config_tab, row_index=2)
    queued_job_ids: list[str] = []
    manifest_ids: list[str] = []
    rng = random.SystemRandom()
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
        )
        remaining = max(0, number_of_videos - (i * clip_count))
        if remaining > 0 and len(run_cuts) > remaining:
            run_cuts = run_cuts[:remaining]
        collection_row = _collection_pool_for_run(collection_pools, i)
        run_collection_folder_id = str(collection_row.get("folder_id") or "").strip()
        run_collection_num = str(collection_row.get("collection_num") or "")
        clips: list[dict[str, str]] = []
        clips = build_manifest_clips(
            list(collection_row.get("files") or []),
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
                }
            )
            run_cuts_with_exec.append(cut_row)
        if run_cuts_with_exec:
            first_cut = run_cuts_with_exec[0]
            effective_preset["stitch_preset"] = str(first_cut.get("stitch_preset") or effective_preset["stitch_preset"])
            effective_preset["text_preset"] = str(first_cut.get("text_preset") or effective_preset["text_preset"])
            try:
                effective_preset["seed"] = int(first_cut.get("seed") or effective_preset["seed"])
            except Exception:
                pass

        manifest: Dict[str, Any] = {
            "schema_version": "pipeline_v1@1",
            "campaign_id": naming_convention,
            "inputs": {
                "clips_drive_folder_id": run_collection_folder_id,
                "clips": clips,
                "audio_drive_file_id": audio_id,
                "lyrics_srt_drive_file_id": lyrics_id,
                "copy_srt_drive_file_id": copy_id or "",
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
            clips_library_folder_id=run_collection_folder_id,
            manifest_drive_file_id=manifest_drive_file_id,
            audio_id=audio_id,
            lyrics_id=lyrics_id,
            copy_id=copy_id,
            preset=effective_preset,
            bpm=f"{stitch_bpm:g}",
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
    print(f"lyrics_srt_drive_file_id={lyrics_id}")
    print(f"copy_srt_drive_file_id={copy_id or ''}")
    print(f"queued_jobs={json.dumps(queued_job_ids)}")
    print(f"manifest_ids={json.dumps(manifest_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

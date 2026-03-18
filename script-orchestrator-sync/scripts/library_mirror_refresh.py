#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.drive_upload import DriveUploadError, from_env as drive_upload_from_env  # noqa: E402
from so_sync.gcs_storage import GcsStorageError, GcsStorageClient  # noqa: E402


CLIPS_LIBRARY_FOLDER_ID_DEFAULT = "YOUR_GOOGLE_SHEET_ID"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
COLLECTION_FOLDER_NAME_RE = re.compile(r"^collection-(\d+)$", flags=re.IGNORECASE)
_CATEGORY_TOKEN_RE = re.compile(r"[a-z]+")
_CATEGORY_KEYWORDS: dict[str, str] = {
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


def _is_video_like(name: str, mime_type: str) -> bool:
    mime = str(mime_type or "").strip().lower()
    if mime.startswith("video/"):
        return True
    lower_name = str(name or "").strip().lower()
    return lower_name.endswith(".mp4") or lower_name.endswith(".mov")


def _collection_num_from_folder_name(name: str) -> int | None:
    raw = str(name or "").strip()
    if not raw:
        return None
    m = COLLECTION_FOLDER_NAME_RE.match(raw)
    if not m:
        return None
    return int(m.group(1))


def _sanitize_object_component(value: str) -> str:
    text = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    safe = safe.strip("._")
    return safe or "item"


def _to_int(raw: Any) -> int:
    try:
        return int(str(raw).strip())
    except Exception:
        return 0


def _build_object_name(
    *,
    prefix: str,
    collection_num: str,
    category: str,
    drive_file_id: str,
    name: str,
) -> str:
    pre = str(prefix or "").strip().strip("/")
    collection = f"collection-{_sanitize_object_component(collection_num)}"
    cat = _sanitize_object_component(category.upper() if category else "_")
    file_name = _sanitize_object_component(name)
    return f"{pre}/clips/{collection}/{cat}/{drive_file_id}__{file_name}"


def _collect_files_from_folder_tree(
    drive_client: Any,
    root_folder_id: str,
    *,
    max_depth: int = 4,
) -> list[dict[str, str]]:
    root = str(root_folder_id or "").strip()
    if not root:
        return []
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
            if mime == DRIVE_FOLDER_MIME and depth < max_depth:
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


def _resolve_collection_folders(drive_client: Any, root_folder_id: str) -> list[dict[str, str]]:
    direct_children = drive_client.list_files_in_folder(root_folder_id)
    folder_rows = [row for row in direct_children if str(row.get("mimeType") or "").strip() == DRIVE_FOLDER_MIME]
    out: list[dict[str, str]] = []
    for row in folder_rows:
        fid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        num = _collection_num_from_folder_name(name)
        if num is None or not fid:
            continue
        out.append({"collection_num": str(num), "folder_id": fid})
    out.sort(key=lambda row: int(str(row.get("collection_num") or "0")))
    return out


def _ensure_unique_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in entries:
        collection_num = str(row.get("collection_num") or "").strip()
        drive_file_id = str(row.get("drive_file_id") or "").strip()
        key = (collection_num, drive_file_id)
        if not collection_num or not drive_file_id or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _parse_iso_utc(value: str) -> dt.datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty datetime")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _load_quarantine_manifest(gcs: GcsStorageClient, object_name: str) -> dict[str, Any]:
    obj = str(object_name or "").strip()
    if not obj:
        return {"schema_version": "library_mirror_quarantine@1", "entries": []}
    if not gcs.object_exists(obj):
        return {"schema_version": "library_mirror_quarantine@1", "entries": []}
    raw = gcs.download_text(obj)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return {"schema_version": "library_mirror_quarantine@1", "entries": []}
    entries = parsed.get("entries")
    if not isinstance(entries, list):
        parsed["entries"] = []
    return parsed


def _extract_drive_id_from_object_name(object_name: str) -> str:
    base = Path(str(object_name or "")).name
    if "__" not in base:
        return ""
    return str(base.split("__", 1)[0]).strip()


def _quarantine_object_name(
    *,
    prefix: str,
    generated_utc: dt.datetime,
    clip_prefix: str,
    active_object_name: str,
) -> str:
    pre = str(prefix or "").strip().strip("/")
    stamp = generated_utc.strftime("%Y%m%dT%H%M%SZ")
    active = str(active_object_name or "").strip()
    if active.startswith(clip_prefix):
        rel = active[len(clip_prefix) :]
    else:
        rel = active
    rel = rel.lstrip("/")
    return f"{pre}/quarantine/{stamp}/{rel}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Mirror a Drive clips library into GCS. "
            "Downloads new/changed clips to bucket, quarantines stale clips, and purges expired quarantine clips."
        )
    )
    ap.add_argument("--drive-folder-id", default=os.environ.get("CLIPS_LIBRARY_FOLDER_ID", CLIPS_LIBRARY_FOLDER_ID_DEFAULT))
    ap.add_argument("--bucket", default=os.environ.get("CLIPS_LIBRARY_MIRROR_BUCKET", ""))
    ap.add_argument("--prefix", default=os.environ.get("CLIPS_LIBRARY_MIRROR_PREFIX", "library-mirror"))
    ap.add_argument("--index-object", default=os.environ.get("CLIPS_LIBRARY_MIRROR_INDEX_OBJECT", ""))
    ap.add_argument("--quarantine-manifest-object", default=os.environ.get("CLIPS_LIBRARY_MIRROR_QUARANTINE_MANIFEST_OBJECT", ""))
    ap.add_argument(
        "--quarantine-ttl-days",
        type=int,
        default=int(str(os.environ.get("CLIPS_LIBRARY_MIRROR_QUARANTINE_TTL_DAYS", "7"))),
    )
    ap.add_argument("--max-depth", type=int, default=int(str(os.environ.get("CLIPS_LIBRARY_MAX_DEPTH", "4"))))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    drive_folder_id = str(args.drive_folder_id or "").strip()
    bucket = str(args.bucket or "").strip()
    prefix = str(args.prefix or "").strip().strip("/")
    max_depth = max(1, int(args.max_depth))
    quarantine_ttl_days = max(1, int(args.quarantine_ttl_days))
    if not drive_folder_id:
        raise RuntimeError("--drive-folder-id is required")
    if not bucket:
        raise RuntimeError("--bucket (or CLIPS_LIBRARY_MIRROR_BUCKET) is required")
    if not prefix:
        raise RuntimeError("--prefix is required")

    index_object = str(args.index_object or "").strip() or f"{prefix}/indexes/library_mirror_index.json"
    quarantine_manifest_object = (
        str(args.quarantine_manifest_object or "").strip() or f"{prefix}/indexes/quarantine_manifest.json"
    )
    clip_prefix = f"{prefix}/clips/"

    try:
        drive_client = drive_upload_from_env()
        gcs = GcsStorageClient.from_adc(bucket)
    except (DriveUploadError, GcsStorageError) as exc:
        raise RuntimeError(str(exc)) from exc

    collection_folders = _resolve_collection_folders(drive_client, drive_folder_id)
    if not collection_folders:
        raise RuntimeError(
            f"No collection folders found under Drive root {drive_folder_id}. "
            "Expected direct subfolders named collection-<number>."
        )

    desired_entries: list[dict[str, Any]] = []
    for row in collection_folders:
        collection_num = str(row.get("collection_num") or "").strip()
        folder_id = str(row.get("folder_id") or "").strip()
        files = _collect_files_from_folder_tree(drive_client, folder_id, max_depth=max_depth)
        for item in files:
            if not _is_video_like(str(item.get("name") or ""), str(item.get("mimeType") or "")):
                continue
            drive_file_id = str(item.get("id") or "").strip()
            if not drive_file_id:
                continue
            category = _normalize_category_key(item.get("category") or "")
            object_name = _build_object_name(
                prefix=prefix,
                collection_num=collection_num,
                category=category,
                drive_file_id=drive_file_id,
                name=str(item.get("name") or ""),
            )
            desired_entries.append(
                {
                    "drive_file_id": drive_file_id,
                    "name": str(item.get("name") or ""),
                    "mimeType": str(item.get("mimeType") or ""),
                    "size": _to_int(item.get("size")),
                    "category": category,
                    "collection_num": collection_num,
                    "gcs_object": object_name,
                    "gcs_uri": f"gs://{bucket}/{object_name}",
                }
            )

    desired_entries = _ensure_unique_entries(desired_entries)
    desired_by_object = {str(row["gcs_object"]): row for row in desired_entries}
    desired_object_names = set(desired_by_object.keys())

    active_rows = gcs.list_objects(prefix=clip_prefix)
    active_by_name = {str(row.get("name") or ""): row for row in active_rows if str(row.get("name") or "")}
    active_names = set(active_by_name.keys())

    quarantine_manifest = _load_quarantine_manifest(gcs, quarantine_manifest_object)
    quarantine_entries_raw = quarantine_manifest.get("entries") or []
    quarantine_entries: list[dict[str, Any]] = [dict(row) for row in quarantine_entries_raw if isinstance(row, dict)]
    quarantine_set = {
        str(row.get("quarantine_object") or "").strip()
        for row in quarantine_entries
        if str(row.get("quarantine_object") or "").strip()
    }
    now_utc = dt.datetime.now(dt.timezone.utc)

    to_upload: list[dict[str, Any]] = []
    for obj, row in desired_by_object.items():
        existing = active_by_name.get(obj)
        if existing is None:
            to_upload.append(row)
            continue
        drive_size = int(row.get("size") or 0)
        gcs_size = int(existing.get("size") or 0)
        if drive_size > 0 and gcs_size > 0 and drive_size != gcs_size:
            to_upload.append(row)

    to_quarantine = sorted(active_names - desired_object_names)
    purge_due: list[dict[str, Any]] = []
    for row in quarantine_entries:
        purge_after_raw = str(row.get("purge_after_utc") or "").strip()
        if not purge_after_raw:
            continue
        try:
            purge_after = _parse_iso_utc(purge_after_raw)
        except Exception:
            continue
        if purge_after <= now_utc:
            purge_due.append(row)
    uploaded = 0
    quarantined = 0
    purged = 0

    print(
        f"mirror_refresh_start drive_folder_id={drive_folder_id} bucket={bucket} prefix={prefix} "
        f"index_object={index_object} quarantine_manifest_object={quarantine_manifest_object} "
        f"ttl_days={quarantine_ttl_days} dry_run={int(args.dry_run)}"
    )
    print(
        f"mirror_refresh_diff desired={len(desired_entries)} active={len(active_names)} "
        f"quarantine={len(quarantine_set)} upload_needed={len(to_upload)} "
        f"quarantine_needed={len(to_quarantine)} purge_due={len(purge_due)}"
    )

    if not args.dry_run:
        for row in to_upload:
            drive_file_id = str(row.get("drive_file_id") or "")
            object_name = str(row.get("gcs_object") or "")
            suffix = Path(str(row.get("name") or "")).suffix or ".bin"
            tmp_path = Path(tempfile.gettempdir()) / f"mirror_{drive_file_id}{suffix}"
            drive_client.download_file(drive_file_id, str(tmp_path))
            try:
                gcs.upload_file(
                    str(tmp_path),
                    object_name,
                    content_type=str(row.get("mimeType") or "application/octet-stream"),
                    metadata={
                        "drive_file_id": drive_file_id,
                        "collection_num": str(row.get("collection_num") or ""),
                        "category": str(row.get("category") or ""),
                        "source_name": str(row.get("name") or ""),
                    },
                )
                uploaded += 1
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        new_quarantine_entries: list[dict[str, Any]] = []
        for active_name in to_quarantine:
            quarantine_object = _quarantine_object_name(
                prefix=prefix,
                generated_utc=now_utc,
                clip_prefix=clip_prefix,
                active_object_name=active_name,
            )
            gcs.copy_object(active_name, quarantine_object)
            gcs.delete_object(active_name)
            new_quarantine_entries.append(
                {
                    "active_object": active_name,
                    "quarantine_object": quarantine_object,
                    "drive_file_id": _extract_drive_id_from_object_name(active_name),
                    "quarantined_utc": now_utc.isoformat(),
                    "purge_after_utc": (now_utc + dt.timedelta(days=quarantine_ttl_days)).isoformat(),
                }
            )
            quarantined += 1
        print(f"mirror_refresh_quarantine moved={quarantined} skipped=0")

        all_quarantine_entries = quarantine_entries + new_quarantine_entries
        kept_quarantine_entries: list[dict[str, Any]] = []
        for row in all_quarantine_entries:
            quarantine_object = str(row.get("quarantine_object") or "").strip()
            if not quarantine_object:
                continue
            exists = gcs.object_exists(quarantine_object)
            if not exists:
                continue
            purge_after_raw = str(row.get("purge_after_utc") or "").strip()
            due = False
            if purge_after_raw:
                try:
                    due = _parse_iso_utc(purge_after_raw) <= now_utc
                except Exception:
                    due = False
            if due:
                gcs.delete_object(quarantine_object)
                purged += 1
                continue
            kept_quarantine_entries.append(row)
        print(f"mirror_refresh_purge purged={purged} kept={len(kept_quarantine_entries)}")

        quarantine_payload = {
            "schema_version": "library_mirror_quarantine@1",
            "generated_utc": now_utc.isoformat(),
            "bucket": bucket,
            "prefix": prefix,
            "entries": sorted(
                kept_quarantine_entries,
                key=lambda row: (
                    str(row.get("purge_after_utc") or ""),
                    str(row.get("quarantine_object") or ""),
                ),
            ),
        }
        gcs.upload_text(
            json.dumps(quarantine_payload, indent=2),
            quarantine_manifest_object,
            content_type="application/json",
            metadata={"generated_utc": now_utc.isoformat()},
        )
    else:
        print(f"mirror_refresh_quarantine moved={len(to_quarantine)} skipped=0")
        print(f"mirror_refresh_purge purged={len(purge_due)} kept={max(0, len(quarantine_entries) - len(purge_due))}")

    generated_utc = now_utc.isoformat()
    index_payload = {
        "schema_version": "library_mirror@1",
        "generated_utc": generated_utc,
        "drive_folder_id": drive_folder_id,
        "bucket": bucket,
        "prefix": prefix,
        "total_clips": len(desired_entries),
        "clips": sorted(
            desired_entries,
            key=lambda row: (
                int(str(row.get("collection_num") or "0")),
                str(row.get("category") or ""),
                str(row.get("name") or ""),
                str(row.get("drive_file_id") or ""),
            ),
        ),
    }

    if not args.dry_run:
        gcs.upload_text(
            json.dumps(index_payload, indent=2),
            index_object,
            content_type="application/json",
            metadata={"generated_utc": generated_utc, "drive_folder_id": drive_folder_id},
        )

    print(
        f"mirror_refresh_done clips={len(desired_entries)} uploaded={uploaded} "
        f"quarantined={quarantined if not args.dry_run else len(to_quarantine)} "
        f"purged={purged if not args.dry_run else len(purge_due)} "
        f"index_object={index_object} quarantine_manifest_object={quarantine_manifest_object} "
        f"dry_run={int(args.dry_run)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

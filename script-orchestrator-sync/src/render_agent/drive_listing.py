from __future__ import annotations

import json
from typing import Any

from so_sync.drive_upload import from_env as drive_upload_from_env

from .job_contract import parse_id_list


def list_folder_video_files(folder_id: str) -> list[dict[str, Any]]:
    client = drive_upload_from_env()
    rows = client.list_files_in_folder(folder_id, mime_prefix="video/")
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "mimeType": str(row.get("mimeType") or ""),
                "size": str(row.get("size") or ""),
            }
        )
    return out


def ensure_folder_readable(folder_id: str) -> None:
    client = drive_upload_from_env()
    client.service.files().get(fileId=folder_id, fields="id,name,mimeType").execute()


def normalize_sources(sources: dict[str, Any]) -> tuple[list[str], list[str]]:
    folder_ids = parse_id_list(sources.get("source_folder_ids", ""))
    file_ids = parse_id_list(sources.get("source_file_ids", ""))
    return folder_ids, file_ids


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def expand_source_pool(folder_ids: list[str], file_ids: list[str]) -> list[str]:
    pool: list[str] = _dedupe_preserve_order([str(f).strip() for f in file_ids if str(f).strip()])
    for folder_id in folder_ids:
        rows = list_folder_video_files(folder_id)
        ids = [str(r.get("id") or "").strip() for r in rows if str(r.get("id") or "").strip()]
        print(
            json.dumps(
                {
                    "event": "source_pool_folder_count",
                    "folder_id": folder_id,
                    "video_count": len(ids),
                },
                sort_keys=True,
            )
        )
        pool.extend(ids)
    return _dedupe_preserve_order(pool)

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from so_sync.drive_upload import DriveUploadError, from_env as drive_upload_from_env


DEFAULT_OUTPUT_FOLDER_ID = "YOUR_GOOGLE_SHEET_ID"


def _mime_for(path: str) -> str:
    if str(path).lower().endswith(".mp4"):
        return "video/mp4"
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def ensure_folder_accessible(folder_id: str) -> None:
    client = drive_upload_from_env()
    try:
        client.service.files().get(fileId=folder_id, fields="id,name").execute()
    except Exception as exc:
        raise DriveUploadError(f"Output folder not accessible folder_id={folder_id} error={exc}") from exc


def upload_file_to_folder(local_path: str, folder_id: str, name: str | None = None) -> dict[str, Any]:
    client = drive_upload_from_env()
    path = Path(local_path)
    if not path.exists():
        raise DriveUploadError(f"upload source missing: {local_path}")
    target_name = str(name or path.name)
    try:
        file_id = client.upload_file(str(path), folder_id, _mime_for(str(path)), name=target_name)
        metadata = client.get_file_metadata(file_id)
    except Exception as exc:
        raise DriveUploadError(f"upload failed local_path={local_path} folder_id={folder_id} error={exc}") from exc
    return {
        "file_id": str(metadata.get("id") or file_id),
        "name": str(metadata.get("name") or target_name),
        "webViewLink": "",
        "webContentLink": "",
    }

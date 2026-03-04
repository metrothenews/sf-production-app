from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from googleapiclient.http import MediaFileUpload

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
    body = {
        "name": name or path.name,
        "parents": [folder_id],
    }
    media = MediaFileUpload(str(path), mimetype=_mime_for(str(path)), resumable=False)
    try:
        resp = client.service.files().create(
            body=body,
            media_body=media,
            fields="id,name,webViewLink,webContentLink",
        ).execute()
    except Exception as exc:
        raise DriveUploadError(f"upload failed local_path={local_path} folder_id={folder_id} error={exc}") from exc
    return {
        "file_id": str(resp.get("id") or ""),
        "name": str(resp.get("name") or body["name"]),
        "webViewLink": str(resp.get("webViewLink") or ""),
        "webContentLink": str(resp.get("webContentLink") or ""),
    }

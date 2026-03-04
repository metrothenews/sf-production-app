"""Minimal Google Drive API client for list/create/upload primitives."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveUploadError(RuntimeError):
    pass


def _escape_query(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def _auth_hint(exc: Exception) -> DriveUploadError:
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if creds_path:
        creds_hint = f"GOOGLE_APPLICATION_CREDENTIALS is set (basename={Path(creds_path).name})."
    else:
        creds_hint = "GOOGLE_APPLICATION_CREDENTIALS is not set."
    return DriveUploadError(
        "Google Drive API call failed. "
        f"{creds_hint} "
        "Set GOOGLE_APPLICATION_CREDENTIALS to a Drive-enabled service account key JSON, "
        "or verify ADC with: gcloud auth application-default print-access-token. "
        f"original_error={exc}"
    )


def _build_drive_service() -> Any:
    try:
        import google.auth
        from googleapiclient.discovery import build
    except Exception as exc:
        raise DriveUploadError(
            "Google Drive API dependencies are missing; install google-auth and google-api-python-client."
        ) from exc
    try:
        creds, _ = google.auth.default(scopes=[DRIVE_SCOPE])
    except Exception as exc:
        raise DriveUploadError(
            "Google Drive auth failed. Set GOOGLE_APPLICATION_CREDENTIALS to a Drive-enabled service account key JSON."
        ) from exc
    return build("drive", "v3", credentials=creds, cache_discovery=False)


@dataclass(frozen=True)
class DriveUploadClient:
    service: Any

    @classmethod
    def from_adc(cls) -> "DriveUploadClient":
        return cls(service=_build_drive_service())

    def list_files_in_folder(self, parent_folder_id: str, mime_prefix: str = "") -> list[dict[str, str]]:
        if not parent_folder_id:
            raise DriveUploadError("parent_folder_id is required for list_files_in_folder")
        q = f"'{_escape_query(parent_folder_id)}' in parents and trashed = false"
        page_token = ""
        rows: list[dict[str, str]] = []
        while True:
            try:
                resp = (
                    self.service.files()
                    .list(
                        q=q,
                        fields="nextPageToken, files(id,name,mimeType,size)",
                        pageSize=1000,
                        pageToken=page_token or None,
                    )
                    .execute()
                )
            except Exception as exc:
                raise _auth_hint(exc) from exc
            items = resp.get("files") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                mime = str(item.get("mimeType") or "")
                if mime_prefix and not mime.startswith(mime_prefix):
                    continue
                rows.append(
                    {
                        "id": str(item.get("id") or ""),
                        "name": str(item.get("name") or ""),
                        "mimeType": mime,
                        "size": str(item.get("size") or ""),
                    }
                )
            page_token = str(resp.get("nextPageToken") or "")
            if not page_token:
                break
        return rows

    def find_folder_by_name(self, name: str, parent_folder_id: str) -> str:
        if not name or not parent_folder_id:
            return ""
        q = (
            f"'{_escape_query(parent_folder_id)}' in parents and "
            f"name = '{_escape_query(name)}' and "
            f"mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        try:
            resp = (
                self.service.files()
                .list(
                    q=q,
                    fields="files(id,name)",
                    pageSize=10,
                )
                .execute()
            )
        except Exception as exc:
            raise _auth_hint(exc) from exc
        rows = resp.get("files") or []
        if not rows:
            return ""
        return str(rows[0].get("id") or "")

    def create_folder(self, name: str, parent_folder_id: str) -> str:
        if not name or not parent_folder_id:
            raise DriveUploadError("name and parent_folder_id are required for create_folder")
        body = {
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_folder_id],
        }
        try:
            resp = self.service.files().create(body=body, fields="id").execute()
        except Exception as exc:
            raise _auth_hint(exc) from exc
        folder_id = str(resp.get("id") or "")
        if not folder_id:
            raise DriveUploadError(f"create_folder response missing id: {resp}")
        return folder_id

    def upload_file(self, local_path: str, parent_folder_id: str, mime_type: str = "application/octet-stream") -> str:
        path = Path(local_path)
        if not path.exists():
            raise DriveUploadError(f"upload source path does not exist: {local_path}")
        if not parent_folder_id:
            raise DriveUploadError("parent_folder_id is required for upload_file")
        try:
            from googleapiclient.http import MediaFileUpload
        except Exception as exc:
            raise DriveUploadError(
                "google-api-python-client is required for upload_file (MediaFileUpload missing)."
            ) from exc
        body = {
            "name": path.name,
            "parents": [parent_folder_id],
        }
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=False)
        try:
            resp = self.service.files().create(body=body, media_body=media, fields="id").execute()
        except Exception as exc:
            raise _auth_hint(exc) from exc
        drive_file_id = str(resp.get("id") or "")
        if not drive_file_id:
            raise DriveUploadError(f"upload response missing id: {resp}")
        return drive_file_id

    def download_file(self, drive_file_id: str, dst_path: str) -> str:
        if not drive_file_id:
            raise DriveUploadError("drive_file_id is required for download_file")
        path = Path(dst_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except Exception as exc:
            raise DriveUploadError(
                "google-api-python-client is required for download_file (MediaIoBaseDownload missing)."
            ) from exc
        request = self.service.files().get_media(fileId=drive_file_id)
        try:
            with path.open("wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
        except Exception as exc:
            raise DriveUploadError(
                f"{_auth_hint(exc)} drive_file_id={drive_file_id}"
            ) from exc
        return str(path)

    def delete_file(self, drive_file_id: str) -> None:
        if not drive_file_id:
            raise DriveUploadError("drive_file_id is required for delete_file")
        try:
            self.service.files().delete(fileId=drive_file_id).execute()
        except Exception as exc:
            raise _auth_hint(exc) from exc


def from_env() -> DriveUploadClient:
    return DriveUploadClient.from_adc()

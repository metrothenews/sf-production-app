"""Minimal Google Drive API client for list/create/upload primitives."""

from __future__ import annotations

import os
import socket
import ssl
import time
from dataclasses import dataclass
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any, Callable, TypeVar


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_DRIVE_API_RETRY_ATTEMPTS = 4
DEFAULT_DRIVE_API_RETRY_INITIAL_DELAY_SECONDS = 1.0
DEFAULT_DRIVE_API_RETRY_MAX_DELAY_SECONDS = 8.0
T = TypeVar("T")


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


def _to_int(raw: Any, default: int) -> int:
    try:
        return int(str(raw).strip())
    except Exception:
        return int(default)


def _to_float(raw: Any, default: float) -> float:
    try:
        return float(str(raw).strip())
    except Exception:
        return float(default)


def _drive_retry_attempts() -> int:
    return max(1, _to_int(os.environ.get("DRIVE_API_RETRY_ATTEMPTS", str(DEFAULT_DRIVE_API_RETRY_ATTEMPTS)), DEFAULT_DRIVE_API_RETRY_ATTEMPTS))


def _drive_retry_initial_delay_seconds() -> float:
    return max(0.0, _to_float(os.environ.get("DRIVE_API_RETRY_INITIAL_DELAY_SECONDS", str(DEFAULT_DRIVE_API_RETRY_INITIAL_DELAY_SECONDS)), DEFAULT_DRIVE_API_RETRY_INITIAL_DELAY_SECONDS))


def _drive_retry_max_delay_seconds() -> float:
    return max(0.0, _to_float(os.environ.get("DRIVE_API_RETRY_MAX_DELAY_SECONDS", str(DEFAULT_DRIVE_API_RETRY_MAX_DELAY_SECONDS)), DEFAULT_DRIVE_API_RETRY_MAX_DELAY_SECONDS))


def _http_status_code(exc: Exception) -> int:
    try:
        resp = getattr(exc, "resp", None)
        status = getattr(resp, "status", None)
        if status is None:
            return 0
        return int(status)
    except Exception:
        return 0


def _is_retryable_drive_exception(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout, ssl.SSLError, IncompleteRead, RemoteDisconnected)):
        return True
    if isinstance(exc, OSError):
        msg_os = str(exc).lower()
        if any(x in msg_os for x in ("timed out", "connection reset", "broken pipe", "temporarily unavailable", "eof")):
            return True
    status = _http_status_code(exc)
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    msg = str(exc).lower()
    if any(x in msg for x in ("timed out", "connection reset", "ssl", "eof occurred", "temporarily unavailable")):
        return True
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, Exception) and cause is not exc:
        return _is_retryable_drive_exception(cause)
    return False


def _call_with_drive_retries(fn: Callable[[], T], *, op_name: str) -> T:
    attempts = _drive_retry_attempts()
    delay = _drive_retry_initial_delay_seconds()
    max_delay = _drive_retry_max_delay_seconds()
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            retryable = _is_retryable_drive_exception(exc)
            if retryable and attempt < attempts:
                print(
                    "drive_api_retry "
                    f"op={op_name} attempt={attempt}/{attempts} "
                    f"next_delay_s={delay:.2f} error_type={type(exc).__name__} error={exc}"
                )
            if attempt >= attempts or not retryable:
                print(
                    "drive_api_fail "
                    f"op={op_name} attempt={attempt}/{attempts} "
                    f"retryable={str(retryable).lower()} error_type={type(exc).__name__} error={exc}"
                )
                raise
            if delay > 0.0:
                time.sleep(delay)
            delay = min(max_delay if max_delay > 0.0 else delay, max(0.0, delay * 2.0))
    assert last_exc is not None
    raise RuntimeError(f"Drive retry loop exhausted for op={op_name}: {last_exc}")


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
                resp = _call_with_drive_retries(
                    lambda: (
                        self.service.files()
                        .list(
                            q=q,
                            fields="nextPageToken, files(id,name,mimeType,size)",
                            pageSize=1000,
                            pageToken=page_token or None,
                        )
                        .execute()
                    )
                    ,
                    op_name="list_files_in_folder",
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
            resp = _call_with_drive_retries(
                lambda: (
                    self.service.files()
                    .list(
                        q=q,
                        fields="files(id,name)",
                        pageSize=10,
                    )
                    .execute()
                )
                ,
                op_name="find_folder_by_name",
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
            resp = _call_with_drive_retries(
                lambda: self.service.files().create(body=body, fields="id").execute(),
                op_name="create_folder",
            )
        except Exception as exc:
            raise _auth_hint(exc) from exc
        folder_id = str(resp.get("id") or "")
        if not folder_id:
            raise DriveUploadError(f"create_folder response missing id: {resp}")
        return folder_id

    def upload_file(
        self,
        local_path: str,
        parent_folder_id: str,
        mime_type: str = "application/octet-stream",
        name: str | None = None,
    ) -> str:
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
            "name": str(name or path.name),
            "parents": [parent_folder_id],
        }
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=False)
        try:
            resp = _call_with_drive_retries(
                lambda: self.service.files().create(body=body, media_body=media, fields="id").execute(),
                op_name="upload_file",
            )
        except Exception as exc:
            raise _auth_hint(exc) from exc
        drive_file_id = str(resp.get("id") or "")
        if not drive_file_id:
            raise DriveUploadError(f"upload response missing id: {resp}")
        return drive_file_id

    def get_file_metadata(self, drive_file_id: str) -> dict[str, Any]:
        if not drive_file_id:
            raise DriveUploadError("drive_file_id is required for get_file_metadata")
        try:
            resp = _call_with_drive_retries(
                lambda: (
                    self.service.files()
                    .get(fileId=drive_file_id, fields="id,size,name,parents,trashed")
                    .execute()
                ),
                op_name="get_file_metadata",
            )
        except Exception as exc:
            raise _auth_hint(exc) from exc

        parents_raw = resp.get("parents") or []
        parents = [str(p) for p in parents_raw if str(p or "").strip()]
        metadata = {
            "id": str(resp.get("id") or ""),
            "size": str(resp.get("size") or ""),
            "name": str(resp.get("name") or ""),
            "parents": parents,
            "trashed": bool(resp.get("trashed", False)),
        }
        if not metadata["id"]:
            raise DriveUploadError(f"get_file_metadata response missing id: {resp}")
        return metadata

    def confirm_upload_size(self, drive_file_id: str, expected_bytes: int) -> None:
        metadata = self.get_file_metadata(drive_file_id)
        if bool(metadata.get("trashed")):
            raise DriveUploadError(f"uploaded file is trashed: file_id={drive_file_id}")
        size_raw = str(metadata.get("size") or "").strip()
        if not size_raw:
            raise DriveUploadError(f"uploaded file missing size metadata: file_id={drive_file_id}")
        try:
            observed = int(size_raw)
        except Exception as exc:
            raise DriveUploadError(
                f"uploaded file has non-numeric size metadata: file_id={drive_file_id} size={size_raw!r}"
            ) from exc
        expected = int(expected_bytes)
        if observed != expected:
            raise DriveUploadError(
                f"uploaded file size mismatch: file_id={drive_file_id} expected_bytes={expected} observed_bytes={observed}"
            )

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
                    _, done = _call_with_drive_retries(
                        lambda: downloader.next_chunk(),
                        op_name="download_file_chunk",
                    )
        except Exception as exc:
            raise DriveUploadError(
                f"{_auth_hint(exc)} drive_file_id={drive_file_id}"
            ) from exc
        return str(path)

    def delete_file(self, drive_file_id: str) -> None:
        if not drive_file_id:
            raise DriveUploadError("drive_file_id is required for delete_file")
        try:
            _call_with_drive_retries(
                lambda: self.service.files().delete(fileId=drive_file_id).execute(),
                op_name="delete_file",
            )
        except Exception as exc:
            raise _auth_hint(exc) from exc


def from_env() -> DriveUploadClient:
    return DriveUploadClient.from_adc()

"""Drive proxy client used by render agent I/O paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import requests


class DriveProxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class DriveProxyClient:
    base_url: str
    api_key: str = ""

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def file_url(self, drive_file_id: str) -> str:
        if not drive_file_id:
            raise DriveProxyError("drive_file_id is required for file_url")
        base = self.base_url.rstrip("/")
        if base.endswith("/f"):
            return f"{base}/{drive_file_id}"
        return f"{base}/f/{drive_file_id}"

    def download_file(self, drive_file_id: str, dst_path: str) -> str:
        if not self.base_url:
            raise DriveProxyError("DRIVE_PROXY_BASE_URL is required for input_mode=drive")
        if not drive_file_id:
            raise DriveProxyError("drive_file_id is required for download_file")
        url = self.file_url(drive_file_id)
        r = requests.get(url, headers=self._headers(), timeout=120, stream=True)
        if r.status_code != 200:
            body = (r.text or "")[:200]
            api_key_sent = "yes" if bool(self.api_key) else "no"
            hint = ""
            if r.status_code == 404:
                hint = (
                    " 404 often means the drive-proxy service account lacks permission to the Drive file. "
                    "Share the file/folder with <SERVICE_ACCOUNT_EMAIL>."
                )
            raise DriveProxyError(
                f"download failed status={r.status_code} url={url} drive_file_id={drive_file_id} "
                f"api_key_sent={api_key_sent} body={body}{hint}"
            )
        path = Path(dst_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 64):
                if chunk:
                    f.write(chunk)
        return str(path)

    def upload_file(self, src_path: str, dst_drive_folder_id: str, filename: str) -> str:
        if not self.base_url:
            raise DriveProxyError("DRIVE_PROXY_BASE_URL is required for upload_file")
        if not dst_drive_folder_id:
            raise DriveProxyError("dst_drive_folder_id is required for upload_file")
        path = Path(src_path)
        if not path.exists():
            raise DriveProxyError(f"upload source path does not exist: {src_path}")
        with path.open("rb") as f:
            files = {"file": (filename or path.name, f, "application/octet-stream")}
            data = {"parent_drive_folder_id": dst_drive_folder_id}
            r = requests.post(
                f"{self.base_url.rstrip('/')}/v1/files/upload",
                headers=self._headers(),
                files=files,
                data=data,
                timeout=300,
            )
        if r.status_code != 200:
            raise DriveProxyError(
                f"upload failed status={r.status_code} dst_drive_folder_id={dst_drive_folder_id} body={r.text}"
            )
        try:
            payload = r.json()
        except Exception as exc:
            raise DriveProxyError(f"upload returned non-JSON response: {r.text}") from exc
        drive_file_id = str(payload.get("drive_file_id") or payload.get("id") or "").strip()
        if not drive_file_id:
            raise DriveProxyError(f"upload response missing drive_file_id: {payload}")
        return drive_file_id

    def create_folder(self, parent_drive_folder_id: str, name: str) -> str:
        if not self.base_url:
            raise DriveProxyError("DRIVE_PROXY_BASE_URL is required for create_folder")
        if not parent_drive_folder_id:
            raise DriveProxyError("parent_drive_folder_id is required for create_folder")
        payload = {"parent_drive_folder_id": parent_drive_folder_id, "name": name}
        r = requests.post(
            f"{self.base_url.rstrip('/')}/v1/folders",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        if r.status_code != 200:
            raise DriveProxyError(
                f"create_folder failed status={r.status_code} parent_drive_folder_id={parent_drive_folder_id} body={r.text}"
            )
        try:
            data = r.json()
        except Exception as exc:
            raise DriveProxyError(f"create_folder returned non-JSON response: {r.text}") from exc
        folder_id = str(data.get("drive_folder_id") or data.get("id") or "").strip()
        if not folder_id:
            raise DriveProxyError(f"create_folder response missing drive_folder_id: {data}")
        return folder_id

    def search_file_id_by_name(
        self,
        name: str,
        *,
        parent_drive_folder_id: str = "",
        mime_type: str = "",
    ) -> str:
        ids = self.search_file_ids_by_name(
            name,
            parent_drive_folder_id=parent_drive_folder_id,
            mime_type=mime_type,
        )
        return ids[0] if ids else ""

    def search_file_ids_by_name(
        self,
        name: str,
        *,
        parent_drive_folder_id: str = "",
        mime_type: str = "",
    ) -> list[str]:
        if not self.base_url:
            raise DriveProxyError("DRIVE_PROXY_BASE_URL is required for search_file_ids_by_name")
        if not name:
            raise DriveProxyError("name is required for search_file_ids_by_name")
        params: Dict[str, Any] = {"name": name}
        if parent_drive_folder_id:
            params["parent_drive_folder_id"] = parent_drive_folder_id
        if mime_type:
            params["mime_type"] = mime_type
        r = requests.get(
            f"{self.base_url.rstrip('/')}/v1/files/search",
            headers=self._headers(),
            params=params,
            timeout=120,
        )
        if r.status_code != 200:
            raise DriveProxyError(
                f"search failed status={r.status_code} parent_drive_folder_id={parent_drive_folder_id} body={r.text}"
            )
        try:
            payload = r.json()
        except Exception as exc:
            raise DriveProxyError(f"search returned non-JSON response: {r.text}") from exc
        rows = payload.get("files") or payload.get("items") or payload.get("results") or []
        if not isinstance(rows, list):
            rows = []
        exact_ids: list[str] = []
        fallback_ids: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_name = str(row.get("name") or "").strip()
            row_id = str(row.get("drive_file_id") or row.get("id") or "").strip()
            if not row_id:
                continue
            if row_name == name:
                exact_ids.append(row_id)
            else:
                fallback_ids.append(row_id)
        return exact_ids or fallback_ids


def from_env() -> DriveProxyClient:
    base_url = os.environ.get("DRIVE_PROXY_BASE_URL", "").strip()
    if not base_url:
        raise DriveProxyError("DRIVE_PROXY_BASE_URL is required (must point to the drive-proxy service).")
    return DriveProxyClient(
        base_url=base_url,
        api_key=os.environ.get("DRIVE_PROXY_API_KEY", "").strip(),
    )

from __future__ import annotations

import os
import time
from pathlib import Path

import requests


PROXY_SHARE_HINT = "Not found. Share file/folder with drive-cdn-proxy@drive-cdn-proxy.iam.gserviceaccount.com"


class DriveProxyError(RuntimeError):
    pass


class DriveProxyClient:
    def __init__(self, base_url: str | None = None, timeout_s: int = 120, max_retries: int = 3):
        self.base_url = (base_url or os.environ.get("DRIVE_PROXY_BASE_URL", "https://YOUR-DRIVE-PROXY.a.run.app")).rstrip("/")
        self.timeout_s = int(timeout_s)
        self.max_retries = int(max_retries)

    def build_url(self, file_id: str) -> str:
        return f"{self.base_url}/f/{str(file_id).strip()}"

    def head(self, file_id: str) -> tuple[int, dict[str, str]]:
        url = self.build_url(file_id)
        r = requests.head(url, timeout=self.timeout_s)
        return int(r.status_code), dict(r.headers)

    def download_file(self, file_id: str, dst_path: str) -> str:
        if not file_id:
            raise DriveProxyError("file_id is required")
        url = self.build_url(file_id)
        tmp_path = f"{dst_path}.part"
        Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, self.max_retries + 1):
            r = requests.get(url, timeout=self.timeout_s, stream=True)
            status = int(r.status_code)
            if status == 200:
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)
                Path(tmp_path).replace(dst_path)
                return dst_path
            if status in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                time.sleep(min(2 ** (attempt - 1), 5))
                continue
            body = (r.text or "")[:200]
            if status in {403, 404}:
                raise DriveProxyError(f"download failed status={status} url={url} body={body} {PROXY_SHARE_HINT}")
            raise DriveProxyError(f"download failed status={status} url={url} body={body}")
        raise DriveProxyError(f"download failed after retries file_id={file_id}")

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.drive_proxy import DriveProxyClient  # noqa: E402


def _must_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _preview_body(content: bytes) -> str:
    if not content:
        return ""
    if any((b < 9 or (13 < b < 32)) for b in content):
        return "<binary>"
    return content.decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check drive-proxy /f/<id> reachability and response")
    parser.add_argument("--drive-file-id", required=True)
    args = parser.parse_args()

    base_url = _must_env("DRIVE_PROXY_BASE_URL")
    api_key = os.environ.get("DRIVE_PROXY_API_KEY", "").strip()
    client = DriveProxyClient(base_url=base_url, api_key=api_key)
    url = client.file_url(args.drive_file_id)
    headers = client._headers()
    resp = requests.get(url, headers=headers, timeout=120, stream=True)
    chunk = next(resp.iter_content(chunk_size=200), b"")

    print(f"url={url}")
    print(f"api_key_sent={'yes' if bool(api_key) else 'no'}")
    print(f"status_code={resp.status_code}")
    print(f"body_preview={_preview_body(chunk)}")
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())

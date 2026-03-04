from __future__ import annotations

import pytest

from so_sync import drive_proxy


def test_from_env_reads_drive_proxy_base_url(monkeypatch) -> None:
    monkeypatch.setenv("DRIVE_PROXY_BASE_URL", "https://drive-proxy.example")
    monkeypatch.setenv("DRIVE_PROXY_API_KEY", "k1")
    client = drive_proxy.from_env()
    assert client.base_url == "https://drive-proxy.example"
    assert client.api_key == "k1"


def test_from_env_raises_when_drive_proxy_base_missing(monkeypatch) -> None:
    monkeypatch.delenv("DRIVE_PROXY_BASE_URL", raising=False)
    with pytest.raises(drive_proxy.DriveProxyError, match=r"DRIVE_PROXY_BASE_URL is required"):
        drive_proxy.from_env()

from __future__ import annotations

from pathlib import Path

import pytest

from so_sync.drive_proxy import DriveProxyClient
import so_sync.drive_proxy as drive_proxy


def test_file_url_uses_f_path() -> None:
    c1 = DriveProxyClient(base_url="https://drive-proxy.example")
    c2 = DriveProxyClient(base_url="https://drive-proxy.example/f")
    c3 = DriveProxyClient(base_url="https://drive-proxy.example/f/")
    assert c1.file_url("abc123") == "https://drive-proxy.example/f/abc123"
    assert c2.file_url("abc123") == "https://drive-proxy.example/f/abc123"
    assert c3.file_url("abc123") == "https://drive-proxy.example/f/abc123"


def test_download_file_hits_f_route(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    class _Resp:
        status_code = 200
        text = ""

        def iter_content(self, chunk_size: int = 0):
            yield b"hello"

    def _fake_get(url: str, **kwargs):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(drive_proxy.requests, "get", _fake_get)
    client = DriveProxyClient(base_url="https://drive-proxy.example")
    dst = tmp_path / "out.bin"
    path = client.download_file("file123", str(dst))
    assert captured["url"] == "https://drive-proxy.example/f/file123"
    assert path == str(dst)
    assert dst.read_bytes() == b"hello"


def test_download_file_404_includes_hint_and_api_key_sent(monkeypatch, tmp_path: Path) -> None:
    class _Resp:
        status_code = 404
        text = "Drive proxy error (404)"

        def iter_content(self, chunk_size: int = 0):
            yield b""

    def _fake_get(url: str, **kwargs):
        return _Resp()

    monkeypatch.setattr(drive_proxy.requests, "get", _fake_get)
    client = DriveProxyClient(base_url="https://drive-proxy.example", api_key="secret")
    with pytest.raises(drive_proxy.DriveProxyError) as exc:
        client.download_file("missing_file", str(tmp_path / "out.bin"))
    msg = str(exc.value)
    assert "api_key_sent=yes" in msg
    assert "Share the file/folder with <SERVICE_ACCOUNT_EMAIL>" in msg

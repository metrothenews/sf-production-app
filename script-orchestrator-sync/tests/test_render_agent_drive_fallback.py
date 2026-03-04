from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_download_with_fallback_uses_drive_api_on_proxy_error(tmp_path, capsys) -> None:
    calls = {"proxy": 0, "upload": 0}

    class _Proxy:
        def download_file(self, drive_file_id: str, dst_path: str) -> str:
            calls["proxy"] += 1
            raise render_agent.DriveProxyError("download failed status=404 url=x")

    class _Upload:
        def download_file(self, drive_file_id: str, dst_path: str) -> str:
            calls["upload"] += 1
            Path(dst_path).write_bytes(b"ok")
            return dst_path

    dst = tmp_path / "a.bin"
    out = render_agent._download_with_fallback(
        "file123",
        str(dst),
        proxy_client=_Proxy(),
        upload_client=_Upload(),
    )
    assert out == str(dst)
    assert calls["proxy"] == 1
    assert calls["upload"] == 1
    assert "drive_download_fallback=drive_api file_id=file123 reason=proxy_404" in capsys.readouterr().out


def test_download_with_fallback_does_not_call_drive_api_when_proxy_succeeds(tmp_path) -> None:
    calls = {"proxy": 0}

    class _Proxy:
        def download_file(self, drive_file_id: str, dst_path: str) -> str:
            calls["proxy"] += 1
            Path(dst_path).write_bytes(b"proxy")
            return dst_path

    class _Upload:
        def download_file(self, drive_file_id: str, dst_path: str) -> str:
            raise AssertionError("upload fallback should not be used when proxy succeeds")

    dst = tmp_path / "b.bin"
    out = render_agent._download_with_fallback(
        "file456",
        str(dst),
        proxy_client=_Proxy(),
        upload_client=_Upload(),
    )
    assert out == str(dst)
    assert calls["proxy"] == 1
    assert dst.read_bytes() == b"proxy"


def test_download_with_fallback_uses_drive_api_when_proxy_env_missing(monkeypatch, tmp_path, capsys) -> None:
    calls = {"upload": 0}

    def _fake_proxy_from_env():
        raise render_agent.DriveProxyError("DRIVE_PROXY_BASE_URL is required (must point to the drive-proxy service).")

    class _Upload:
        def download_file(self, drive_file_id: str, dst_path: str) -> str:
            calls["upload"] += 1
            Path(dst_path).write_bytes(b"ok")
            return dst_path

    monkeypatch.setattr(render_agent, "drive_proxy_from_env", _fake_proxy_from_env)
    dst = tmp_path / "c.bin"
    out = render_agent._download_with_fallback(
        "file789",
        str(dst),
        upload_client=_Upload(),
    )
    assert out == str(dst)
    assert calls["upload"] == 1
    assert "drive_download_fallback=drive_api file_id=file789 reason=proxy_unconfigured" in capsys.readouterr().out

from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "drive_proxy_preflight.py"
_SPEC = importlib.util.spec_from_file_location("drive_proxy_preflight", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import script from {_SCRIPT_PATH}")
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)


def test_preflight_hits_f_route_and_sends_api_key_header(monkeypatch, capsys) -> None:
    calls: dict[str, object] = {}

    class _Resp:
        status_code = 200

        def iter_content(self, chunk_size: int = 0):
            yield b"ok"

    def _fake_get(url: str, headers: dict[str, str], timeout: int, stream: bool):
        calls["url"] = url
        calls["headers"] = headers
        return _Resp()

    monkeypatch.setenv("DRIVE_PROXY_BASE_URL", "https://drive-proxy.example")
    monkeypatch.setenv("DRIVE_PROXY_API_KEY", "k1")
    monkeypatch.setattr(preflight.requests, "get", _fake_get)
    monkeypatch.setattr(preflight.sys, "argv", ["drive_proxy_preflight.py", "--drive-file-id", "file123"])

    rc = preflight.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert calls["url"] == "https://drive-proxy.example/f/file123"
    assert calls["headers"] == {"X-API-Key": "k1"}
    assert "api_key_sent=yes" in out

from __future__ import annotations

from render_agent.drive_proxy import DriveProxyClient


def test_build_url_trims_slashes() -> None:
    c1 = DriveProxyClient(base_url="https://drive-proxy.example")
    c2 = DriveProxyClient(base_url="https://drive-proxy.example/")
    assert c1.build_url("abc") == "https://drive-proxy.example/f/abc"
    assert c2.build_url("abc") == "https://drive-proxy.example/f/abc"

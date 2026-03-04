from __future__ import annotations

import sys
from types import ModuleType

from render_agent.auth_diag import describe_google_auth


class _Creds:
    service_account_email = "svc@example.com"
    scopes = ["scope-a"]


def _install_fake_google_auth(monkeypatch, *, default_result=None, load_result=None, default_exc=None):
    google_mod = ModuleType("google")
    auth_mod = ModuleType("google.auth")

    def _default(*args, **kwargs):
        if default_exc:
            raise default_exc
        return default_result

    def _load(path, *args, **kwargs):
        return load_result

    auth_mod.default = _default
    auth_mod.load_credentials_from_file = _load
    google_mod.auth = auth_mod
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.auth", auth_mod)


def test_describe_google_auth_gac_mode(monkeypatch, tmp_path) -> None:
    creds_file = tmp_path / "svc.json"
    creds_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(creds_file))
    _install_fake_google_auth(monkeypatch, default_result=(_Creds(), "adc-project"), load_result=(_Creds(), "svc-project"))

    diag = describe_google_auth()

    assert diag["mode"] == "gac"
    assert diag["principal_hint"] == "svc@example.com"
    assert diag["project_id"] == "svc-project"
    assert diag["scopes"] == ["scope-a"]
    assert diag["gac_path"] == str(creds_file)


def test_describe_google_auth_adc_error(monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    _install_fake_google_auth(monkeypatch, default_exc=RuntimeError("invalid_grant"))

    diag = describe_google_auth()

    assert diag["mode"] == "adc"
    assert diag["error_type"] == "RuntimeError"
    assert "invalid_grant" in str(diag["error"])

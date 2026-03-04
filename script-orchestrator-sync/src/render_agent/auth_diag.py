from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _principal_hint(creds: Any) -> str | None:
    for attr in ("service_account_email", "_service_account_email", "signer_email"):
        val = getattr(creds, attr, None)
        if val:
            return str(val)
    return None


def _scopes_hint(creds: Any) -> list[str] | None:
    scopes = getattr(creds, "scopes", None) or getattr(creds, "default_scopes", None)
    if not scopes:
        return None
    return [str(s) for s in scopes]


def describe_google_auth() -> dict[str, Any]:
    adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    base = {
        "mode": "gac" if gac else "adc",
        "principal_hint": None,
        "project_id": None,
        "scopes": None,
        "adc_file_exists": adc_path.exists(),
    }
    try:
        import google.auth
        from google.auth import load_credentials_from_file
    except Exception as exc:
        return {**base, "error": str(exc), "error_type": type(exc).__name__}

    try:
        if gac:
            creds, project_id = load_credentials_from_file(gac)
            return {
                **base,
                "gac_path": gac,
                "principal_hint": _principal_hint(creds),
                "project_id": project_id,
                "scopes": _scopes_hint(creds),
            }
        creds, project_id = google.auth.default()
        return {
            **base,
            "principal_hint": _principal_hint(creds),
            "project_id": project_id,
            "scopes": _scopes_hint(creds),
        }
    except Exception as exc:
        out = dict(base)
        if gac:
            out["gac_path"] = gac
        out["error"] = str(exc)
        out["error_type"] = type(exc).__name__
        return out

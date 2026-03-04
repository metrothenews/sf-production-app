#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway  # noqa: E402
from so_sync.render_repo import find_render_job_by_id, upsert_render_job  # noqa: E402

DEFAULT_BASE = "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"


def _must_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def build_requeue_payload(existing_row: Dict[str, Any], *, now_utc: str) -> Dict[str, Any]:
    row = dict(existing_row)
    row.update(
        {
            "enabled": "TRUE",
            "status": "queued",
            "lease_owner": "",
            "lease_until_utc": "",
            "run_id": "",
            "error_msg": "",
            "updated_utc": now_utc,
        }
    )
    return row


def _snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "render_job_id",
        "enabled",
        "status",
        "lease_owner",
        "lease_until_utc",
        "run_id",
        "error_msg",
    ]
    return {k: row.get(k, "") for k in keys}


def main() -> int:
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    render_job_id = _must_env("RENDER_JOB_ID")
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", DEFAULT_BASE).strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    gw = SheetsGateway(base_url=base_url, api_key=api_key)

    existing = find_render_job_by_id(gw, control_sheet_id, render_job_id)
    if not existing:
        raise RuntimeError(f"Render_Jobs row not found for render_job_id={render_job_id}")

    print(f"before={_snapshot(existing)}")
    updated = build_requeue_payload(existing, now_utc=_utc_now_iso())
    upsert_render_job(gw, control_sheet_id, updated)
    after = find_render_job_by_id(gw, control_sheet_id, render_job_id) or updated
    print(f"after={_snapshot(after)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

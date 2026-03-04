from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "requeue_render_job.py"
_SPEC = importlib.util.spec_from_file_location("requeue_render_job", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import script from {_SCRIPT_PATH}")
requeue = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(requeue)


def test_build_requeue_payload_sets_required_columns() -> None:
    existing = {
        "render_job_id": "cfg_123",
        "enabled": "FALSE",
        "status": "failed",
        "lease_owner": "host1",
        "lease_until_utc": "2026-03-03T00:00:00+00:00",
        "run_id": "run_1",
        "error_msg": "boom",
    }
    row = requeue.build_requeue_payload(existing, now_utc="2026-03-03T01:00:00+00:00")
    assert row["enabled"] == "TRUE"
    assert row["status"] == "queued"
    assert row["lease_owner"] == ""
    assert row["lease_until_utc"] == ""
    assert row["run_id"] == ""
    assert row["error_msg"] == ""
    assert row["updated_utc"] == "2026-03-03T01:00:00+00:00"

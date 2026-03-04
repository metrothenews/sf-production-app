from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "seed_one_render_job.py"
_SPEC = importlib.util.spec_from_file_location("seed_one_render_job", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import script from {_SCRIPT_PATH}")
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)


def test_build_seed_values_includes_required_columns() -> None:
    payload = seed.build_seed_values(
        job_id="cfg_1",
        source_folder_ids=["folderA"],
        source_file_ids=["fileA", "fileB"],
        target_count=5,
        status="queued",
        select_mode="shuffle_seeded",
        select_seed="12345",
        output_folder_id="outFolder",
        now_utc="2026-03-03T00:00:00+00:00",
    )
    assert payload["render_job_id"] == "cfg_1"
    assert payload["enabled"] == "TRUE"
    assert payload["status"] == "queued"
    assert payload["target_count"] == "5"
    assert payload["source_folder_ids"] == "folderA"
    assert payload["source_file_ids"] == "fileA,fileB"
    assert payload["output_folder_id"] == "outFolder"
    assert payload["lease_owner"] == ""
    assert payload["run_id"] == ""
    assert payload["error_msg"] == ""

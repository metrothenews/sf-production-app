from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_so_campaigns_schema.py"
_SPEC = importlib.util.spec_from_file_location("audit_so_campaigns_schema", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import audit script from {_SCRIPT_PATH}")
audit = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = audit
_SPEC.loader.exec_module(audit)


def test_build_audit_report_missing_performance_columns() -> None:
    header = ["campaign_id", "edit_mode", "render_mode", "notes"]
    rows = [
        ["campaign_1", "FF_ONLY", "found_footage", "a"],
        ["campaign_2", "FF_PLUS_PERF", "", "b"],
    ]
    report = audit.build_audit_report(header, rows, sample_rows=2)
    assert "performance_mode" in report["performance_columns_missing"]
    assert "performance_preset_dsl" in report["performance_columns_missing"]
    assert report["sample_completeness"]["render_mode"]["sample_nonempty"] == 1
    assert report["sample_completeness"]["edit_mode"]["sample_nonempty"] == 2


def test_build_audit_report_present_columns_have_completeness_counts() -> None:
    header = [
        "campaign_id",
        "edit_mode",
        "render_mode",
        "performance_mode",
        "performance_audio_policy",
        "performance_preset_dsl",
    ]
    rows = [
        ["campaign_1", "FF_PLUS_PERF", "performance", "preset", "master_only", "4a2br2cw"],
        ["campaign_2", "FF_ONLY", "found_footage", "", "", ""],
    ]
    report = audit.build_audit_report(header, rows, sample_rows=2)
    assert report["performance_columns_missing"][0] == "performance_master_clip_drive_file_id"
    assert report["sample_completeness"]["performance_mode"]["sample_nonempty"] == 1
    assert report["sample_completeness"]["performance_audio_policy"]["sample_nonempty"] == 1

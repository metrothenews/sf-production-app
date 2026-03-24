from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_so_campaigns_performance_mode.py"
_SPEC = importlib.util.spec_from_file_location("migrate_so_campaigns_performance_mode", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import migration script from {_SCRIPT_PATH}")
migrator = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = migrator
_SPEC.loader.exec_module(migrator)


def _row_update_map(plan: dict) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    for row in plan.get("row_updates", []):
        out[int(row["row_num"])] = dict(row["updates"])
    return out


def test_build_migration_plan_appends_columns_only_at_end() -> None:
    header = ["campaign_id", "edit_mode", "notes"]
    rows = [["campaign_1", "FF_ONLY", "x"]]
    plan = migrator.build_migration_plan(header, rows)
    assert plan["target_header"][: len(header)] == header
    assert plan["added_columns"] == migrator.NEW_COLUMNS


def test_build_migration_plan_backfills_compat_defaults() -> None:
    header = ["campaign_id", "edit_mode", "render_mode", "performance_mode", "performance_audio_policy"]
    rows = [
        ["campaign_1", "FF_ONLY", "", "", ""],
        ["campaign_2", "FF_PLUS_PERF", "", "", ""],
        ["campaign_3", "", "performance", "", ""],
        ["campaign_4", "", "found_footage", "", ""],
    ]
    plan = migrator.build_migration_plan(header, rows)
    updates = _row_update_map(plan)
    assert updates[2] == {"render_mode": "found_footage"}
    assert updates[3]["render_mode"] == "performance"
    assert updates[3]["performance_mode"] == "simple_sync_cut"
    assert updates[3]["performance_audio_policy"] == "master_only"
    assert updates[4] == {"performance_mode": "simple_sync_cut", "performance_audio_policy": "master_only"}
    assert 5 not in updates


def test_migration_plan_idempotent_after_apply_in_memory() -> None:
    header = ["campaign_id", "edit_mode", "render_mode"]
    rows = [
        ["campaign_1", "FF_ONLY", ""],
        ["campaign_2", "FF_PLUS_PERF", ""],
    ]
    first = migrator.build_migration_plan(header, rows)
    next_header, next_rows = migrator.apply_plan_in_memory(header, rows, first)
    second = migrator.build_migration_plan(next_header, next_rows)
    assert second["added_columns"] == []
    assert second["row_updates"] == []

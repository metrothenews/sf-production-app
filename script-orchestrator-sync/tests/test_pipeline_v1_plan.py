from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_pipeline_v1_stage_plan_without_copy() -> None:
    manifest = {"copy_pass": {"enabled": False}}
    resolved_inputs = {"copy_srt_path": ""}
    assert render_agent.plan_pipeline_v1_stages(manifest, resolved_inputs) == ["stitch", "lyrics"]


def test_pipeline_v1_stage_plan_with_copy() -> None:
    manifest = {"copy_pass": {"enabled": True}}
    resolved_inputs = {"copy_srt_path": "/tmp/copy.srt"}
    assert render_agent.plan_pipeline_v1_stages(manifest, resolved_inputs) == ["stitch", "lyrics", "copy"]

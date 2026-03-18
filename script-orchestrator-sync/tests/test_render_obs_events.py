from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_EVENTS_PATH = Path(__file__).resolve().parents[1] / "src" / "render_obs" / "events.py"
_SPEC = importlib.util.spec_from_file_location("render_obs_events", _EVENTS_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import events module from {_EVENTS_PATH}")
events_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(events_mod)
REQUIRED_KEYS = events_mod.REQUIRED_KEYS
RenderEventLogger = events_mod.RenderEventLogger


def test_render_event_logger_writes_required_keys(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    logger = RenderEventLogger(str(path), enabled=True)
    logger.emit(
        run_id="r1",
        render_job_id="j1",
        cut_idx=2,
        stage="effects:clip.mp4",
        attempt=1,
        event="stage_start",
        severity="info",
        code="",
        message="start",
        cmd="blender -b --python script.py",
    )
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    for key in REQUIRED_KEYS:
        assert key in payload
    assert payload["run_id"] == "r1"
    assert payload["render_job_id"] == "j1"
    assert payload["cut_idx"] == 2
    assert payload["event"] == "stage_start"


def test_render_event_logger_rebind_copies_existing_lines(tmp_path: Path) -> None:
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    logger = RenderEventLogger(str(first), enabled=True)
    logger.emit(
        run_id="r1",
        render_job_id="j1",
        event="run_start",
        message="start",
        stage="run",
    )
    logger.rebind(str(second))
    logger.emit(
        run_id="r1",
        render_job_id="j1",
        event="run_end",
        message="done",
        stage="run",
    )
    lines = second.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "run_start"
    assert json.loads(lines[1])["event"] == "run_end"

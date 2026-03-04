from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_run_to_log_retries_then_succeeds(monkeypatch, tmp_path) -> None:
    calls = {"n": 0}

    def _fake_run(cmd, **kwargs):
        calls["n"] += 1
        stdout = kwargs.get("stdout")
        stdout.write(f"attempt={calls['n']}\n".encode("utf-8"))
        if calls["n"] == 1:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(render_agent.subprocess, "run", _fake_run)
    log_path = tmp_path / "stage.log"
    render_agent._run_to_log(
        ["blender", "--fake"],
        str(log_path),
        stage="lyrics",
        retries=1,
        retry_delay_seconds=0.0,
    )
    assert calls["n"] == 2
    text = log_path.read_text(encoding="utf-8")
    assert "retry attempt 2/2 stage=lyrics" in text


def test_run_to_log_failure_includes_tail(monkeypatch, tmp_path) -> None:
    def _fake_run(cmd, **kwargs):
        stdout = kwargs.get("stdout")
        stdout.write(b"Segmentation fault\n")
        return SimpleNamespace(returncode=139)

    monkeypatch.setattr(render_agent.subprocess, "run", _fake_run)
    log_path = tmp_path / "stage_fail.log"
    with pytest.raises(RuntimeError, match=r"log_tail=.*Segmentation fault"):
        render_agent._run_to_log(
            ["blender", "--fake"],
            str(log_path),
            stage="stitch",
            retries=0,
            retry_delay_seconds=0.0,
        )


def test_pick_cut_source_video_prefers_candidate_when_long_enough(monkeypatch) -> None:
    monkeypatch.setattr(render_agent, "_probe_media_duration_seconds", lambda path: 12.0)
    out = render_agent._pick_cut_source_video(
        base_mp4="/tmp/base.mp4",
        candidate_mp4="/tmp/lyrics.mp4",
        max_cut_end_s=10.0,
        candidate_label="lyrics",
        candidate_log_path="/tmp/lyrics.log",
    )
    assert out == "/tmp/lyrics.mp4"


def test_pick_cut_source_video_falls_back_to_base_when_candidate_short(monkeypatch) -> None:
    durations = {"/tmp/base.mp4": 15.0, "/tmp/lyrics.mp4": 2.0}
    monkeypatch.setattr(render_agent, "_probe_media_duration_seconds", lambda path: durations[path])
    out = render_agent._pick_cut_source_video(
        base_mp4="/tmp/base.mp4",
        candidate_mp4="/tmp/lyrics.mp4",
        max_cut_end_s=10.0,
        candidate_label="lyrics",
        candidate_log_path="/tmp/lyrics.log",
    )
    assert out == "/tmp/base.mp4"


def test_pick_cut_source_video_raises_when_both_short(monkeypatch) -> None:
    durations = {"/tmp/base.mp4": 4.0, "/tmp/lyrics.mp4": 2.0}
    monkeypatch.setattr(render_agent, "_probe_media_duration_seconds", lambda path: durations[path])
    with pytest.raises(RuntimeError, match=r"candidate_label=lyrics"):
        render_agent._pick_cut_source_video(
            base_mp4="/tmp/base.mp4",
            candidate_mp4="/tmp/lyrics.mp4",
            max_cut_end_s=10.0,
            candidate_label="lyrics",
            candidate_log_path="/tmp/lyrics.log",
        )

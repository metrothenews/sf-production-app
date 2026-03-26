from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_run_to_log_retries_then_succeeds(monkeypatch, tmp_path) -> None:
    calls = {"n": 0}

    class _FakePopen:
        def __init__(self, _cmd, stdout=None, stderr=None):
            del stderr
            calls["n"] += 1
            self.returncode = None
            self._poll_count = 0
            if stdout is not None:
                stdout.write(f"attempt={calls['n']}\n".encode("utf-8"))

        def poll(self):
            self._poll_count += 1
            if self._poll_count == 1:
                return None
            self.returncode = 1 if calls["n"] == 1 else 0
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return int(self.returncode or 0)

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(render_agent.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(render_agent.time, "sleep", lambda _x: None)
    monkeypatch.setattr(render_agent, "_pipeline_stage_timeout_seconds", lambda: 0.0)
    monkeypatch.setattr(render_agent, "_pipeline_stage_heartbeat_seconds", lambda: 0.0)
    render_agent._init_event_logger(run_id="r_test", render_job_id="j_test", path=str(tmp_path / "events.jsonl"))

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
    class _FakePopen:
        def __init__(self, _cmd, stdout=None, stderr=None):
            del stderr
            self.returncode = None
            self._poll_count = 0
            if stdout is not None:
                stdout.write(b"Segmentation fault\n")

        def poll(self):
            self._poll_count += 1
            if self._poll_count == 1:
                return None
            self.returncode = 139
            return 139

        def wait(self, timeout=None):
            del timeout
            return int(self.returncode or 139)

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(render_agent.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(render_agent.time, "sleep", lambda _x: None)
    monkeypatch.setattr(render_agent, "_pipeline_stage_timeout_seconds", lambda: 0.0)
    monkeypatch.setattr(render_agent, "_pipeline_stage_heartbeat_seconds", lambda: 0.0)
    render_agent._init_event_logger(run_id="r_test", render_job_id="j_test", path=str(tmp_path / "events.jsonl"))

    log_path = tmp_path / "stage_fail.log"
    with pytest.raises(RuntimeError, match=r"log_tail=.*Segmentation fault"):
        render_agent._run_to_log(
            ["blender", "--fake"],
            str(log_path),
            stage="stitch",
            retries=0,
            retry_delay_seconds=0.0,
        )


def test_run_to_log_timeout(monkeypatch, tmp_path) -> None:
    class _FakePopen:
        def __init__(self, _cmd, stdout=None, stderr=None):
            del stdout, stderr
            self.returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            del timeout
            return -9

        def kill(self):
            self.returncode = -9

    ticks = {"n": 0}

    def _fake_monotonic():
        ticks["n"] += 1
        return ticks["n"] * 0.1

    monkeypatch.setattr(render_agent.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(render_agent.time, "sleep", lambda _x: None)
    monkeypatch.setattr(render_agent.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(render_agent, "_pipeline_stage_timeout_seconds", lambda: 0.2)
    monkeypatch.setattr(render_agent, "_pipeline_stage_heartbeat_seconds", lambda: 0.0)
    render_agent._init_event_logger(run_id="r_test", render_job_id="j_test", path=str(tmp_path / "events.jsonl"))

    with pytest.raises(RuntimeError, match=r"timed out"):
        render_agent._run_to_log(
            ["blender", "--fake"],
            str(tmp_path / "stage_timeout.log"),
            stage="effects",
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


def test_pick_cut_source_video_keeps_candidate_when_base_fallback_disabled(monkeypatch) -> None:
    durations = {"/tmp/base.mp4": 15.0, "/tmp/lyrics.mp4": 2.0}
    monkeypatch.setattr(render_agent, "_probe_media_duration_seconds", lambda path: durations[path])
    out = render_agent._pick_cut_source_video(
        base_mp4="/tmp/base.mp4",
        candidate_mp4="/tmp/lyrics.mp4",
        max_cut_end_s=10.0,
        candidate_label="lyrics",
        candidate_log_path="/tmp/lyrics.log",
        allow_base_fallback=False,
    )
    assert out == "/tmp/lyrics.mp4"


def test_pick_cut_source_video_uses_gate_aligned_tolerance(monkeypatch) -> None:
    durations = {"/tmp/base.mp4": 15.0, "/tmp/lyrics.mp4": 9.85}
    monkeypatch.setattr(render_agent, "_probe_media_duration_seconds", lambda path: durations[path])
    out = render_agent._pick_cut_source_video(
        base_mp4="/tmp/base.mp4",
        candidate_mp4="/tmp/lyrics.mp4",
        max_cut_end_s=10.0,
        candidate_label="lyrics",
        candidate_log_path="/tmp/lyrics.log",
    )
    assert out == "/tmp/lyrics.mp4"


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


def test_expected_frames_for_selected_source_prefers_probed_frames(monkeypatch) -> None:
    monkeypatch.setattr(render_agent, "_probe_media_video_frame_count", lambda _path: 801)
    out = render_agent._expected_frames_for_selected_source(
        selected_source="/tmp/lyrics.mp4",
        default_expected_frames=804,
    )
    assert out == 801


def test_expected_frames_for_selected_source_uses_default_when_probe_missing(monkeypatch) -> None:
    monkeypatch.setattr(render_agent, "_probe_media_video_frame_count", lambda _path: 0)
    out = render_agent._expected_frames_for_selected_source(
        selected_source="/tmp/lyrics.mp4",
        default_expected_frames=804,
    )
    assert out == 804

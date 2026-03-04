from __future__ import annotations

from pathlib import Path

from so_sync import render_batch_trigger as trigger


def test_parse_queue_progress_snapshots_extracts_json_lines() -> None:
    output = "\n".join(
        [
            "queued_render_job_id=cfg_1",
            'queue_progress={"percent":0.0,"done_jobs":0,"total_jobs":3}',
            "other_log=ok",
            'queue_progress={"percent":33.33,"done_jobs":1,"total_jobs":3}',
        ]
    )
    snapshots = trigger.parse_queue_progress_snapshots(output)
    assert len(snapshots) == 2
    assert snapshots[-1]["percent"] == 33.33


def test_summarize_completed_run_busy() -> None:
    output = "batch_busy=true reason=render_jobs_running\n"
    result = trigger.summarize_completed_run(2, output)
    assert result["status"] == "busy"
    assert result["busy_reason"] == "render_jobs_running"


def test_summarize_completed_run_error_reads_batch_error_line() -> None:
    output = "batch_error=compile_failed code=1\n"
    result = trigger.summarize_completed_run(1, output)
    assert result["status"] == "error"
    assert "batch_error=compile_failed" in str(result["error"])


def test_start_render_batch_process_returns_started_for_long_run(monkeypatch) -> None:
    class _FakeProc:
        pid = 77777

        def poll(self):
            return None

    def _fake_popen(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(trigger.subprocess, "Popen", _fake_popen)
    result = trigger.start_render_batch_process(
        "sheet_1",
        startup_wait_seconds=0.0,
    )
    assert result["status"] == "started"
    assert result["pid"] == 77777
    assert Path(result["log_path"]).name.startswith("render_batch_")


def test_start_render_batch_process_returns_busy_when_process_exits_fast(monkeypatch) -> None:
    class _FakeProc:
        pid = 88888

        def poll(self):
            return 2

    def _fake_popen(*args, **kwargs):
        stdout = kwargs.get("stdout")
        stdout.write("batch_busy=true reason=local_lock\n")
        stdout.flush()
        return _FakeProc()

    monkeypatch.setattr(trigger.subprocess, "Popen", _fake_popen)
    result = trigger.start_render_batch_process("sheet_2", startup_wait_seconds=0.0)
    assert result["status"] == "busy"
    assert result["busy_reason"] == "local_lock"
    assert result["return_code"] == 2

from __future__ import annotations

import importlib


run_mod = importlib.import_module("render_agent.run")


def test_read_jobs_to_run_filters_by_job_id(monkeypatch) -> None:
    def _fake_rows():
        return [
            {"render_job_id": "job_a", "status": "queued", "enabled": "TRUE"},
            {"render_job_id": "job_b", "status": "retry", "enabled": "TRUE"},
        ]

    monkeypatch.setattr(run_mod, "_read_render_jobs_rows", _fake_rows)
    jobs = run_mod.read_jobs_to_run(job_id="job_b", limit=10)
    assert [j["render_job_id"] for j in jobs] == ["job_b"]

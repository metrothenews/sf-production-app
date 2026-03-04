#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway  # noqa: E402
from so_sync.render_repo import read_render_jobs_all  # noqa: E402
from so_sync.repositories import normalize_enabled_value  # noqa: E402

DEFAULT_BASE = "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"
LOCK_FILE = Path(tempfile.gettempdir()) / "so_render_batch_trigger.lock"
ACTIVE_PROCESS_STATUSES = {"leased", "running"}
RUNNABLE_STATUSES = {"queued", "retry"}


def _must_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _read_jobs(gw: SheetsGateway, control_sheet_id: str) -> list[dict[str, Any]]:
    return read_render_jobs_all(gw, control_sheet_id)


def is_process_running(rows: Iterable[dict[str, Any]]) -> bool:
    for row in rows:
        if not normalize_enabled_value(row.get("enabled")):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status in ACTIVE_PROCESS_STATUSES:
            return True
    return False


def parse_queued_job_ids(compile_stdout: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in str(compile_stdout or "").splitlines():
        m = re.match(r"^queued_render_job_id=([^\s]+)", line.strip())
        if not m:
            continue
        job_id = m.group(1).strip()
        if job_id and job_id not in seen:
            seen.add(job_id)
            out.append(job_id)
    return out


def compute_queue_progress(rows: Iterable[dict[str, Any]], target_job_ids: list[str]) -> dict[str, Any]:
    status_by_id = {
        str(row.get("render_job_id") or "").strip(): str(row.get("status") or "").strip().lower()
        for row in rows
    }
    total = len(target_job_ids)
    if total <= 0:
        return {
            "total_jobs": 0,
            "done_jobs": 0,
            "success_jobs": 0,
            "failed_jobs": 0,
            "running_jobs": 0,
            "queued_jobs": 0,
            "percent": 100.0,
        }
    success = 0
    failed = 0
    running = 0
    queued = 0
    for job_id in target_job_ids:
        status = status_by_id.get(job_id, "")
        if status == "success":
            success += 1
        elif status == "failed":
            failed += 1
        elif status in {"running", "leased"}:
            running += 1
        elif status in RUNNABLE_STATUSES:
            queued += 1
    done = success + failed
    percent = (done * 100.0) / float(total)
    return {
        "total_jobs": total,
        "done_jobs": done,
        "success_jobs": success,
        "failed_jobs": failed,
        "running_jobs": running,
        "queued_jobs": queued,
        "percent": round(percent, 2),
    }


def _run_cmd(cmd: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc


def _publish_progress(gw: SheetsGateway | None, snapshot: dict[str, Any]) -> None:
    print(f"queue_progress={json.dumps(snapshot, sort_keys=True, separators=(',', ':'))}")
    progress_sheet_id = os.environ.get("PROGRESS_SHEET_ID", "").strip()
    progress_range = os.environ.get("PROGRESS_RANGE", "").strip()
    if gw is not None and progress_sheet_id and progress_range:
        gw.write(progress_sheet_id, [{"range": progress_range, "values": [[snapshot["percent"]]]}])


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger compile + render batch once with queue progress tracking.")
    parser.add_argument("--poll-seconds", type=float, default=1.0, help="Delay between queue status refreshes.")
    parser.add_argument("--max-rounds", type=int, default=20, help="Safety bound for retry loops.")
    args = parser.parse_args()

    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", DEFAULT_BASE).strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    gw = SheetsGateway(base_url=base_url, api_key=api_key)

    lock_fh = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"batch_busy=true reason=local_lock lock_file={LOCK_FILE}")
        return 2

    try:
        current_rows = _read_jobs(gw, control_sheet_id)
        if is_process_running(current_rows):
            print("batch_busy=true reason=render_jobs_running")
            return 2

        env = os.environ.copy()
        compile_proc = _run_cmd(
            [sys.executable, "scripts/compile_config_to_render_job.py"],
            env=env,
        )
        if compile_proc.returncode != 0:
            print(f"batch_error=compile_failed code={compile_proc.returncode}")
            return compile_proc.returncode

        target_job_ids = parse_queued_job_ids(compile_proc.stdout or "")
        if not target_job_ids:
            print("batch_noop=true reason=no_queued_jobs")
            return 0

        rows = _read_jobs(gw, control_sheet_id)
        _publish_progress(gw, compute_queue_progress(rows, target_job_ids))

        rounds = 0
        while rounds < max(1, int(args.max_rounds)):
            rounds += 1
            rows = _read_jobs(gw, control_sheet_id)
            status_by_id = {
                str(row.get("render_job_id") or "").strip(): str(row.get("status") or "").strip().lower()
                for row in rows
            }
            runnable = [job_id for job_id in target_job_ids if status_by_id.get(job_id, "") in RUNNABLE_STATUSES]
            if not runnable:
                break
            for job_id in runnable:
                run_env = dict(env)
                run_env["JOB_ID"] = job_id
                run_proc = _run_cmd([sys.executable, "scripts/render_agent.py", "run-once"], env=run_env)
                if run_proc.returncode not in {0, 1}:
                    print(f"batch_error=run_once_unexpected_rc job_id={job_id} code={run_proc.returncode}")
                    return run_proc.returncode
                rows = _read_jobs(gw, control_sheet_id)
                _publish_progress(gw, compute_queue_progress(rows, target_job_ids))
                time.sleep(max(float(args.poll_seconds), 0.0))

        rows = _read_jobs(gw, control_sheet_id)
        final_snapshot = compute_queue_progress(rows, target_job_ids)
        _publish_progress(gw, final_snapshot)
        if final_snapshot["done_jobs"] < final_snapshot["total_jobs"]:
            print(
                "batch_error=incomplete "
                f"done={final_snapshot['done_jobs']} total={final_snapshot['total_jobs']} max_rounds={args.max_rounds}"
            )
            return 1
        return 0
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())

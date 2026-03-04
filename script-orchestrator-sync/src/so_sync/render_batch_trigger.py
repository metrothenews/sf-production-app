from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIGGER_SCRIPT = REPO_ROOT / "scripts" / "trigger_render_batch.py"
QUEUE_PROGRESS_PREFIX = "queue_progress="
QUEUED_JOB_PREFIX = "queued_render_job_id="


def parse_queue_progress_snapshots(output: str) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for line in str(output or "").splitlines():
        text = line.strip()
        if not text.startswith(QUEUE_PROGRESS_PREFIX):
            continue
        raw = text[len(QUEUE_PROGRESS_PREFIX) :].strip()
        try:
            decoded = json.loads(raw)
        except Exception:
            continue
        if isinstance(decoded, dict):
            snapshots.append(decoded)
    return snapshots


def parse_queued_job_ids(output: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in str(output or "").splitlines():
        m = re.match(r"^queued_render_job_id=([^\s]+)", line.strip())
        if not m:
            continue
        job_id = m.group(1).strip()
        if job_id and job_id not in seen:
            seen.add(job_id)
            out.append(job_id)
    return out


def _first_line_with_prefix(output: str, prefix: str) -> str:
    for line in str(output or "").splitlines():
        text = line.strip()
        if text.startswith(prefix):
            return text
    return ""


def _kv(line: str, key: str) -> str:
    pattern = rf"(?:^|\s){re.escape(key)}=([^\s]+)"
    m = re.search(pattern, str(line or ""))
    if not m:
        return ""
    return m.group(1).strip()


def summarize_completed_run(return_code: int, output: str) -> dict[str, Any]:
    snapshots = parse_queue_progress_snapshots(output)
    final_progress = snapshots[-1] if snapshots else None
    queued_job_ids = parse_queued_job_ids(output)

    busy_line = _first_line_with_prefix(output, "batch_busy=true")
    if busy_line:
        return {
            "status": "busy",
            "return_code": int(return_code),
            "busy_reason": _kv(busy_line, "reason") or "unknown",
            "noop_reason": None,
            "error": None,
            "queued_job_ids": queued_job_ids,
            "final_progress": final_progress,
        }

    noop_line = _first_line_with_prefix(output, "batch_noop=true")
    if noop_line:
        return {
            "status": "noop",
            "return_code": int(return_code),
            "busy_reason": None,
            "noop_reason": _kv(noop_line, "reason") or "unknown",
            "error": None,
            "queued_job_ids": queued_job_ids,
            "final_progress": final_progress,
        }

    error_line = _first_line_with_prefix(output, "batch_error=")
    if return_code != 0 or error_line:
        err = error_line or f"batch exited non-zero (code={return_code})"
        return {
            "status": "error",
            "return_code": int(return_code),
            "busy_reason": None,
            "noop_reason": None,
            "error": err,
            "queued_job_ids": queued_job_ids,
            "final_progress": final_progress,
        }

    return {
        "status": "success",
        "return_code": int(return_code),
        "busy_reason": None,
        "noop_reason": None,
        "error": None,
        "queued_job_ids": queued_job_ids,
        "final_progress": final_progress,
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def start_render_batch_process(
    control_sheet_id: str,
    *,
    poll_seconds: float | None = None,
    max_rounds: int | None = None,
    startup_wait_seconds: float = 0.75,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    control_id = str(control_sheet_id or "").strip()
    if not control_id:
        raise ValueError("control_sheet_id is required")
    if not TRIGGER_SCRIPT.exists():
        raise RuntimeError(f"Missing trigger script: {TRIGGER_SCRIPT}")

    env = os.environ.copy()
    env["CONTROL_SHEET_ID"] = control_id
    for k, v in (extra_env or {}).items():
        key = str(k).strip()
        if not key:
            continue
        env[key] = str(v)

    cmd = [sys.executable, str(TRIGGER_SCRIPT)]
    if poll_seconds is not None:
        cmd.extend(["--poll-seconds", str(float(poll_seconds))])
    if max_rounds is not None:
        cmd.extend(["--max-rounds", str(int(max_rounds))])

    logs_dir = Path(tempfile.gettempdir()) / "so_render_batch_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"render_batch_{time.time_ns()}.log"

    with log_path.open("w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )

    deadline = time.monotonic() + max(0.0, float(startup_wait_seconds))
    rc = proc.poll()
    while rc is None and time.monotonic() < deadline:
        time.sleep(0.05)
        rc = proc.poll()

    if rc is None:
        return {
            "status": "started",
            "pid": int(proc.pid),
            "return_code": None,
            "busy_reason": None,
            "noop_reason": None,
            "error": None,
            "queued_job_ids": [],
            "final_progress": None,
            "log_path": str(log_path),
        }

    output = _read_text(log_path)
    result = summarize_completed_run(int(rc), output)
    result["pid"] = int(proc.pid)
    result["log_path"] = str(log_path)
    return result

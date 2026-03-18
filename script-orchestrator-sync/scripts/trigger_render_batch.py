#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway  # noqa: E402
from so_sync.render_repo import read_render_jobs_all, upsert_render_job  # noqa: E402
from so_sync.repositories import normalize_enabled_value  # noqa: E402

DEFAULT_BASE = "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"
LOCK_FILE = Path(tempfile.gettempdir()) / "so_render_batch_trigger.lock"
ACTIVE_PROCESS_STATUSES = {"leased", "running"}
RUNNABLE_STATUSES = {"queued", "retry"}
DEFAULT_ACTIVE_STALE_SECONDS = 1800.0


def _must_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _read_jobs(gw: SheetsGateway, control_sheet_id: str) -> list[dict[str, Any]]:
    return read_render_jobs_all(gw, control_sheet_id)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_utc(raw: Any) -> dt.datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _to_float(raw: Any, default: float) -> float:
    try:
        return float(str(raw).strip())
    except Exception:
        return float(default)


def _auto_recover_stale_active_enabled() -> bool:
    return str(os.environ.get("AUTO_RECOVER_STALE_ACTIVE_JOBS", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _active_stale_seconds() -> float:
    return max(0.0, _to_float(os.environ.get("RENDER_ACTIVE_STALE_SECONDS", str(DEFAULT_ACTIVE_STALE_SECONDS)), DEFAULT_ACTIVE_STALE_SECONDS))


def _is_stale_active_job(row: dict[str, Any], *, now_utc: dt.datetime, stale_seconds: float) -> bool:
    if stale_seconds <= 0:
        return False
    if not normalize_enabled_value(row.get("enabled")):
        return False
    status = str(row.get("status") or "").strip().lower()
    if status not in ACTIVE_PROCESS_STATUSES:
        return False

    updated = _parse_utc(row.get("updated_utc"))
    lease_until = _parse_utc(row.get("lease_until_utc"))
    age_s = ((now_utc - updated).total_seconds() if updated is not None else None)

    if status == "leased":
        if lease_until is not None and lease_until <= now_utc:
            return True
        if age_s is None:
            return True
        return age_s >= stale_seconds

    # running
    if age_s is None:
        return False
    return age_s >= stale_seconds


def find_stale_active_jobs(rows: Iterable[dict[str, Any]], *, now_utc: dt.datetime, stale_seconds: float) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if _is_stale_active_job(row, now_utc=now_utc, stale_seconds=stale_seconds)]


def _recover_stale_active_jobs(
    gw: SheetsGateway,
    control_sheet_id: str,
    rows: Iterable[dict[str, Any]],
    *,
    now_utc: dt.datetime,
    stale_seconds: float,
) -> int:
    stale_rows = find_stale_active_jobs(rows, now_utc=now_utc, stale_seconds=stale_seconds)
    recovered = 0
    stamp = now_utc.isoformat()
    for row in stale_rows:
        job_id = str(row.get("render_job_id") or "").strip()
        if not job_id:
            continue
        updated = dict(row)
        prev_status = str(updated.get("status") or "").strip().lower()
        updated["status"] = "retry"
        updated["lease_owner"] = ""
        updated["lease_until_utc"] = ""
        updated["updated_utc"] = stamp
        marker = (
            "auto_recovered_stale_active "
            f"previous_status={prev_status} stale_seconds={stale_seconds:.1f} at={stamp}"
        )
        prior_error = str(updated.get("error_msg") or "").strip()
        updated["error_msg"] = marker if not prior_error else f"{prior_error} | {marker}"
        upsert_render_job(gw, control_sheet_id, updated)
        recovered += 1
        print(f"batch_recovered_stale_job render_job_id={job_id} previous_status={prev_status}")
    return recovered


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


def _emit_preflight(name: str, ok: bool, **details: Any) -> None:
    payload = {"check": name, "ok": bool(ok), **details}
    print(f"preflight_check={json.dumps(payload, sort_keys=True, separators=(',', ':'))}")


def _run_preflight(gw: SheetsGateway, control_sheet_id: str) -> int:
    failed = 0
    ffmpeg_bin = os.environ.get("FFMPEG_BIN", "").strip() or "ffmpeg"
    ffprobe_bin = os.environ.get("FFPROBE_BIN", "").strip() or "ffprobe"
    blender_bin = os.environ.get("BLENDER_BIN", "").strip() or "/Applications/Blender.app/Contents/MacOS/Blender"

    ffmpeg_ok = shutil.which(ffmpeg_bin) is not None
    _emit_preflight("ffmpeg_present", ffmpeg_ok, binary=ffmpeg_bin)
    failed += 0 if ffmpeg_ok else 1

    ffprobe_ok = shutil.which(ffprobe_bin) is not None
    _emit_preflight("ffprobe_present", ffprobe_ok, binary=ffprobe_bin)
    failed += 0 if ffprobe_ok else 1

    blender_ok = Path(blender_bin).exists() or (shutil.which(blender_bin) is not None)
    _emit_preflight("blender_present", blender_ok, binary=blender_bin)
    failed += 0 if blender_ok else 1

    tmp_dir = Path(tempfile.gettempdir())
    usage = shutil.disk_usage(tmp_dir)
    free_gb = usage.free / float(1024 ** 3)
    disk_ok = free_gb >= 5.0
    _emit_preflight("disk_headroom", disk_ok, temp_dir=str(tmp_dir), free_gb=round(free_gb, 2), threshold_gb=5.0)
    failed += 0 if disk_ok else 1

    flags = {
        "PIPELINE_PER_CUT_RENDER": os.environ.get("PIPELINE_PER_CUT_RENDER", "1"),
        "PIPELINE_EFFECTS_PASS2_ENABLED": os.environ.get("PIPELINE_EFFECTS_PASS2_ENABLED", "1"),
        "PIPELINE_EFFECTS_PASS2_STRICT": os.environ.get("PIPELINE_EFFECTS_PASS2_STRICT", "1"),
        "PIPELINE_OBSERVABILITY_MODE": os.environ.get("PIPELINE_OBSERVABILITY_MODE", "deep"),
        "PIPELINE_JSON_EVENTS_ENABLED": os.environ.get("PIPELINE_JSON_EVENTS_ENABLED", "1"),
    }
    _emit_preflight("env_flags", True, **flags)

    rows = _read_jobs(gw, control_sheet_id)
    queued = [
        str(r.get("render_job_id") or "").strip()
        for r in rows
        if normalize_enabled_value(r.get("enabled"))
        and str(r.get("status") or "").strip().lower() in RUNNABLE_STATUSES
    ]
    _emit_preflight("queued_jobs", len(queued) > 0, queued_count=len(queued), sample=queued[:5])
    failed += 0 if queued else 1

    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger compile + render batch once with queue progress tracking.")
    parser.add_argument("--poll-seconds", type=float, default=1.0, help="Delay between queue status refreshes.")
    parser.add_argument("--max-rounds", type=int, default=20, help="Safety bound for retry loops.")
    parser.add_argument("--preflight-only", action="store_true", help="Run preflight checks and exit.")
    args = parser.parse_args()

    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", DEFAULT_BASE).strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    gw = SheetsGateway(base_url=base_url, api_key=api_key)
    if args.preflight_only:
        return _run_preflight(gw, control_sheet_id)

    lock_fh = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"batch_busy=true reason=local_lock lock_file={LOCK_FILE}")
        return 2

    try:
        current_rows = _read_jobs(gw, control_sheet_id)
        if is_process_running(current_rows):
            recovered = 0
            stale_seconds = _active_stale_seconds()
            if _auto_recover_stale_active_enabled() and stale_seconds > 0.0:
                recovered = _recover_stale_active_jobs(
                    gw,
                    control_sheet_id,
                    current_rows,
                    now_utc=_utc_now(),
                    stale_seconds=stale_seconds,
                )
                if recovered > 0:
                    current_rows = _read_jobs(gw, control_sheet_id)
            if is_process_running(current_rows):
                print(f"batch_busy=true reason=render_jobs_running recovered={recovered}")
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

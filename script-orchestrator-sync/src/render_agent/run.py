from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from so_sync.gateway import SheetsGateway
from so_sync.render_contract import (
    RENDER_CANONICAL_HEADERS,
    RENDER_HEADER_ALIASES,
    RENDER_JOBS_TAB,
    RENDER_STATUS_QUEUED,
    RENDER_STATUS_RETRY,
)
from so_sync.render_leaser import lease_one_render_job
from so_sync.render_leaser import mark_failed, mark_running, mark_success
from so_sync.render_repo import append_outputs_index_rows, find_render_job_by_id
from so_sync.repositories import normalize_enabled_value
from so_sync.sheet_contract import build_header_index, row_to_canonical_dict

from .auth_diag import describe_google_auth
from .drive_listing import ensure_folder_readable, expand_source_pool, normalize_sources
from .drive_proxy import DriveProxyClient, DriveProxyError
from .drive_uploader import DEFAULT_OUTPUT_FOLDER_ID, ensure_folder_accessible, upload_file_to_folder
from .job_contract import parse_select_mode, parse_select_seed, parse_target_count, resolve_output_folder_id
from .select_k import select_k
from so_sync.drive_upload import from_env as drive_upload_from_env


def _must_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _base_url() -> str:
    return os.environ.get("SHEETS_JSON_BASE_URL", "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app").strip().rstrip("/")


def _gateway() -> SheetsGateway:
    return SheetsGateway(
        base_url=_base_url(),
        api_key=os.environ.get("SHEETS_JSON_API_KEY", "").strip(),
    )


OPTIONAL_JOB_FIELDS = [
    "target_count",
    "source_folder_ids",
    "source_file_ids",
    "select_mode",
    "select_seed",
    "output_folder_id",
]

OPTIONAL_ALIASES = {
    "target_count": ["n", "target count", "clip count", "number of outputs", "number_of_outputs"],
    "source_folder_ids": ["source folder ids", "folders", "drive folder ids"],
    "source_file_ids": ["source file ids", "files", "drive file ids"],
    "select_mode": ["select mode"],
    "select_seed": ["select seed"],
    "output_folder_id": ["output folder id"],
}


def _quote_tab(tab_name: str) -> str:
    escaped = str(tab_name).replace("'", "''")
    return f"'{escaped}'"


def _read_render_jobs_rows() -> list[dict[str, Any]]:
    gw = _gateway()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    resp = gw.read(control_sheet_id, [f"{_quote_tab(RENDER_JOBS_TAB)}!A1:ZZ"])
    values = (resp.get("valueRanges") or [{}])[0].get("values", []) or []
    if not values:
        return []
    header = values[0]
    raw_rows = values[1:]
    aliases = dict(RENDER_HEADER_ALIASES)
    aliases.update(OPTIONAL_ALIASES)
    header_index = build_header_index(header, aliases=aliases)
    keys = list(RENDER_CANONICAL_HEADERS[RENDER_JOBS_TAB]) + OPTIONAL_JOB_FIELDS
    out: list[dict[str, Any]] = []
    for raw in raw_rows:
        out.append(row_to_canonical_dict(raw, canonical_headers=keys, header_index=header_index))
    return out


def read_jobs_to_run(*, job_id: str = "", limit: int = 0) -> list[dict[str, Any]]:
    rows = _read_render_jobs_rows()
    rows = [
        r
        for r in rows
        if normalize_enabled_value(r.get("enabled"))
        and str(r.get("status") or "").strip().lower() in {RENDER_STATUS_QUEUED, RENDER_STATUS_RETRY}
    ]
    if job_id:
        rows = [r for r in rows if str(r.get("render_job_id") or "").strip() == job_id]
    if limit > 0:
        rows = rows[:limit]
    return rows


def mark_job_running(job_id: str, run_id: str, started_utc: str) -> None:
    gw = _gateway()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    mark_running(gw, control_sheet_id, job_id, run_id, now_utc=started_utc)


def mark_job_success(job_id: str, run_id: str, outputs: list[dict[str, Any]], finished_utc: str) -> None:
    gw = _gateway()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    mark_success(gw, control_sheet_id, job_id, run_id, now_utc=finished_utc)
    rows = []
    for out in outputs:
        file_id = str(out.get("file_id") or "")
        name = str(out.get("name") or "")
        rows.append(
            {
                "output_id": hashlib.sha256(f"{run_id}:{file_id}:{name}".encode("utf-8")).hexdigest()[:32],
                "run_id": run_id,
                "render_job_id": job_id,
                "campaign_id": "",
                "artifact_type": "final_output",
                "drive_file_id": file_id,
                "drive_folder_id": str(out.get("folder_id") or ""),
                "filename": name,
                "bytes": "",
                "sha256": "",
                "created_utc": finished_utc,
                "notes": str(out.get("webViewLink") or ""),
            }
        )
    if rows:
        append_outputs_index_rows(gw, control_sheet_id, rows)


def mark_job_failed(job_id: str, run_id: str, error: str, finished_utc: str) -> None:
    gw = _gateway()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    mark_failed(gw, control_sheet_id, job_id, run_id, error, now_utc=finished_utc)


def _run_once_subprocess(*, job_id: str = "") -> int:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    if job_id:
        env["JOB_ID"] = job_id
    cmd = [sys.executable, str(repo_root / "scripts" / "render_agent.py"), "run-once"]
    proc = subprocess.run(cmd, env=env, cwd=str(repo_root), check=False)
    return int(proc.returncode)


def _drive_api_error(context: str, exc: Exception) -> RuntimeError:
    diag = describe_google_auth()
    hint = (
        "Run: gcloud auth application-default login; "
        "or set GOOGLE_APPLICATION_CREDENTIALS to a Drive-enabled service account key JSON; "
        "if invalid_grant persists: delete ~/.config/gcloud/application_default_credentials.json and re-login."
    )
    return RuntimeError(f"{context} error={exc} auth_diag={json.dumps(diag, sort_keys=True)} next_steps={hint}")


def _safe_name(name: str) -> str:
    return "".join(c if (c.isalnum() or c in {"-", "_", "."}) else "_" for c in str(name))


def _write_inputs_manifest(run_dir: Path, manifest_rows: list[dict[str, Any]]) -> str:
    path = run_dir / "inputs_manifest.json"
    path.write_text(json.dumps(manifest_rows, indent=2), encoding="utf-8")
    return str(path)


def _download_selected(
    selected_ids: list[str],
    run_dir: Path,
    *,
    names_by_id: dict[str, str] | None = None,
) -> tuple[list[str], str]:
    inputs_dir = run_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    names = names_by_id or {}
    proxy = DriveProxyClient()
    fallback_enabled = os.environ.get("DRIVE_PROXY_FALLBACK_TO_DRIVE_API", "0").strip() == "1"
    fallback_on_404 = os.environ.get("DRIVE_PROXY_FALLBACK_ON_404", "0").strip() == "1"
    upload_client = drive_upload_from_env() if fallback_enabled else None
    paths: list[str] = []
    manifest_rows: list[dict[str, Any]] = []
    for i, file_id in enumerate(selected_ids, start=1):
        name = names.get(file_id) or file_id
        filename = f"{i:03d}_{file_id}_{_safe_name(name)}"
        dst = str(inputs_dir / filename)
        try:
            proxy.download_file(file_id, dst)
        except DriveProxyError as exc:
            msg = str(exc)
            is_404 = "status=404" in msg
            if not fallback_enabled or (is_404 and not fallback_on_404):
                raise
            if upload_client is None:
                raise
            print(f"drive_download_fallback=drive_api file_id={file_id} reason={msg[:80]}")
            try:
                upload_client.download_file(file_id, dst)
            except Exception as up_exc:
                raise _drive_api_error("drive_api_download_fallback_failed", up_exc) from up_exc
        paths.append(dst)
        manifest_rows.append({"file_id": file_id, "name": name, "local_path": dst})
    return paths, _write_inputs_manifest(run_dir, manifest_rows)


def render_outputs(job: dict[str, Any], input_paths: list[str], work_dir: Path) -> list[str]:
    if not input_paths:
        raise RuntimeError("No input files selected for render")
    outputs_dir = work_dir / "outputs"
    logs_dir = work_dir / "logs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    final_out = outputs_dir / "final.mp4"
    render_log = logs_dir / "render.log"
    cmd_tpl = str(job.get("renderer_cmd") or "").strip()
    if cmd_tpl:
        cmd = cmd_tpl.format(
            input_path=input_paths[0],
            output_path=str(final_out),
            work_dir=str(work_dir),
        )
        with render_log.open("wb") as f:
            proc = subprocess.run(cmd, shell=True, stdout=f, stderr=subprocess.STDOUT, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"render command failed exit_code={proc.returncode} log={render_log}")
    else:
        # Dev fast path: use first selected clip as final output when no explicit renderer command is provided.
        shutil.copy2(input_paths[0], final_out)
        render_log.write_text("render_mode=copy_first_input\n", encoding="utf-8")
    return [str(final_out)]


def _acquire_job(*, job_id: str = "", no_lease: bool = False) -> dict[str, Any] | None:
    rows = _read_render_jobs_rows()
    if job_id and no_lease:
        for row in rows:
            if str(row.get("render_job_id") or "").strip() == job_id:
                return row
        return None
    gw = _gateway()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    now = _utc_now_iso()
    agent_id = os.environ.get("AGENT_ID", socket.gethostname()).strip() or "render-agent"
    lease_seconds = int(os.environ.get("LEASE_SECONDS", "900"))
    if job_id:
        os.environ["JOB_ID"] = job_id
    leased = lease_one_render_job(gw, control_sheet_id, now, lease_seconds, agent_id)
    if job_id:
        os.environ.pop("JOB_ID", None)
    return leased


def _run_selected_job(job: dict[str, Any], *, leased: bool) -> int:
    job_id = str(job.get("render_job_id") or "").strip()
    if not job_id:
        raise RuntimeError("render_job_id missing")
    run_id = uuid.uuid4().hex[:32]
    started = _utc_now_iso()
    if leased:
        mark_job_running(job_id, run_id, started)
    folder_ids, file_ids = normalize_sources(
        {
            "source_folder_ids": job.get("source_folder_ids") or job.get("input_drive_folder_id") or "",
            "source_file_ids": job.get("source_file_ids") or "",
        }
    )
    allow_no_listing = os.environ.get("ALLOW_NO_LISTING", "0").strip() == "1"
    listing_required = bool(folder_ids)
    if listing_required:
        for folder_id in folder_ids:
            try:
                ensure_folder_readable(folder_id)
            except Exception as exc:
                if allow_no_listing and file_ids:
                    print(
                        json.dumps(
                            {
                                "event": "listing_preflight_fallback",
                                "folder_id": folder_id,
                                "reason": str(exc),
                            },
                            sort_keys=True,
                        )
                    )
                    listing_required = False
                    break
                raise _drive_api_error(
                    "source folder read/list check failed; local agent identity must have read/list access",
                    exc,
                ) from exc

    try:
        pool = expand_source_pool(folder_ids if listing_required else [], file_ids)
    except Exception as exc:
        if allow_no_listing and file_ids:
            print(
                json.dumps(
                    {
                        "event": "source_pool_fallback_to_file_ids_only",
                        "reason": str(exc),
                    },
                    sort_keys=True,
                )
            )
            pool = list(file_ids)
        else:
            raise _drive_api_error("drive listing failed", exc) from exc
    target_count = parse_target_count(job.get("target_count"))
    if target_count <= 0 and pool:
        target_count = min(len(pool), max(1, int(os.environ.get("DEFAULT_TARGET_COUNT", "1"))))
    mode = parse_select_mode(job.get("select_mode"))
    seed = parse_select_seed(job.get("select_seed"))
    selected = select_k(pool, target_count, mode, seed)
    run_dir = Path("work") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    input_paths, _ = _download_selected(selected, run_dir)
    outputs = render_outputs(job, input_paths, run_dir)
    output_folder_id = resolve_output_folder_id(job.get("output_folder_id") or job.get("output_drive_folder_id") or "")
    output_folder_id = output_folder_id or DEFAULT_OUTPUT_FOLDER_ID
    try:
        ensure_folder_accessible(output_folder_id)
    except Exception as exc:
        raise _drive_api_error(
            "output folder not writable by local identity (this is NOT proxy SA)",
            exc,
        ) from exc
    uploaded: list[dict[str, Any]] = []
    for out_path in outputs:
        meta = upload_file_to_folder(out_path, output_folder_id)
        meta["folder_id"] = output_folder_id
        uploaded.append(meta)
    finished = _utc_now_iso()
    if leased:
        mark_job_success(job_id, run_id, uploaded, finished)
    print(
        json.dumps(
            {
                "job_id": job_id,
                "run_id": run_id,
                "selected_count": len(selected),
                "uploaded": uploaded,
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local render agent end-to-end loop")
    parser.add_argument("--limit", type=int, default=1, help="Number of run-once cycles to execute")
    parser.add_argument("--dry-run", action="store_true", help="List runnable jobs and exit")
    parser.add_argument("--job-id", default="", help="Run only this render_job_id")
    parser.add_argument("--smoke", action="store_true", help="Alias for --limit 1")
    parser.add_argument("--once", action="store_true", help="Run at most one job and exit")
    parser.add_argument("--no-lease", action="store_true", help="With --job-id, run job without leasing")
    args = parser.parse_args()

    limit = 1 if (args.smoke or args.once) else max(1, int(args.limit))
    if args.dry_run:
        jobs = read_jobs_to_run(job_id=args.job_id, limit=limit)
        allow_no_listing = os.environ.get("ALLOW_NO_LISTING", "0").strip() == "1"
        for job in jobs:
            folder_ids, file_ids = normalize_sources(
                {
                    "source_folder_ids": job.get("source_folder_ids") or job.get("input_drive_folder_id") or "",
                    "source_file_ids": job.get("source_file_ids"),
                }
            )
            target_count = parse_target_count(job.get("target_count"))
            mode = parse_select_mode(job.get("select_mode"))
            seed = parse_select_seed(job.get("select_seed"))
            output_folder_id = resolve_output_folder_id(job.get("output_folder_id"))
            try:
                pool = expand_source_pool(folder_ids, file_ids)
            except Exception as exc:
                if allow_no_listing and file_ids:
                    pool = list(file_ids)
                    print(
                        json.dumps(
                            {
                                "event": "dry_run_pool_fallback_to_file_ids_only",
                                "job_id": str(job.get("render_job_id") or ""),
                                "reason": str(exc),
                            },
                            sort_keys=True,
                        )
                    )
                else:
                    raise _drive_api_error("dry-run source pool expansion failed", exc) from exc
            if target_count <= 0 and pool:
                target_count = min(len(pool), max(1, int(os.environ.get("DEFAULT_TARGET_COUNT", "1"))))
            selected = select_k(pool, target_count, mode, seed)
            print(
                json.dumps(
                    {
                        "job_id": str(job.get("render_job_id") or ""),
                        "target_count": target_count,
                        "pool_size": len(pool),
                        "selected_count": len(selected),
                        "selected_file_ids": selected,
                        "output_folder_id": output_folder_id,
                    },
                    sort_keys=True,
                )
            )
        print(json.dumps({"count": len(jobs), "agent_id": os.environ.get("AGENT_ID", socket.gethostname()).strip() or "render-agent"}, sort_keys=True))
        return 0

    rc = 0
    for _ in range(limit):
        job = _acquire_job(job_id=args.job_id, no_lease=bool(args.no_lease))
        if not job:
            print(json.dumps({"event": "no_job_acquired", "job_id": args.job_id}, sort_keys=True))
            return 0
        leased = not bool(args.no_lease and args.job_id)
        try:
            rc = _run_selected_job(job, leased=leased)
        except Exception as exc:
            run_id = uuid.uuid4().hex[:32]
            if leased:
                mark_job_failed(str(job.get("render_job_id") or ""), run_id, str(exc), _utc_now_iso())
            print(f"Render run failed: run_id={run_id} render_job_id={job.get('render_job_id')} error={exc}", file=sys.stderr)
            return 1
        if rc != 0:
            return rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

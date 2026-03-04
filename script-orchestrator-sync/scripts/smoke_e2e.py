#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List

# Allow importing from ../src without installing a package
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway, SheetsGatewayError  # noqa: E402
from so_sync.repositories import (  # noqa: E402
    find_run_by_id,
    read_mapping,
    read_sync_jobs_all,
    select_job_from_sync_jobs,
    _read_tab as repo_read_tab,
)
from so_sync.sheet_contract import pad_row  # noqa: E402
from so_sync.sync_engine import execute_job  # noqa: E402

DEFAULT_BASE = "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"
ALLOWLIST_HINT = (
    "If this is an allowlist/share issue: add the sheet id to SHEETS_ALLOWLIST and share the sheet with "
    "sheets-db-service@latest-ave-sheets-01.iam.gserviceaccount.com."
)


class RecordingGateway:
    def __init__(self, inner: SheetsGateway):
        self.inner = inner
        self.read_calls: List[Dict[str, Any]] = []
        self.write_calls: List[Dict[str, Any]] = []

    def read(self, sheet_id: str, ranges: List[str], **kwargs: Any) -> Dict[str, Any]:
        self.read_calls.append({"sheet_id": sheet_id, "ranges": ranges})
        return self.inner.read(sheet_id, ranges, **kwargs)

    def write(self, sheet_id: str, updates: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        self.write_calls.append({"sheet_id": sheet_id, "updates": updates})
        return self.inner.write(sheet_id, updates, **kwargs)


def _must_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _status_transitions_ok(history: List[str]) -> bool:
    return history[:2] == ["queued", "running"] and history[-1] in {"success", "failed"}


def _norm(v: Any) -> str:
    return "" if v is None else str(v)


def _extract_first_values_matrix(data: Dict[str, Any]) -> List[List[Any]]:
    if "valueRanges" in data:
        ranges = data.get("valueRanges") or []
        if not ranges:
            return []
        return (ranges[0] or {}).get("values", []) or []
    return data.get("values", []) or []


def _assert_target_write(gateway: RecordingGateway, target_sheet_id: str, target_header: List[str]) -> None:
    target_writes = [w for w in gateway.write_calls if w["sheet_id"] == target_sheet_id]
    if not target_writes:
        raise AssertionError("No target-sheet write calls were recorded")

    updates = [u for w in target_writes for u in w["updates"]]
    for update in updates:
        for row in update.get("values", []):
            if len(row) != len(target_header):
                raise AssertionError(
                    f"Written row length {len(row)} does not match target header length {len(target_header)} "
                    f"for range {update.get('range')}"
                )

    first = updates[0]
    expected = first["values"][0]
    read_back = gateway.read(target_sheet_id, [first["range"]])
    read_rows = _extract_first_values_matrix(read_back)
    if not read_rows:
        raise AssertionError(f"Read-back returned no rows for target range {first['range']}")
    actual = pad_row(read_rows[0], len(target_header))
    if [_norm(x) for x in actual] != [_norm(x) for x in expected]:
        raise AssertionError(
            f"Read-back mismatch at {first['range']}. expected={expected} actual={actual}"
        )


def _target_write_ranges(gateway: RecordingGateway, target_sheet_id: str) -> List[str]:
    target_writes = [w for w in gateway.write_calls if w["sheet_id"] == target_sheet_id]
    return [str(u.get("range")) for w in target_writes for u in w["updates"] if u.get("range")]


def _count_target_rows(gateway: RecordingGateway, target_sheet_id: str, target_tab_name: str, has_alias: bool) -> int:
    _, target_rows = repo_read_tab(gateway, target_sheet_id, target_tab_name, has_alias)
    return len(target_rows)


def _print_debug_config(job: Dict[str, Any], mappings_count: int) -> None:
    payload = {
        "job_id": str(job.get("job_id") or ""),
        "enabled": str(job.get("enabled") or ""),
        "source_spreadsheet_id": str(job.get("source_spreadsheet_id") or ""),
        "target_spreadsheet_id": str(job.get("target_spreadsheet_id") or ""),
        "source_tab_name": str(job.get("source_tab_name") or ""),
        "target_tab_name": str(job.get("target_tab_name") or ""),
        "source_range": str(job.get("source_range") or ""),
        "target_range": str(job.get("target_range") or ""),
        "has_alias_row": str(job.get("has_alias_row") or ""),
        "write_mode": str(job.get("write_mode") or ""),
        "mappings_count": mappings_count,
    }
    print(f"DEBUG_CONFIG {json.dumps(payload, sort_keys=True, separators=(',', ':'))}")


def _print_last_sheet_context(gateway: RecordingGateway) -> None:
    if gateway.read_calls:
        last_read = gateway.read_calls[-1]
        print(
            f"Last read context: sheet_id={last_read['sheet_id']} ranges={last_read['ranges']}",
            file=sys.stderr,
        )
    if gateway.write_calls:
        last_write = gateway.write_calls[-1]
        ranges = [u.get("range") for u in last_write["updates"]]
        print(
            f"Last write context: sheet_id={last_write['sheet_id']} ranges={ranges}",
            file=sys.stderr,
        )


def main() -> int:
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", DEFAULT_BASE).strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    requested_job_id = os.environ.get("JOB_ID", "").strip() or None
    dry_run = os.environ.get("DRY_RUN", "0").strip() == "1"
    debug_dump = os.environ.get("DEBUG_DUMP", "0").strip() == "1"
    idempotency_verify = os.environ.get("IDEMPOTENCY_VERIFY", "0").strip() == "1"

    gw = RecordingGateway(SheetsGateway(base_url=base_url, api_key=api_key))

    try:
        jobs = read_sync_jobs_all(gw, control_sheet_id)
        job, selected_from = select_job_from_sync_jobs(jobs, job_id=requested_job_id)
        job_id = str(job.get("job_id") or "").strip()
        print(f"Selected job_id={job_id} source={selected_from}")

        target_sheet_id = str(job.get("target_spreadsheet_id") or "").strip()
        if not target_sheet_id:
            raise RuntimeError(f"JOB_ID '{job_id}' has empty target_spreadsheet_id in Sync_Jobs")

        dedupe_key = os.environ.get("DEDUPE_KEY", "").strip() or f"smoke-{job_id}-{uuid.uuid4().hex[:10]}"

        has_alias = str(job.get("has_alias_row", "")).strip().lower() in {"true", "1", "yes"}
        target_tab_name = str(job["target_tab_name"])
        target_header, _ = repo_read_tab(gw, target_sheet_id, target_tab_name, has_alias)
        mappings = read_mapping(gw, control_sheet_id, job_id)
        if debug_dump:
            _print_debug_config(job, len(mappings))

        print(f"Smoke run start: base_url={base_url} control_sheet_id={control_sheet_id} job_id={job_id}")
        print(f"Target: sheet_id={target_sheet_id} tab={target_tab_name} dry_run={dry_run}")

        target_rows_before: int | None = None
        write_mode = str(job.get("write_mode") or "append").strip().lower()
        if not dry_run and idempotency_verify:
            if write_mode == "append":
                target_rows_before = _count_target_rows(gw, target_sheet_id, target_tab_name, has_alias)
                print(f"Idempotency baseline rows={target_rows_before}")
            else:
                print(
                    "Idempotency row-count check skipped: target range is not well-defined "
                    f"for write_mode={write_mode}."
                )

        run_entry = execute_job(
            gateway=gw,
            control_sheet_id=control_sheet_id,
            target_sheet_id=target_sheet_id,
            job_id=job_id,
            trigger="smoke_e2e",
            dedupe_key=dedupe_key,
            skip_target_write=dry_run,
        )

        run_id = str(run_entry.get("run_id") or "").strip()
        if not run_id:
            raise AssertionError("execute_job did not return run_id")
        run_row = find_run_by_id(gw, control_sheet_id, run_id)
        if not run_row:
            raise AssertionError(f"Run row not found in Runs_Ledger for run_id={run_id}")

        history = [str(x) for x in run_entry.get("status_history", [])]
        if not history:
            raise AssertionError("run_entry.status_history missing")
        if not _status_transitions_ok(history):
            raise AssertionError(f"Unexpected status transitions: {history}")

        if not run_row.get("source_fingerprint"):
            raise AssertionError("Runs_Ledger source_fingerprint was empty")
        if not run_row.get("dedupe_key"):
            raise AssertionError("Runs_Ledger dedupe_key was empty")
        if str(run_row.get("status")) not in {"success", "failed"}:
            raise AssertionError(f"Unexpected final status: {run_row.get('status')}")

        if str(run_row.get("status")) == "failed":
            raise RuntimeError(f"Run failed. error_msg={run_row.get('error_msg')}")

        if dry_run:
            target_writes = [w for w in gw.write_calls if w["sheet_id"] == target_sheet_id]
            if target_writes:
                raise AssertionError("DRY_RUN=1 was set but target-sheet writes were recorded")
        else:
            target_ranges = _target_write_ranges(gw, target_sheet_id)
            print(f"Target write ranges: {target_ranges}")
            if not idempotency_verify:
                _assert_target_write(gw, target_sheet_id, target_header)
            if target_rows_before is not None:
                target_rows_after = _count_target_rows(gw, target_sheet_id, target_tab_name, has_alias)
                print(f"Idempotency rows after={target_rows_after}")
                if target_rows_after > target_rows_before:
                    raise RuntimeError("Idempotency violated: duplicate target writes detected.")

        print(f"Smoke run success: run_id={run_id} status_history={history} rows_processed={run_row.get('rows_processed')}")
        return 0
    except SheetsGatewayError as e:
        print(f"Gateway error: {e}", file=sys.stderr)
        _print_last_sheet_context(gw)
        if e.response_body:
            print(f"Gateway response body: {e.response_body}", file=sys.stderr)
        if e.next_action:
            print(f"Next action: {e.next_action}", file=sys.stderr)
        else:
            print(f"Next action: {ALLOWLIST_HINT}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Smoke test failed: {e}", file=sys.stderr)
        _print_last_sheet_context(gw)
        print(f"Next action: {ALLOWLIST_HINT}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

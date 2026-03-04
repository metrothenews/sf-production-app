#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Allow importing from ../src without installing a package
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway  # type: ignore


SHEET_PROD = os.environ.get("SHEET_PROD", "YOUR_GOOGLE_SHEET_ID").strip()
SO_RUNS_TAB = os.environ.get("SO_RUNS_TAB", "SO Runs").strip()
SO_ENUMS_TAB = os.environ.get("SO_ENUMS_TAB", "SO Enums").strip()

BASE = os.environ.get(
    "SHEETS_JSON_BASE_URL",
    os.environ.get("SO_GATEWAY_BASE", "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"),
).strip().rstrip("/")
KEY = os.environ.get("SHEETS_JSON_API_KEY", os.environ.get("SO_GATEWAY_KEY", "")).strip()
APPLY = os.environ.get("APPLY", "0") == "1"
SMOKE = os.environ.get("SMOKE", "0") == "1"


def must(v: str, name: str) -> str:
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def qtab(tab: str) -> str:
    # Sheets A1 convention: quote tabs with spaces/special chars.
    # Safe to always quote.
    return f"'{tab}'"


def find_next_empty_row(values: List[List[Any]]) -> int:
    # values includes header row typically; Sheets omits trailing empties.
    # next row = len(values) + 1
    return len(values) + 1


def main() -> None:
    must(BASE, "SHEETS_JSON_BASE_URL or SO_GATEWAY_BASE")
    must(KEY, "SHEETS_JSON_API_KEY or SO_GATEWAY_KEY")

    gw = SheetsGateway(base_url=BASE, api_key=KEY)

    headers: List[str] = [
        # A) Identity + grouping
        "run_id","batch_id","run_index","campaign_id","created_utc",
        # B) State machine + leasing
        "status","attempt","lease_token","lease_owner","lease_expires_utc","last_heartbeat_utc",
        # C) Deterministic resolved inputs
        "audio_drive_file_id","srt_drive_file_id","bpm","stitch_preset_dsl","text_preset_name","fps","aspect","seed",
        "random_inpoints","max_unique_tries","max_segments_per_cut","bg_mode",
        # D) Found-footage sourcing (v1)
        "ff_manifest_drive_file_id","ff_folder_ids_csv",
        # E) Output naming + destination
        "output_name","output_drive_folder_id",
        # F) Stage 1 writebacks (agent)
        "stage1_status","stage1_started_utc","stage1_finished_utc","stage1_output_drive_file_id","stage1_log_drive_file_id",
        # G) Stage 2 writebacks (agent)
        "stage2_status","stage2_started_utc","stage2_finished_utc","final_output_drive_file_id","stage2_log_drive_file_id",
        # H) Diagnostics + provenance
        "error_stage","error_message","tool_versions_json",
        # I) Future expansion placeholders (v1 empty)
        "edit_mode","perf_manifest_drive_file_id","layer_preset_name","perf_alignment_mode","perf_mix_ratio","perf_injection_rules",
        # J) Robustness spine (minimal required now)
        "intent_hash","tracking_json","request_id","created_by","finalized_utc",
        "stitcher_script_path","text_script_path","blender_binary_path","cache_dir","clean_cache",
    ]
    if len(headers) != 56:
        raise RuntimeError(f"Expected 56 headers, got {len(headers)}")

    # 1) Write SO Runs header row
    runs_header_range = f"{qtab(SO_RUNS_TAB)}!A1:BD1"
    runs_header_update: Dict[str, Any] = {"range": runs_header_range, "values": [headers]}

    # 2) Append enums to next empty row (Field in col A, Allowed Values in col B)
    enums_read = gw.read(SHEET_PROD, [f"{qtab(SO_ENUMS_TAB)}!A1:B2000"])
    enum_values = enums_read["valueRanges"][0].get("values", [])
    next_row = find_next_empty_row(enum_values)

    enum_rows = [
        ["run_status", "QUEUED,LEASED,RUNNING,SUCCEEDED,FAILED,CANCELED"],
        ["stage_status", "PENDING,RUNNING,SUCCEEDED,FAILED,SKIPPED"],
        ["error_stage", "LEASE,VALIDATION,STAGE1,STAGE2"],
    ]
    enums_write_range = f"{qtab(SO_ENUMS_TAB)}!A{next_row}:B{next_row + len(enum_rows) - 1}"
    enums_update: Dict[str, Any] = {"range": enums_write_range, "values": enum_rows}

    if not APPLY:
        print("DRY RUN (set APPLY=1 to write):")
        print(" - would write headers:", runs_header_range)
        print(" - would append enums:", enums_write_range)
        return

    write_resp = gw.write(SHEET_PROD, [runs_header_update, enums_update])
    if not write_resp.get("ok"):
        raise RuntimeError(f"write failed: {write_resp}")

    # 3) Verify headers read back
    verify = gw.read(SHEET_PROD, [runs_header_range])
    got = verify["valueRanges"][0].get("values", [[]])[0]
    if got != headers:
        raise RuntimeError("Header mismatch after write.")
    print("OK: SO Runs headers match exactly (56 cols).")

    # 4) Verify enums present (search A:B)
    verify_enums = gw.read(SHEET_PROD, [f"{qtab(SO_ENUMS_TAB)}!A1:B3000"])
    vals = verify_enums["valueRanges"][0].get("values", [])
    want = {"run_status","stage_status","error_stage"}
    found = set()
    for r in vals:
        if r and str(r[0]).strip() in want:
            found.add(str(r[0]).strip())
    missing = sorted(want - found)
    if missing:
        raise RuntimeError(f"Missing enums after write: {missing}")
    print("OK: Enums present:", sorted(found))

    # 5) Optional smoke write row 2 (run_id + status)
    if SMOKE:
        # Find run_id/status columns in header
        run_id_col = headers.index("run_id")
        status_col = headers.index("status")

        def col_to_a1(col_idx_0: int) -> str:
            n = col_idx_0 + 1
            s = ""
            while n:
                n, r = divmod(n - 1, 26)
                s = chr(65 + r) + s
            return s

        row2_updates = [
            {"range": f"{qtab(SO_RUNS_TAB)}!{col_to_a1(run_id_col)}2", "values": [["smoke_run_001"]]},
            {"range": f"{qtab(SO_RUNS_TAB)}!{col_to_a1(status_col)}2", "values": [["QUEUED"]]},
        ]
        resp2 = gw.write(SHEET_PROD, row2_updates)
        if not resp2.get("ok"):
            raise RuntimeError(f"smoke write failed: {resp2}")
        check2 = gw.read(SHEET_PROD, [f"{qtab(SO_RUNS_TAB)}!A2:BD2"])
        print("OK: Smoke row 2 readback:", check2["valueRanges"][0].get("values", []))


if __name__ == "__main__":
    main()

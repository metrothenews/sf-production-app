#!/usr/bin/env python3
from __future__ import annotations

"""
Gateway contract reminder:
- POST /v1/read payload shape:  {"sheet_id": "...", "ranges": ["'Tab'!A1:Z9", ...]}
- POST /v1/write payload shape: {"sheet_id": "...", "updates": [{"range": "...", "values": [[...]]}, ...]}
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Allow importing from ../src without installing a package
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway, SheetsGatewayError  # noqa: E402
from so_sync.sheet_contract import (  # noqa: E402
    ACCESS_CONTROL_TAB,
    CANONICAL_HEADERS,
    MAPPING_TAB,
    RUNS_LEDGER_TAB,
    SYNC_JOBS_TAB,
    dict_to_row,
    pad_row,
)

DEFAULT_BASE = "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"
DEFAULT_JOB_ID = "default"
LEGACY_SAMPLE_JOB_ID = "sample_sync_intake_to_so"


def _must_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _quote_tab(tab_name: str) -> str:
    escaped = str(tab_name).replace("'", "''")
    return f"'{escaped}'"


def _extract_first_values_matrix(data: Dict[str, Any]) -> List[List[Any]]:
    if "valueRanges" in data:
        ranges = data.get("valueRanges") or []
        if not ranges:
            return []
        return (ranges[0] or {}).get("values", []) or []
    return data.get("values", []) or []


def _read_tab_values(gw: SheetsGateway, sheet_id: str, tab_name: str) -> List[List[Any]]:
    range_addr = f"{_quote_tab(tab_name)}!A1:ZZ"
    data = gw.read(sheet_id, [range_addr])
    return _extract_first_values_matrix(data)


def _header_diff(expected: List[str], actual: List[str]) -> str:
    lines = ["Header mismatch:"]
    width = max(len(expected), len(actual))
    for i in range(width):
        e = expected[i] if i < len(expected) else "<missing>"
        a = actual[i] if i < len(actual) else "<missing>"
        marker = "OK" if e == a else "DIFF"
        lines.append(f"  col {i + 1:02d} [{marker}] expected={e!r} actual={a!r}")
    return "\n".join(lines)


def _normalize_bool_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return "TRUE"
    return "FALSE"


def _check_required_tabs_exist(gw: SheetsGateway, sheet_id: str, tabs: List[str]) -> None:
    missing: List[str] = []
    for tab in tabs:
        try:
            _read_tab_values(gw, sheet_id, tab)
        except SheetsGatewayError as e:
            text = str(e).lower()
            if "unable to parse range" in text:
                missing.append(tab)
                continue
            raise
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"Missing required tabs in CONTROL_SHEET_ID={sheet_id}: {joined}.\n"
            "Create them in Google Sheets UI:\n"
            "1) Open the CONTROL sheet.\n"
            "2) Click '+' at the bottom to add tabs named exactly: Sync_Jobs, Mapping, Runs_Ledger.\n"
            "3) Re-run scripts/bootstrap_control_sheet.py."
        )


def _ensure_header(gw: SheetsGateway, sheet_id: str, tab_name: str, expected_header: List[str]) -> None:
    try:
        values = _read_tab_values(gw, sheet_id, tab_name)
    except SheetsGatewayError as e:
        raise RuntimeError(
            f"Tab '{tab_name}' could not be read. If this is 'Unable to parse range', create the tab first."
        ) from e

    if not values:
        gw.write(sheet_id, [{"range": f"{_quote_tab(tab_name)}!A1", "values": [expected_header]}])
        print(f"Created header row for tab={tab_name}")
        return

    actual = [str(x).strip() for x in values[0]]
    while actual and actual[-1] == "":
        actual.pop()
    if actual == expected_header:
        print(f"Header OK (no-op): tab={tab_name}")
        return
    raise RuntimeError(f"Tab={tab_name}\n{_header_diff(expected_header, actual)}")


def _append_rows(gw: SheetsGateway, sheet_id: str, tab_name: str, rows: List[List[Any]], width: int) -> None:
    values = _read_tab_values(gw, sheet_id, tab_name)
    next_row = len(values) + 1
    padded = [pad_row(r, width) for r in rows]
    gw.write(sheet_id, [{"range": f"{_quote_tab(tab_name)}!A{next_row}", "values": padded}])
    print(f"Inserted {len(rows)} row(s) into tab={tab_name} at row={next_row}")


def _write_row(gw: SheetsGateway, sheet_id: str, tab_name: str, row_num: int, row: List[Any], width: int) -> None:
    gw.write(sheet_id, [{"range": f"{_quote_tab(tab_name)}!A{row_num}", "values": [pad_row(row, width)]}])


def _row_to_dict(headers: List[str], row: List[Any]) -> Dict[str, Any]:
    padded = pad_row(row, len(headers))
    return {h: padded[i] for i, h in enumerate(headers)}


def _row_changed(a: List[Any], b: List[Any]) -> bool:
    return ["" if v is None else str(v) for v in a] != ["" if v is None else str(v) for v in b]


def _default_job_template() -> Dict[str, Any]:
    source_sheet = os.environ.get("SAMPLE_SOURCE_SHEET_ID", "YOUR_GOOGLE_SHEET_ID").strip()
    target_sheet = os.environ.get("SAMPLE_TARGET_SHEET_ID", "YOUR_GOOGLE_SHEET_ID").strip()
    source_tab = os.environ.get("SAMPLE_SOURCE_TAB", "Sheet2").strip()
    target_tab = os.environ.get("SAMPLE_TARGET_TAB", "SO Campaigns").strip()
    return {
        "job_id": DEFAULT_JOB_ID,
        "source_spreadsheet_id": source_sheet,
        "target_spreadsheet_id": target_sheet,
        "source_tab_name": source_tab,
        "target_tab_name": target_tab,
        "source_range": "",
        "target_key_column": "",
        "write_mode": "append",
        "enabled": "TRUE",
        "schema_version": 1,
        "has_alias_row": "FALSE",
        "target_range": "",
    }


def _merge_with_default_job_fields(job: Dict[str, Any]) -> Dict[str, Any]:
    defaults = _default_job_template()
    merged = dict(job)
    for key, default_val in defaults.items():
        if key in {"job_id", "enabled"}:
            merged[key] = default_val
        elif key not in merged or str(merged.get(key) or "").strip() == "":
            merged[key] = default_val
    return merged


def _seed_sample_job_if_missing(gw: SheetsGateway, sheet_id: str) -> str:
    headers = CANONICAL_HEADERS[SYNC_JOBS_TAB]
    values = _read_tab_values(gw, sheet_id, SYNC_JOBS_TAB)
    rows = values[1:] if len(values) > 1 else []
    job_col_idx = headers.index("job_id")
    enabled_col_idx = headers.index("enabled")

    default_row_nums: List[int] = []
    legacy_row_nums: List[int] = []
    for i, row in enumerate(rows, start=2):
        job_id = str(row[job_col_idx]).strip() if len(row) > job_col_idx else ""
        if job_id == DEFAULT_JOB_ID:
            default_row_nums.append(i)
        elif job_id == LEGACY_SAMPLE_JOB_ID:
            legacy_row_nums.append(i)

    if default_row_nums:
        keep_row_num = default_row_nums[0]
        keep_row = rows[keep_row_num - 2]
        keep_dict = _merge_with_default_job_fields(_row_to_dict(headers, keep_row))
        desired = dict_to_row(keep_dict, headers)
        if _row_changed(pad_row(keep_row, len(headers)), desired):
            _write_row(gw, sheet_id, SYNC_JOBS_TAB, keep_row_num, desired, len(headers))
            print(f"Normalized canonical job row at row={keep_row_num} job_id={DEFAULT_JOB_ID}")

        for row_num in default_row_nums[1:]:
            row = pad_row(rows[row_num - 2], len(headers))
            if str(row[enabled_col_idx] or "").strip().upper() != "FALSE":
                row[enabled_col_idx] = "FALSE"
                _write_row(gw, sheet_id, SYNC_JOBS_TAB, row_num, row, len(headers))
                print(f"Disabled duplicate default job row at row={row_num}")

        for row_num in legacy_row_nums:
            row = pad_row(rows[row_num - 2], len(headers))
            if str(row[enabled_col_idx] or "").strip().upper() != "FALSE":
                row[enabled_col_idx] = "FALSE"
                _write_row(gw, sheet_id, SYNC_JOBS_TAB, row_num, row, len(headers))
                print(f"Disabled legacy sample job row at row={row_num} job_id={LEGACY_SAMPLE_JOB_ID}")

        if len(default_row_nums) == 1 and not legacy_row_nums:
            print(f"Sample job already present (no-op): job_id={DEFAULT_JOB_ID}")
        return DEFAULT_JOB_ID

    if legacy_row_nums:
        migrate_row_num = legacy_row_nums[0]
        migrate_row = rows[migrate_row_num - 2]
        migrate_dict = _merge_with_default_job_fields(_row_to_dict(headers, migrate_row))
        desired = dict_to_row(migrate_dict, headers)
        _write_row(gw, sheet_id, SYNC_JOBS_TAB, migrate_row_num, desired, len(headers))
        print(f"Migrated legacy sample job row to job_id={DEFAULT_JOB_ID} at row={migrate_row_num}")

        for row_num in legacy_row_nums[1:]:
            row = pad_row(rows[row_num - 2], len(headers))
            if str(row[enabled_col_idx] or "").strip().upper() != "FALSE":
                row[enabled_col_idx] = "FALSE"
                _write_row(gw, sheet_id, SYNC_JOBS_TAB, row_num, row, len(headers))
                print(f"Disabled extra legacy sample job row at row={row_num}")
        return DEFAULT_JOB_ID

    sample_job = _default_job_template()
    _append_rows(gw, sheet_id, SYNC_JOBS_TAB, [dict_to_row(sample_job, headers)], len(headers))
    return DEFAULT_JOB_ID


def _seed_sample_mapping_if_missing(gw: SheetsGateway, sheet_id: str, sample_job_id: str) -> None:
    headers = CANONICAL_HEADERS[MAPPING_TAB]
    values = _read_tab_values(gw, sheet_id, MAPPING_TAB)
    rows = values[1:] if len(values) > 1 else []
    job_col_idx = headers.index("job_id")

    existing_default_rows = [
        i for i, row in enumerate(rows, start=2)
        if len(row) > job_col_idx and str(row[job_col_idx]).strip() == sample_job_id
    ]
    if existing_default_rows:
        print(f"Sample mapping rows already present (no-op): job_id={sample_job_id}")
        return

    legacy_rows = [
        i for i, row in enumerate(rows, start=2)
        if len(row) > job_col_idx and str(row[job_col_idx]).strip() == LEGACY_SAMPLE_JOB_ID
    ]
    if legacy_rows:
        for row_num in legacy_rows:
            row = pad_row(rows[row_num - 2], len(headers))
            row[job_col_idx] = sample_job_id
            _write_row(gw, sheet_id, MAPPING_TAB, row_num, row, len(headers))
        print(f"Migrated {len(legacy_rows)} mapping row(s) from {LEGACY_SAMPLE_JOB_ID} to {sample_job_id}")
        return

    sample_rows = [
        {
            # sample mapping row
            "job_id": sample_job_id,
            "source_col": "Naming Convention",
            "target_col": "notes",
            "transform": "none",
            "default_value": "",
            "on_missing": "default",
            "trim": "TRUE",
            "schema_version": 1,
        },
        {
            # sample mapping row
            "job_id": sample_job_id,
            "source_col": "Audio File",
            "target_col": "audio_chip",
            "transform": "none",
            "default_value": "",
            "on_missing": "default",
            "trim": "TRUE",
            "schema_version": 1,
        },
    ]
    _append_rows(gw, sheet_id, MAPPING_TAB, [dict_to_row(r, headers) for r in sample_rows], len(headers))


def _verify_post_write_state(gw: SheetsGateway, sheet_id: str, required_tabs: List[str]) -> None:
    for tab in required_tabs:
        values = _read_tab_values(gw, sheet_id, tab)
        header = [str(x).strip() for x in (values[0] if values else [])]
        while header and header[-1] == "":
            header.pop()
        expected = CANONICAL_HEADERS[tab]
        if header != expected:
            raise RuntimeError(f"Post-write header verification failed for {tab}\n{_header_diff(expected, header)}")

    sync_values = _read_tab_values(gw, sheet_id, SYNC_JOBS_TAB)
    sync_header = CANONICAL_HEADERS[SYNC_JOBS_TAB]
    rows = sync_values[1:] if len(sync_values) > 1 else []
    job_col_idx = sync_header.index("job_id")
    enabled_col_idx = sync_header.index("enabled")

    default_rows = []
    legacy_rows = []
    for i, row in enumerate(rows, start=2):
        padded = pad_row(row, len(sync_header))
        job_id = str(padded[job_col_idx] or "").strip()
        if job_id == DEFAULT_JOB_ID:
            default_rows.append((i, padded))
        if job_id == LEGACY_SAMPLE_JOB_ID:
            legacy_rows.append((i, padded))

    if not default_rows:
        raise RuntimeError("Post-write verification failed: default job row is missing in Sync_Jobs.")
    default_enabled = _normalize_bool_text(default_rows[0][1][enabled_col_idx])
    if default_enabled != "TRUE":
        raise RuntimeError(
            f"Post-write verification failed: default job enabled expected TRUE actual {default_rows[0][1][enabled_col_idx]!r}"
        )
    for row_num, row in legacy_rows:
        if _normalize_bool_text(row[enabled_col_idx]) != "FALSE":
            raise RuntimeError(
                f"Post-write verification failed: legacy sample job at row {row_num} must be disabled, "
                f"actual enabled={row[enabled_col_idx]!r}"
            )


def main() -> int:
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", DEFAULT_BASE).strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    include_access_control = os.environ.get("INCLUDE_ACCESS_CONTROL", "0").strip() == "1"

    gw = SheetsGateway(base_url=base_url, api_key=api_key)

    required_tabs = [SYNC_JOBS_TAB, MAPPING_TAB, RUNS_LEDGER_TAB]
    if include_access_control:
        required_tabs.append(ACCESS_CONTROL_TAB)

    _check_required_tabs_exist(gw, control_sheet_id, required_tabs)

    for tab in required_tabs:
        _ensure_header(gw, control_sheet_id, tab, CANONICAL_HEADERS[tab])

    sample_job_id = _seed_sample_job_if_missing(gw, control_sheet_id)
    _seed_sample_mapping_if_missing(gw, control_sheet_id, sample_job_id)
    _verify_post_write_state(gw, control_sheet_id, required_tabs)

    print("Bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

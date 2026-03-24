#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Allow importing from ../src without installing package
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway  # noqa: E402


GATEWAY_BASE = os.environ.get(
    "SHEETS_JSON_BASE_URL",
    os.environ.get("SO_GATEWAY_BASE", "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"),
).rstrip("/")
GATEWAY_KEY = os.environ.get("SHEETS_JSON_API_KEY", os.environ.get("SO_GATEWAY_KEY", "")).strip()
SHEET_ID = os.environ.get("SHEET_ID", os.environ.get("SHEET_PROD", "")).strip()
PROD_TAB = os.environ.get("PROD_TAB", "SO Campaigns").strip()
SAMPLE_ROWS = int(os.environ.get("SAMPLE_ROWS", "25"))

PERFORMANCE_COLUMNS: List[str] = [
    "render_mode",
    "performance_mode",
    "performance_audio_policy",
    "performance_master_clip_drive_file_id",
    "performance_clip_a_drive_file_id",
    "performance_clip_b_drive_file_id",
    "performance_clip_c_drive_file_id",
    "performance_clip_d_drive_file_id",
    "performance_clip_e_drive_file_id",
    "performance_clip_f_drive_file_id",
    "performance_preset_dsl",
]

LEGACY_COLUMNS: List[str] = [
    "campaign_id",
    "edit_mode",
    "stitch_preset_dsl",
    "text_preset_name",
    "audio_chip",
    "audio_drive_file_id",
    "srt_chip",
    "srt_drive_file_id",
]


def _must(value: str, name: str) -> str:
    if not str(value or "").strip():
        raise RuntimeError(f"Missing required env var: {name}")
    return str(value).strip()


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


def _clean_header(row: List[Any]) -> List[str]:
    out = [str(v).strip() for v in list(row or [])]
    while out and out[-1] == "":
        out.pop()
    return out


def _nonempty_count(rows: List[List[Any]], idx: int) -> int:
    count = 0
    for row in rows:
        if idx >= len(row):
            continue
        if str(row[idx] or "").strip():
            count += 1
    return count


def build_audit_report(header: List[str], rows: List[List[Any]], *, sample_rows: int) -> Dict[str, Any]:
    expected = PERFORMANCE_COLUMNS
    missing = [c for c in expected if c not in header]
    extras = [c for c in header if c not in (LEGACY_COLUMNS + PERFORMANCE_COLUMNS)]
    sample = rows[: max(0, int(sample_rows))]
    fields = LEGACY_COLUMNS + PERFORMANCE_COLUMNS
    completeness: Dict[str, Any] = {}
    for field in fields:
        if field not in header:
            completeness[field] = {"present": False}
            continue
        idx = header.index(field)
        completeness[field] = {
            "present": True,
            "sample_nonempty": _nonempty_count(sample, idx),
            "sample_total": len(sample),
        }
    return {
        "header_count": len(header),
        "headers": header,
        "performance_columns_expected": list(PERFORMANCE_COLUMNS),
        "performance_columns_missing": missing,
        "extra_columns_non_standard": extras,
        "row_count": len(rows),
        "sample_rows_analyzed": len(sample),
        "sample_completeness": completeness,
    }


def main() -> int:
    sheet_id = _must(SHEET_ID, "SHEET_ID or SHEET_PROD")
    tab = _must(PROD_TAB, "PROD_TAB")
    gw = SheetsGateway(GATEWAY_BASE, GATEWAY_KEY)
    data = gw.read(sheet_id, [f"{_quote_tab(tab)}!A1:ZZZ"])
    values = _extract_first_values_matrix(data)
    if not values:
        raise RuntimeError(f"No values found in sheet={sheet_id} tab={tab}")
    header = _clean_header(values[0])
    rows = values[1:]
    report = build_audit_report(header, rows, sample_rows=SAMPLE_ROWS)
    report["sheet_id"] = sheet_id
    report["tab"] = tab
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

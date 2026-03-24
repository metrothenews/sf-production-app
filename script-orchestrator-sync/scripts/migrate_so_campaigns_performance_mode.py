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
APPLY = os.environ.get("APPLY", "0").strip() == "1"

NEW_COLUMNS: List[str] = [
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


def _must(value: str, name: str) -> str:
    if not str(value or "").strip():
        raise RuntimeError(f"Missing required env var: {name}")
    return str(value).strip()


def _quote_tab(tab_name: str) -> str:
    escaped = str(tab_name).replace("'", "''")
    return f"'{escaped}'"


def _col_to_a1(col_idx_0: int) -> str:
    n = int(col_idx_0) + 1
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _extract_first_values_matrix(data: Dict[str, Any]) -> List[List[Any]]:
    if "valueRanges" in data:
        ranges = data.get("valueRanges") or []
        if not ranges:
            return []
        return (ranges[0] or {}).get("values", []) or []
    return data.get("values", []) or []


def _clean_header(raw: List[Any]) -> List[str]:
    header = [str(v).strip() for v in list(raw or [])]
    while header and header[-1] == "":
        header.pop()
    return header


def _pad_row(row: List[Any], width: int) -> List[str]:
    raw = [str(v) if v is not None else "" for v in list(row or [])]
    if len(raw) < width:
        raw.extend([""] * (width - len(raw)))
    return raw[:width]


def _normalized_lower(value: str) -> str:
    return str(value or "").strip().lower()


def _normalized_upper(value: str) -> str:
    return str(value or "").strip().upper()


def build_migration_plan(header: List[str], rows: List[List[Any]]) -> Dict[str, Any]:
    target_header = list(header)
    added_columns: List[str] = []
    for col in NEW_COLUMNS:
        if col not in target_header:
            target_header.append(col)
            added_columns.append(col)

    idx = {name: i for i, name in enumerate(target_header)}
    row_updates: List[Dict[str, Any]] = []
    warnings: List[str] = []
    skipped_rows: List[int] = []

    for row_num, row in enumerate(rows, start=2):
        padded = _pad_row(row, len(target_header))

        def get(name: str) -> str:
            i = idx.get(name)
            if i is None or i >= len(padded):
                return ""
            return str(padded[i] or "").strip()

        updates: Dict[str, str] = {}
        render_mode = _normalized_lower(get("render_mode"))
        edit_mode = _normalized_upper(get("edit_mode"))

        if not render_mode:
            if edit_mode == "FF_ONLY":
                updates["render_mode"] = "found_footage"
                render_mode = "found_footage"
            elif edit_mode == "FF_PLUS_PERF":
                updates["render_mode"] = "performance"
                render_mode = "performance"
            elif edit_mode:
                warnings.append(f"row={row_num} unknown_edit_mode={edit_mode}")
        elif render_mode not in {"found_footage", "performance"}:
            warnings.append(f"row={row_num} unknown_render_mode={render_mode}")
            skipped_rows.append(row_num)
            continue

        if render_mode == "performance":
            if not get("performance_mode"):
                updates["performance_mode"] = "simple_sync_cut"
            if not get("performance_audio_policy"):
                updates["performance_audio_policy"] = "master_only"

        if updates:
            row_updates.append({"row_num": row_num, "updates": updates})

    return {
        "target_header": target_header,
        "added_columns": added_columns,
        "row_updates": row_updates,
        "warnings": warnings,
        "skipped_rows": skipped_rows,
        "rows_scanned": len(rows),
    }


def apply_plan_in_memory(header: List[str], rows: List[List[Any]], plan: Dict[str, Any]) -> tuple[List[str], List[List[str]]]:
    target_header = list(plan.get("target_header") or list(header))
    out_rows = [_pad_row(row, len(target_header)) for row in list(rows or [])]
    row_updates = list(plan.get("row_updates") or [])
    idx = {name: i for i, name in enumerate(target_header)}
    for row_update in row_updates:
        row_num = int(row_update.get("row_num") or 0)
        if row_num < 2:
            continue
        row_idx = row_num - 2
        if row_idx >= len(out_rows):
            continue
        row = out_rows[row_idx]
        updates = row_update.get("updates") if isinstance(row_update.get("updates"), dict) else {}
        for field, value in updates.items():
            col_idx = idx.get(str(field))
            if col_idx is None:
                continue
            row[col_idx] = str(value)
    return target_header, out_rows


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

    plan = build_migration_plan(header, rows)
    target_header = list(plan["target_header"])
    row_updates = list(plan["row_updates"])

    batch_updates: List[Dict[str, Any]] = []
    if list(plan.get("added_columns") or []):
        batch_updates.append({"range": f"{_quote_tab(tab)}!A1", "values": [target_header]})

    if row_updates:
        idx = {name: i for i, name in enumerate(target_header)}
        for row_update in row_updates:
            row_num = int(row_update["row_num"])
            updates = row_update["updates"]
            for field, value in updates.items():
                col_idx = idx[str(field)]
                a1_col = _col_to_a1(col_idx)
                batch_updates.append({"range": f"{_quote_tab(tab)}!{a1_col}{row_num}", "values": [[value]]})

    summary = {
        "sheet_id": sheet_id,
        "tab": tab,
        "apply": bool(APPLY),
        "added_columns": list(plan.get("added_columns") or []),
        "rows_scanned": int(plan.get("rows_scanned") or 0),
        "rows_touched": len(row_updates),
        "cell_updates": int(sum(len((r.get("updates") or {})) for r in row_updates)),
        "warnings": list(plan.get("warnings") or []),
        "skipped_rows": list(plan.get("skipped_rows") or []),
        "write_batch_count": len(batch_updates),
        "write_preview": batch_updates[:25],
    }

    if APPLY and batch_updates:
        resp = gw.write(sheet_id, batch_updates)
        summary["write_ok"] = bool(resp.get("ok"))
    else:
        summary["write_ok"] = True

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Repository helpers for render control-plane tabs."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .gateway import SheetsGateway
from .render_contract import (
    OUTPUTS_INDEX_TAB,
    RENDER_CANONICAL_HEADERS,
    RENDER_HEADER_ALIASES,
    RENDER_JOBS_TAB,
    RENDER_RUNS_TAB,
)
from .repositories import normalize_enabled_value
from .sheet_contract import (
    build_header_index,
    dict_to_row,
    pad_row,
    resolve_header_key,
    row_to_canonical_dict,
)


def _quote_tab(tab_name: str) -> str:
    escaped = str(tab_name).replace("'", "''")
    return f"'{escaped}'"


def _first_values_matrix(read_response: Dict[str, Any]) -> List[List[Any]]:
    if "valueRanges" in read_response:
        ranges = read_response.get("valueRanges") or []
        if not ranges:
            return []
        first = ranges[0] or {}
        return first.get("values", []) or []
    return read_response.get("values", []) or []


def _read_tab(gateway: SheetsGateway, sheet_id: str, tab_name: str) -> tuple[List[str], List[List[Any]]]:
    range_addr = f"{_quote_tab(tab_name)}!A1:ZZ"
    resp = gateway.read(sheet_id, [range_addr])
    values = _first_values_matrix(resp)
    if not values:
        raise ValueError(f"Tab '{tab_name}' returned no data for range {range_addr}")
    header = [str(v).strip() for v in values[0]]
    return header, values[1:]


def _ensure_render_headers(tab_name: str, header_index: Dict[str, int]) -> None:
    expected = RENDER_CANONICAL_HEADERS[tab_name]
    missing = [h for h in expected if resolve_header_key(h, RENDER_HEADER_ALIASES) not in header_index]
    if missing:
        raise ValueError(f"Tab '{tab_name}' missing required columns: {missing}")


def _dict_to_sheet_row(d: Dict[str, Any], header: List[str]) -> List[Any]:
    row: List[Any] = []
    for h in header:
        canonical_key = resolve_header_key(h, RENDER_HEADER_ALIASES)
        row.append(d.get(canonical_key))
    return row


def _find_row_index_by_key(rows: List[List[Any]], key_col_idx: int, key_val: str) -> Optional[int]:
    needle = str(key_val).strip()
    for i, row in enumerate(rows):
        if len(row) > key_col_idx and str(row[key_col_idx]).strip() == needle:
            return i
    return None


def read_render_jobs_all(gateway: SheetsGateway, sheet_id: str) -> List[Dict[str, Any]]:
    header, rows = _read_tab(gateway, sheet_id, RENDER_JOBS_TAB)
    header_index = build_header_index(header, aliases=RENDER_HEADER_ALIASES)
    _ensure_render_headers(RENDER_JOBS_TAB, header_index)
    canonical = RENDER_CANONICAL_HEADERS[RENDER_JOBS_TAB]
    return [
        row_to_canonical_dict(raw, canonical_headers=canonical, header_index=header_index)
        for raw in rows
    ]


def list_render_jobs(
    gateway: SheetsGateway,
    sheet_id: str,
    *,
    enabled: bool | None = None,
    statuses: Iterable[str] | None = None,
) -> List[Dict[str, Any]]:
    rows = read_render_jobs_all(gateway, sheet_id)
    status_set = {str(s).strip().lower() for s in statuses} if statuses else None
    out: List[Dict[str, Any]] = []
    for row in rows:
        if enabled is not None and normalize_enabled_value(row.get("enabled")) != enabled:
            continue
        status = str(row.get("status") or "").strip().lower()
        if status_set is not None and status not in status_set:
            continue
        out.append(row)
    return out


def find_render_job_by_id(gateway: SheetsGateway, sheet_id: str, render_job_id: str) -> Optional[Dict[str, Any]]:
    header, rows = _read_tab(gateway, sheet_id, RENDER_JOBS_TAB)
    header_index = build_header_index(header, aliases=RENDER_HEADER_ALIASES)
    _ensure_render_headers(RENDER_JOBS_TAB, header_index)
    key_idx = header_index[resolve_header_key("render_job_id", RENDER_HEADER_ALIASES)]
    row_idx = _find_row_index_by_key(rows, key_idx, render_job_id)
    if row_idx is None:
        return None
    canonical = RENDER_CANONICAL_HEADERS[RENDER_JOBS_TAB]
    return row_to_canonical_dict(rows[row_idx], canonical_headers=canonical, header_index=header_index)


def upsert_render_job(gateway: SheetsGateway, sheet_id: str, render_job: Dict[str, Any]) -> None:
    header, rows = _read_tab(gateway, sheet_id, RENDER_JOBS_TAB)
    header_index = build_header_index(header, aliases=RENDER_HEADER_ALIASES)
    _ensure_render_headers(RENDER_JOBS_TAB, header_index)
    key_idx = header_index[resolve_header_key("render_job_id", RENDER_HEADER_ALIASES)]
    key_val = str(render_job.get("render_job_id") or "").strip()
    if not key_val:
        raise ValueError("render_job_id is required for upsert_render_job")
    existing_idx = _find_row_index_by_key(rows, key_idx, key_val)
    if existing_idx is not None:
        sheet_row = existing_idx + 2
    else:
        sheet_row = len(rows) + 2
    row_vals = _dict_to_sheet_row(render_job, header)
    gateway.write(sheet_id, [{"range": f"{_quote_tab(RENDER_JOBS_TAB)}!A{sheet_row}", "values": [row_vals]}])


def append_outputs_index_rows(gateway: SheetsGateway, sheet_id: str, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    header, existing = _read_tab(gateway, sheet_id, OUTPUTS_INDEX_TAB)
    header_index = build_header_index(header, aliases=RENDER_HEADER_ALIASES)
    _ensure_render_headers(OUTPUTS_INDEX_TAB, header_index)
    canonical = RENDER_CANONICAL_HEADERS[OUTPUTS_INDEX_TAB]
    values = [pad_row(dict_to_row(r, canonical), len(canonical)) for r in rows]
    start = len(existing) + 2
    gateway.write(sheet_id, [{"range": f"{_quote_tab(OUTPUTS_INDEX_TAB)}!A{start}", "values": values}])
    return len(values)


def read_outputs_index_all(gateway: SheetsGateway, sheet_id: str) -> List[Dict[str, Any]]:
    header, rows = _read_tab(gateway, sheet_id, OUTPUTS_INDEX_TAB)
    header_index = build_header_index(header, aliases=RENDER_HEADER_ALIASES)
    _ensure_render_headers(OUTPUTS_INDEX_TAB, header_index)
    canonical = RENDER_CANONICAL_HEADERS[OUTPUTS_INDEX_TAB]
    return [
        row_to_canonical_dict(raw, canonical_headers=canonical, header_index=header_index)
        for raw in rows
    ]


def count_outputs_for_run(
    rows: Iterable[Dict[str, Any]],
    *,
    run_id: str,
    render_job_id: str,
    artifact_type: str,
) -> int:
    wanted_run = str(run_id).strip()
    wanted_job = str(render_job_id).strip()
    wanted_type = str(artifact_type).strip().lower()
    return sum(
        1
        for row in rows
        if str(row.get("run_id") or "").strip() == wanted_run
        and str(row.get("render_job_id") or "").strip() == wanted_job
        and str(row.get("artifact_type") or "").strip().lower() == wanted_type
    )


def find_render_run_by_id(gateway: SheetsGateway, sheet_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    header, rows = _read_tab(gateway, sheet_id, RENDER_RUNS_TAB)
    header_index = build_header_index(header, aliases=RENDER_HEADER_ALIASES)
    _ensure_render_headers(RENDER_RUNS_TAB, header_index)
    key_idx = header_index[resolve_header_key("run_id", RENDER_HEADER_ALIASES)]
    row_idx = _find_row_index_by_key(rows, key_idx, run_id)
    if row_idx is None:
        return None
    canonical = RENDER_CANONICAL_HEADERS[RENDER_RUNS_TAB]
    return row_to_canonical_dict(rows[row_idx], canonical_headers=canonical, header_index=header_index)


def upsert_render_run(gateway: SheetsGateway, sheet_id: str, run_row: Dict[str, Any]) -> None:
    header, rows = _read_tab(gateway, sheet_id, RENDER_RUNS_TAB)
    header_index = build_header_index(header, aliases=RENDER_HEADER_ALIASES)
    _ensure_render_headers(RENDER_RUNS_TAB, header_index)
    key_idx = header_index[resolve_header_key("run_id", RENDER_HEADER_ALIASES)]
    key_val = str(run_row.get("run_id") or "").strip()
    if not key_val:
        raise ValueError("run_id is required for upsert_render_run")
    existing_idx = _find_row_index_by_key(rows, key_idx, key_val)
    if existing_idx is not None:
        sheet_row = existing_idx + 2
    else:
        sheet_row = len(rows) + 2
    row_vals = _dict_to_sheet_row(run_row, header)
    gateway.write(sheet_id, [{"range": f"{_quote_tab(RENDER_RUNS_TAB)}!A{sheet_row}", "values": [row_vals]}])

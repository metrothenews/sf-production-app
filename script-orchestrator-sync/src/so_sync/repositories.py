"""Repository layer for reading/writing the canonical sheets.
All functions use :class:`so_sync.gateway.SheetsGateway` to talk to the
Sheets‑JSON gateway. The contract guarantees that each sheet has a header row
(row 1). Data rows start at row 2 unless the job specifies `has_alias_row=True`,
in which case they start at row 3.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .gateway import SheetsGateway
from .sheet_contract import (
    ACCESS_CONTROL_TAB,
    CANONICAL_HEADERS,
    HEADER_ALIASES,
    MAPPING_TAB,
    RUNS_LEDGER_TAB,
    SYNC_JOBS_TAB,
    build_header_index,
    resolve_header_key,
    row_to_canonical_dict,
)


def _read_tab(
    gateway: SheetsGateway,
    sheet_id: str,
    tab_name: str,
    has_alias_row: bool = False,
) -> tuple[List[str], List[List[Any]]]:
    """Read a whole tab.
    Returns the header row and a list of data rows (as they appear in the sheet).
    If ``has_alias_row`` is True, the second row is treated as an alias row and
    data starts at row 3.
    """
    range_addr = f"{_quote_tab(tab_name)}!A1:ZZZ"
    resp = gateway.read(sheet_id, [range_addr])
    values = _first_values_matrix(resp)
    if not values:
        raise ValueError(f"Tab '{tab_name}' returned no data for range {range_addr}")
    physical_header = [str(v) for v in values[0]]
    alias_row = values[1] if has_alias_row and len(values) > 1 else None
    header = []
    for idx, raw_header in enumerate(physical_header):
        if alias_row and idx < len(alias_row) and str(alias_row[idx] or "").strip():
            header.append(str(alias_row[idx]).strip())
        else:
            header.append(str(raw_header).strip())
    data_start = 2 if has_alias_row else 1  # 0‑based index for data rows
    data_rows = values[data_start:]
    return header, data_rows


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


def _ensure_canonical_headers(
    tab_name: str,
    header_index: Dict[str, int],
    canonical_headers: List[str],
) -> None:
    missing = [h for h in canonical_headers if resolve_header_key(h) not in header_index]
    if missing:
        raise ValueError(
            f"Tab '{tab_name}' is missing canonical headers: {missing}. "
            "Check header row spelling/aliases."
        )


def _dict_to_sheet_row(d: Dict[str, Any], header: List[str]) -> List[Any]:
    row: List[Any] = []
    for h in header:
        canonical_key = resolve_header_key(h, HEADER_ALIASES)
        row.append(d.get(canonical_key))
    return row


def normalize_enabled_value(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n", ""}:
        return False
    return False


def select_job_from_sync_jobs(
    jobs: List[Dict[str, Any]],
    *,
    job_id: str | None = None,
) -> tuple[Dict[str, Any], str]:
    enabled_jobs = [j for j in jobs if normalize_enabled_value(j.get("enabled"))]
    known_job_ids = sorted(str(j.get("job_id")) for j in enabled_jobs if str(j.get("job_id") or "").strip())
    requested = str(job_id or "").strip()
    if requested:
        selected = next((j for j in enabled_jobs if str(j.get("job_id") or "").strip() == requested), None)
        if not selected:
            raise ValueError(f"JOB_ID '{requested}' not found in Sync_Jobs. Known job_ids={known_job_ids}")
        return selected, "env"

    if len(enabled_jobs) == 0:
        raise ValueError("No enabled jobs in Sync_Jobs (set enabled=TRUE on exactly one row).")
    if len(enabled_jobs) > 1:
        raise ValueError("Multiple enabled jobs enabled; disable extras or set JOB_ID.")
    return enabled_jobs[0], "auto-selection"

# ---------------------------------------------------------------------------
# Sync_Jobs repository
# ---------------------------------------------------------------------------
def read_sync_jobs_all(gateway: SheetsGateway, sheet_id: str) -> List[Dict[str, Any]]:
    header, rows = _read_tab(gateway, sheet_id, SYNC_JOBS_TAB)
    canonical_headers = CANONICAL_HEADERS[SYNC_JOBS_TAB]
    header_index = build_header_index(header, aliases=HEADER_ALIASES)
    _ensure_canonical_headers(SYNC_JOBS_TAB, header_index, canonical_headers)
    jobs: List[Dict[str, Any]] = []
    for raw in rows:
        row_dict = row_to_canonical_dict(
            raw,
            canonical_headers=canonical_headers,
            header_index=header_index,
        )
        jobs.append(row_dict)
    return jobs


def read_sync_jobs(gateway: SheetsGateway, sheet_id: str) -> List[Dict[str, Any]]:
    return [j for j in read_sync_jobs_all(gateway, sheet_id) if normalize_enabled_value(j.get("enabled"))]

# ---------------------------------------------------------------------------
# Mapping repository
# ---------------------------------------------------------------------------
def read_mapping(gateway: SheetsGateway, sheet_id: str, job_id: str) -> List[Dict[str, Any]]:
    header, rows = _read_tab(gateway, sheet_id, MAPPING_TAB)
    canonical_headers = CANONICAL_HEADERS[MAPPING_TAB]
    header_index = build_header_index(header, aliases=HEADER_ALIASES)
    _ensure_canonical_headers(MAPPING_TAB, header_index, canonical_headers)
    mappings: List[Dict[str, Any]] = []
    for raw in rows:
        row_dict = row_to_canonical_dict(
            raw,
            canonical_headers=canonical_headers,
            header_index=header_index,
        )
        if row_dict.get("job_id") == job_id:
            mappings.append(row_dict)
    # Enforce uniqueness of (job_id, source_col)
    seen = set()
    for m in mappings:
        key = (m["job_id"], m["source_col"])
        if key in seen:
            raise ValueError(f"Duplicate mapping for job {m['job_id']} source column {m['source_col']}")
        seen.add(key)
    return mappings

# ---------------------------------------------------------------------------
# Runs Ledger repository
# ---------------------------------------------------------------------------
def _find_row_index_by_key(rows: List[List[Any]], key_col_idx: int, key_val: str) -> Optional[int]:
    for i, row in enumerate(rows):
        if len(row) > key_col_idx and str(row[key_col_idx]).strip() == str(key_val).strip():
            return i
    return None

def find_run_by_id(gateway: SheetsGateway, sheet_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    header, rows = _read_tab(gateway, sheet_id, RUNS_LEDGER_TAB)
    canonical_headers = CANONICAL_HEADERS[RUNS_LEDGER_TAB]
    header_index = build_header_index(header, aliases=HEADER_ALIASES)
    _ensure_canonical_headers(RUNS_LEDGER_TAB, header_index, canonical_headers)
    run_col_idx = header_index[resolve_header_key("run_id")]
    run_idx = _find_row_index_by_key(rows, run_col_idx, run_id)
    if run_idx is None:
        return None
    return row_to_canonical_dict(
        rows[run_idx],
        canonical_headers=canonical_headers,
        header_index=header_index,
    )

def find_run_by_dedupe_key(gateway: SheetsGateway, sheet_id: str, dedupe_key: str) -> Optional[Dict[str, Any]]:
    header, rows = _read_tab(gateway, sheet_id, RUNS_LEDGER_TAB)
    canonical_headers = CANONICAL_HEADERS[RUNS_LEDGER_TAB]
    header_index = build_header_index(header, aliases=HEADER_ALIASES)
    _ensure_canonical_headers(RUNS_LEDGER_TAB, header_index, canonical_headers)
    dedupe_col = header_index.get(resolve_header_key("dedupe_key"))
    if dedupe_col is None:
        return None
    run_idx = _find_row_index_by_key(rows, dedupe_col, dedupe_key)
    if run_idx is None:
        return None
    return row_to_canonical_dict(
        rows[run_idx],
        canonical_headers=canonical_headers,
        header_index=header_index,
    )

def upsert_run(gateway: SheetsGateway, sheet_id: str, run_dict: Dict[str, Any]) -> None:
    """Insert or update a run row.
    The function reads the existing sheet to locate the row (by ``run_id``). If the
    row exists it is replaced; otherwise it is appended after the last data row.
    """
    header, rows = _read_tab(gateway, sheet_id, RUNS_LEDGER_TAB)
    canonical_headers = CANONICAL_HEADERS[RUNS_LEDGER_TAB]
    header_index = build_header_index(header, aliases=HEADER_ALIASES)
    _ensure_canonical_headers(RUNS_LEDGER_TAB, header_index, canonical_headers)
    run_id = str(run_dict["run_id"])
    run_col_idx = header_index[resolve_header_key("run_id")]
    existing_idx = _find_row_index_by_key(rows, run_col_idx, run_id)
    row_vals = _dict_to_sheet_row(run_dict, header)
    if existing_idx is not None:
        sheet_row = existing_idx + 2  # header is row 1, data starts at row 2
    else:
        sheet_row = len(rows) + 2  # append after existing data rows
    target_range = f"{_quote_tab(RUNS_LEDGER_TAB)}!A{sheet_row}"
    update_payload = [{"range": target_range, "values": [row_vals]}]
    gateway.write(sheet_id, update_payload)

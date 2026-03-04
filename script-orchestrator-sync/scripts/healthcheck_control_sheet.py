#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Allow importing from ../src without installing a package
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway, SheetsGatewayError  # noqa: E402
from so_sync.repositories import (  # noqa: E402
    normalize_enabled_value,
    read_sync_jobs_all,
    select_job_from_sync_jobs,
)
from so_sync.render_contract import (  # noqa: E402
    OUTPUTS_INDEX_TAB,
    RENDER_CANONICAL_HEADERS,
    RENDER_JOBS_TAB,
    RENDER_RUNS_TAB,
)
from so_sync.sheet_contract import CANONICAL_HEADERS, MAPPING_TAB, RUNS_LEDGER_TAB, SYNC_JOBS_TAB  # noqa: E402

DEFAULT_BASE = "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"


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
    rng = f"{_quote_tab(tab_name)}!A1:ZZ"
    data = gw.read(sheet_id, [rng])
    return _extract_first_values_matrix(data)


def _ensure_tab_readable(gw: SheetsGateway, sheet_id: str, tab_name: str) -> None:
    try:
        _read_tab_values(gw, sheet_id, tab_name)
    except SheetsGatewayError as e:
        text = str(e).lower()
        if "unable to parse range" in text:
            raise RuntimeError(
                f"Missing tab '{tab_name}' in CONTROL sheet. Create tab '{tab_name}' in Google Sheets and rerun."
            ) from e
        raise RuntimeError(f"Failed reading tab '{tab_name}': {e}") from e


def _tab_exists(gw: SheetsGateway, sheet_id: str, tab_name: str) -> bool:
    try:
        _read_tab_values(gw, sheet_id, tab_name)
        return True
    except SheetsGatewayError as e:
        if "unable to parse range" in str(e).lower():
            return False
        raise RuntimeError(f"Failed reading tab '{tab_name}': {e}") from e


def _ensure_required_headers(gw: SheetsGateway, sheet_id: str, tab_name: str) -> None:
    values = _read_tab_values(gw, sheet_id, tab_name)
    if not values:
        raise RuntimeError(f"Tab '{tab_name}' is empty; expected header row.")
    header = [str(x).strip() for x in values[0]]
    while header and header[-1] == "":
        header.pop()
    missing = [h for h in CANONICAL_HEADERS[tab_name] if h not in header]
    if missing:
        raise RuntimeError(f"Tab '{tab_name}' missing required headers: {missing}")


def _ensure_required_headers_from_map(
    gw: SheetsGateway,
    sheet_id: str,
    tab_name: str,
    expected_map: Dict[str, List[str]],
) -> None:
    values = _read_tab_values(gw, sheet_id, tab_name)
    if not values:
        raise RuntimeError(f"Tab '{tab_name}' is empty; expected header row.")
    header = [str(x).strip() for x in values[0]]
    while header and header[-1] == "":
        header.pop()
    missing = [h for h in expected_map[tab_name] if h not in header]
    if missing:
        raise RuntimeError(f"Tab '{tab_name}' missing required headers: {missing}")


def _print_job_states(jobs: List[Dict[str, Any]]) -> None:
    parts = []
    for job in jobs:
        jid = str(job.get("job_id") or "")
        enabled = "TRUE" if normalize_enabled_value(job.get("enabled")) else "FALSE"
        parts.append(f"{jid}:{enabled}")
    print(f"Sync_Jobs states: {parts}")


def main() -> int:
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", DEFAULT_BASE).strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")
    require_render_tabs = os.environ.get("REQUIRE_RENDER_TABS", "0").strip() == "1"

    gw = SheetsGateway(base_url=base_url, api_key=api_key)

    for tab in [SYNC_JOBS_TAB, MAPPING_TAB, RUNS_LEDGER_TAB]:
        _ensure_tab_readable(gw, control_sheet_id, tab)
    _ensure_required_headers(gw, control_sheet_id, MAPPING_TAB)
    _ensure_required_headers(gw, control_sheet_id, RUNS_LEDGER_TAB)

    render_tabs = [RENDER_JOBS_TAB, OUTPUTS_INDEX_TAB, RENDER_RUNS_TAB]
    existing_render_tabs = [t for t in render_tabs if _tab_exists(gw, control_sheet_id, t)]
    if require_render_tabs:
        missing = [t for t in render_tabs if t not in existing_render_tabs]
        if missing:
            raise RuntimeError(
                f"Missing required render tabs: {missing}. "
                "Create tabs Render_Jobs, Outputs_Index, Render_Runs in Google Sheets and rerun."
            )
    for tab in existing_render_tabs:
        _ensure_required_headers_from_map(gw, control_sheet_id, tab, RENDER_CANONICAL_HEADERS)

    jobs = read_sync_jobs_all(gw, control_sheet_id)
    try:
        selected, source = select_job_from_sync_jobs(jobs, job_id=None)
        print(f"Healthcheck OK: selected job_id={selected.get('job_id')} source={source}")
    except ValueError as exc:
        _print_job_states(jobs)
        print(f"Healthcheck failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

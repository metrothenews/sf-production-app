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
from so_sync.render_contract import (  # noqa: E402
    OUTPUTS_INDEX_TAB,
    RENDER_CANONICAL_HEADERS,
    RENDER_JOBS_TAB,
    RENDER_RUNS_TAB,
)

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


def _header_diff(expected: List[str], actual: List[str]) -> str:
    lines = ["Header mismatch:"]
    width = max(len(expected), len(actual))
    for i in range(width):
        e = expected[i] if i < len(expected) else "<missing>"
        a = actual[i] if i < len(actual) else "<missing>"
        marker = "OK" if e == a else "DIFF"
        lines.append(f"  col {i + 1:02d} [{marker}] expected={e!r} actual={a!r}")
    return "\n".join(lines)


def _ensure_tabs_exist(gw: SheetsGateway, sheet_id: str, tabs: List[str]) -> None:
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
            f"Missing required render tabs in CONTROL_SHEET_ID={sheet_id}: {joined}.\n"
            "Create them in Google Sheets UI:\n"
            "1) Open the CONTROL sheet.\n"
            "2) Add tabs named exactly: Render_Jobs, Outputs_Index, Render_Runs.\n"
            "3) Re-run scripts/bootstrap_render_tabs.py."
        )


def _ensure_header(gw: SheetsGateway, sheet_id: str, tab_name: str, expected_header: List[str]) -> None:
    values = _read_tab_values(gw, sheet_id, tab_name)
    if not values:
        gw.write(sheet_id, [{"range": f"{_quote_tab(tab_name)}!A1", "values": [expected_header]}])
        print(f"Wrote header row: {tab_name}")
        return
    actual = [str(x).strip() for x in values[0]]
    while actual and actual[-1] == "":
        actual.pop()
    if actual == expected_header:
        print(f"Header OK (no-op): {tab_name}")
        return
    if actual and expected_header[: len(actual)] == actual:
        gw.write(sheet_id, [{"range": f"{_quote_tab(tab_name)}!A1", "values": [expected_header]}])
        print(f"Extended header row with new trailing columns: {tab_name}")
        return
    raise RuntimeError(f"Header mismatch for tab={tab_name}\n{_header_diff(expected_header, actual)}")


def _verify_headers(gw: SheetsGateway, sheet_id: str, tabs: List[str]) -> None:
    for tab in tabs:
        values = _read_tab_values(gw, sheet_id, tab)
        actual = [str(x).strip() for x in (values[0] if values else [])]
        while actual and actual[-1] == "":
            actual.pop()
        expected = RENDER_CANONICAL_HEADERS[tab]
        if actual != expected:
            raise RuntimeError(f"Post-write verification failed for {tab}\n{_header_diff(expected, actual)}")


def main() -> int:
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", DEFAULT_BASE).strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    control_sheet_id = _must_env("CONTROL_SHEET_ID")

    gw = SheetsGateway(base_url=base_url, api_key=api_key)
    tabs = [RENDER_JOBS_TAB, OUTPUTS_INDEX_TAB, RENDER_RUNS_TAB]
    _ensure_tabs_exist(gw, control_sheet_id, tabs)
    for tab in tabs:
        _ensure_header(gw, control_sheet_id, tab, RENDER_CANONICAL_HEADERS[tab])
    _verify_headers(gw, control_sheet_id, tabs)
    print("Render tabs bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

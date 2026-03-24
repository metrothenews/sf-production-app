#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway  # noqa: E402


GATEWAY_BASE = os.environ.get(
    "SHEETS_JSON_BASE_URL",
    os.environ.get("SO_GATEWAY_BASE", "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"),
).rstrip("/")
GATEWAY_KEY = os.environ.get("SHEETS_JSON_API_KEY", os.environ.get("SO_GATEWAY_KEY", "")).strip()

SHEET_INTAKE = os.environ.get("SHEET_INTAKE", "YOUR_GOOGLE_SHEET_ID")
INTAKE_TAB = os.environ.get("INTAKE_TAB", "mode")
INTAKE_ROW = int(os.environ.get("INTAKE_ROW", "2"))
FOUND_FOOTAGE_MODE_TAB = os.environ.get("FOUND_FOOTAGE_MODE_TAB", "found-footage-mode")
PERFORMANCE_MODE_TAB = os.environ.get("PERFORMANCE_MODE_TAB", "performance-footage-mode")
HYBRID_MODE_TAB = os.environ.get("HYBRID_MODE_TAB", "hybrid-mode")
MODE_CONTROL_TAB = os.environ.get("MODE_CONTROL_TAB", "mode").strip()


def _read_row_dict(gw: SheetsGateway, *, tab_name: str) -> Dict[str, Any]:
    rng = f"'{tab_name}'!A1:AZ{INTAKE_ROW}"
    data = gw.read(SHEET_INTAKE, [rng])
    values = data["valueRanges"][0].get("values", [])
    if len(values) < 2:
        raise RuntimeError(f"Expected header + at least one row from {rng}")
    header = values[0]
    row = values[-1]
    out: Dict[str, Any] = {}
    for i, h in enumerate(header):
        k = str(h).strip()
        if not k:
            continue
        out[k] = row[i] if i < len(row) else ""
    return out


def _normalize_mode_label(value: str) -> str:
    out = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    return " ".join(out.split())


def _mode_to_tab_name(mode_value: str) -> str:
    normalized = _normalize_mode_label(mode_value)
    if not normalized:
        return ""
    if "hybrid" in normalized:
        return HYBRID_MODE_TAB
    if "performance" in normalized:
        return PERFORMANCE_MODE_TAB
    if "found" in normalized and "footage" in normalized:
        return FOUND_FOOTAGE_MODE_TAB
    return ""


def _is_mode_control_tab(tab_name: str) -> bool:
    normalized = str(tab_name or "").strip().lower()
    if not normalized:
        return False
    configured = str(MODE_CONTROL_TAB or "").strip().lower() or "mode"
    return normalized in {configured, "api-read"}


def main() -> int:
    gw = SheetsGateway(GATEWAY_BASE, GATEWAY_KEY)
    control = _read_row_dict(gw, tab_name=INTAKE_TAB)
    mode_value = str(control.get("Mode") or "").strip()
    selected_tab = _mode_to_tab_name(mode_value)
    out: Dict[str, Any] = dict(control)
    if _is_mode_control_tab(INTAKE_TAB) and selected_tab:
        routed = _read_row_dict(gw, tab_name=selected_tab)
        for key, val in routed.items():
            if str(val or "").strip():
                out[key] = val
            elif key not in out:
                out[key] = val
    out["_mode"] = mode_value
    out["_source_tab"] = selected_tab or INTAKE_TAB

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

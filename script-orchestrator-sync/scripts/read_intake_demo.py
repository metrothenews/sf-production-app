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
INTAKE_TAB = os.environ.get("INTAKE_TAB", "Sheet2")
INTAKE_ROW = int(os.environ.get("INTAKE_ROW", "2"))


def main() -> int:
    gw = SheetsGateway(GATEWAY_BASE, GATEWAY_KEY)

    rng = f"{INTAKE_TAB}!A1:AE{INTAKE_ROW}"
    data = gw.read(SHEET_INTAKE, [rng])
    values = data["valueRanges"][0].get("values", [])

    if len(values) < 2:
        raise SystemExit(f"Expected header + at least one row from {rng}")

    header = values[0]
    row = values[-1]

    out: Dict[str, Any] = {}
    for i, h in enumerate(header):
        k = str(h).strip()
        if not k:
            continue
        out[k] = row[i] if i < len(row) else ""

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

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

SHEET_PROD = os.environ.get("SHEET_PROD", "YOUR_GOOGLE_SHEET_ID")
PROD_TAB = os.environ.get("PROD_TAB", "SO Campaigns")

# Safety: only writes when APPLY=1
APPLY = os.environ.get("APPLY", "0") == "1"


def main() -> int:
    gw = SheetsGateway(GATEWAY_BASE, GATEWAY_KEY)

    updates: List[Dict[str, Any]] = [
        {"range": f"'{PROD_TAB}'!C2", "values": [["write_prod_demo: hello"]]},  # notes column in your sheet is C
    ]

    print("Planned update:")
    for u in updates:
        print(f"  {u['range']}: {u['values'][0][0]!r}")

    if not APPLY:
        print("\nNo write performed. Set APPLY=1 to write.")
        return 0

    resp = gw.write(SHEET_PROD, updates)
    print("WRITE OK:", resp.get("ok"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

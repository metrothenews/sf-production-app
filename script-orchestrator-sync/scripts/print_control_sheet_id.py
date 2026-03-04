#!/usr/bin/env python3
from __future__ import annotations

import os

PLACEHOLDER_VALUES = {"", "PUT_CONTROL_SHEET_ID_HERE", "your_control_sheet_id"}


def main() -> int:
    control_sheet_id = os.environ.get("CONTROL_SHEET_ID", "").strip()
    base_url = os.environ.get(
        "SHEETS_JSON_BASE_URL",
        "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app",
    ).strip()

    print(f"SHEETS_JSON_BASE_URL={base_url}")
    print(f"CONTROL_SHEET_ID={control_sheet_id or '<unset>'}")

    if control_sheet_id in PLACEHOLDER_VALUES:
        print("WARNING: CONTROL_SHEET_ID is unset or placeholder. Set it before running smoke.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

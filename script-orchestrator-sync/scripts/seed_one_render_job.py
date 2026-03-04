#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from render_agent.job_contract import DEFAULT_OUTPUT_FOLDER_ID, parse_id_list  # noqa: E402
from so_sync.gateway import SheetsGateway  # noqa: E402
from so_sync.render_contract import (  # noqa: E402
    RENDER_CANONICAL_HEADERS,
    RENDER_HEADER_ALIASES,
    RENDER_JOBS_TAB,
    RENDER_STATUS_QUEUED,
    RENDER_STATUS_RETRY,
)
from so_sync.sheet_contract import build_header_index, resolve_header_key  # noqa: E402


OPTIONAL_ALIASES = {
    "target_count": ["n", "target count", "clip count", "number of outputs", "number_of_outputs"],
    "source_folder_ids": ["source folder ids", "folders", "drive folder ids"],
    "source_file_ids": ["source file ids", "files", "drive file ids"],
    "select_mode": ["select mode"],
    "select_seed": ["select seed"],
    "output_folder_id": ["output folder id"],
}


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _must_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _quote_tab(tab_name: str) -> str:
    escaped = str(tab_name).replace("'", "''")
    return f"'{escaped}'"


def _first_values(resp: dict[str, Any]) -> list[list[Any]]:
    return ((resp.get("valueRanges") or [{}])[0].get("values") or [])


def _find_row_index(rows: list[list[Any]], key_idx: int, key_val: str) -> int:
    needle = str(key_val).strip()
    for i, row in enumerate(rows):
        if key_idx < len(row) and str(row[key_idx]).strip() == needle:
            return i
    return -1


def _set_field(
    row: list[Any],
    header_index: dict[str, int],
    key: str,
    value: Any,
    written: dict[str, Any],
) -> None:
    idx = header_index.get(resolve_header_key(key, {**RENDER_HEADER_ALIASES, **OPTIONAL_ALIASES}))
    if idx is None:
        return
    while len(row) <= idx:
        row.append("")
    row[idx] = value
    written[key] = value


def build_seed_values(
    *,
    job_id: str,
    source_folder_ids: list[str],
    source_file_ids: list[str],
    target_count: int,
    status: str,
    select_mode: str,
    select_seed: str,
    output_folder_id: str,
    now_utc: str,
) -> dict[str, Any]:
    use_legacy_input_folder = not bool(source_file_ids)
    return {
        "render_job_id": job_id,
        "enabled": "TRUE",
        "status": status,
        "target_count": str(max(0, int(target_count))),
        "source_folder_ids": ",".join(source_folder_ids),
        "source_file_ids": ",".join(source_file_ids),
        "select_mode": select_mode,
        "select_seed": str(select_seed),
        "output_folder_id": output_folder_id or DEFAULT_OUTPUT_FOLDER_ID,
        "input_drive_folder_id": ",".join(source_folder_ids) if use_legacy_input_folder else "",
        "output_drive_folder_id": output_folder_id or DEFAULT_OUTPUT_FOLDER_ID,
        "lease_owner": "",
        "lease_until_utc": "",
        "run_id": "",
        "error_msg": "",
        "updated_utc": now_utc,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed/update one Render_Jobs row so it is runnable.")
    parser.add_argument("--sheet-id", default=os.environ.get("CONTROL_SHEET_ID", "").strip())
    parser.add_argument("--tab", default=RENDER_JOBS_TAB)
    parser.add_argument("--row", type=int, default=0)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--source-folder-id", action="append", default=[])
    parser.add_argument("--source-file-ids", default="")
    parser.add_argument("--target-count", type=int, required=True)
    parser.add_argument("--status", default=RENDER_STATUS_QUEUED)
    parser.add_argument("--select-mode", default="shuffle_seeded")
    parser.add_argument("--select-seed", default="12345")
    parser.add_argument("--output-folder-id", default=DEFAULT_OUTPUT_FOLDER_ID)
    args = parser.parse_args()

    sheet_id = str(args.sheet_id or "").strip() or _must_env("CONTROL_SHEET_ID")
    base_url = os.environ.get("SHEETS_JSON_BASE_URL", "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app").strip().rstrip("/")
    api_key = os.environ.get("SHEETS_JSON_API_KEY", "").strip()
    gw = SheetsGateway(base_url=base_url, api_key=api_key)

    print(
        json.dumps(
            {
                "tab": RENDER_JOBS_TAB,
                "job_key": "render_job_id",
                "runnable_statuses": [RENDER_STATUS_QUEUED, RENDER_STATUS_RETRY],
                "canonical_headers": RENDER_CANONICAL_HEADERS[RENDER_JOBS_TAB],
                "optional_source_fields": [
                    "target_count",
                    "source_folder_ids",
                    "source_file_ids",
                    "select_mode",
                    "select_seed",
                    "output_folder_id",
                ],
            },
            sort_keys=True,
        )
    )

    range_addr = f"{_quote_tab(args.tab)}!A1:ZZ"
    values = _first_values(gw.read(sheet_id, [range_addr]))
    if not values:
        raise RuntimeError(f"Tab '{args.tab}' is empty or missing headers")
    header = [str(x).strip() for x in values[0]]
    rows = values[1:]
    aliases = dict(RENDER_HEADER_ALIASES)
    aliases.update(OPTIONAL_ALIASES)
    header_index = build_header_index(header, aliases=aliases)
    key_idx = header_index.get(resolve_header_key("render_job_id", aliases))
    if key_idx is None:
        raise RuntimeError("Missing render_job_id column")

    source_folder_ids = parse_id_list(args.source_folder_id)
    source_file_ids = parse_id_list(args.source_file_ids)
    if not source_folder_ids and not source_file_ids:
        raise RuntimeError("At least one source reference is required (--source-folder-id or --source-file-ids)")

    row_idx = (args.row - 2) if args.row >= 2 else _find_row_index(rows, key_idx, args.job_id)
    is_append = row_idx < 0
    if is_append:
        row = [""] * len(header)
        row_num = len(rows) + 2
    else:
        row = list(rows[row_idx])
        row_num = row_idx + 2
        while len(row) < len(header):
            row.append("")

    now = _utc_now_iso()
    to_write = build_seed_values(
        job_id=args.job_id,
        source_folder_ids=source_folder_ids,
        source_file_ids=source_file_ids,
        target_count=args.target_count,
        status=args.status,
        select_mode=args.select_mode,
        select_seed=str(args.select_seed),
        output_folder_id=args.output_folder_id,
        now_utc=now,
    )
    written: dict[str, Any] = {}
    for key, value in to_write.items():
        _set_field(row, header_index, key, value, written)
    created_idx = header_index.get("created_utc")
    created_existing = ""
    if created_idx is not None and created_idx < len(row):
        created_existing = str(row[created_idx] or "").strip()
    _set_field(row, header_index, "created_utc", created_existing or now, written)

    gw.write(sheet_id, [{"range": f"{_quote_tab(args.tab)}!A{row_num}", "values": [row]}])

    print(
        json.dumps(
            {
                "sheet_id": sheet_id,
                "tab": args.tab,
                "row_num": row_num,
                "job_id": args.job_id,
                "is_append": is_append,
                "written": written,
                "permissions_reminder": {
                    "proxy_source_share": "drive-cdn-proxy@drive-cdn-proxy.iam.gserviceaccount.com",
                    "output_folder_drive_creds": "local agent GOOGLE_APPLICATION_CREDENTIALS must have write access",
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

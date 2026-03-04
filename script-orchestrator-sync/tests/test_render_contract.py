from __future__ import annotations

from so_sync.render_contract import OUTPUTS_INDEX_TAB, RENDER_CANONICAL_HEADERS, RENDER_HEADER_ALIASES, RENDER_JOBS_TAB
from so_sync.sheet_contract import build_header_index, dict_to_row, pad_row, resolve_header_key, row_to_canonical_dict


def test_render_header_alias_lookup_and_normalization() -> None:
    header = [" Render Job ID ", "Output drive folder id", "LEASE UNTIL UTC", "enabled"]
    idx = build_header_index(header, aliases=RENDER_HEADER_ALIASES)
    assert idx["render_job_id"] == 0
    assert idx["output_drive_folder_id"] == 1
    assert idx["lease_until_utc"] == 2
    assert resolve_header_key("render job id", RENDER_HEADER_ALIASES) == "render_job_id"


def test_render_ragged_projection_pads_missing_cells() -> None:
    header = RENDER_CANONICAL_HEADERS[RENDER_JOBS_TAB]
    idx = build_header_index(header, aliases=RENDER_HEADER_ALIASES)
    projected = row_to_canonical_dict(
        ["job-1", "TRUE", "5"],
        canonical_headers=header,
        header_index=idx,
    )
    assert projected["render_job_id"] == "job-1"
    assert projected["enabled"] == "TRUE"
    assert projected["priority"] == "5"
    assert projected["status"] is None


def test_outputs_row_padding_matches_header_width() -> None:
    headers = RENDER_CANONICAL_HEADERS[OUTPUTS_INDEX_TAB]
    row = dict_to_row({"output_id": "o1", "filename": "x.txt"}, headers)
    assert len(row) == len(headers)
    assert len(pad_row(["o1"], len(headers))) == len(headers)

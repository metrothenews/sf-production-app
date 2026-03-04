from __future__ import annotations

from so_sync.sheet_contract import (
    build_header_index,
    dict_to_row,
    pad_row,
    resolve_header_key,
    row_to_canonical_dict,
)


def test_header_lookup_handles_case_whitespace_and_aliases() -> None:
    header = [" Job ID ", " SOURCE COLUMN ", "Target Column", "Schema Version "]
    idx = build_header_index(header)

    assert idx["job_id"] == 0
    assert idx["source_col"] == 1
    assert idx["target_col"] == 2
    assert idx["schema_version"] == 3
    assert resolve_header_key("  fingerprint  ") == "source_fingerprint"


def test_alias_row_controls_logical_names_when_present() -> None:
    physical_header = ["Column A", "Column B", "Column C"]
    alias_row = ["campaign_id", "Audio Chip", ""]
    idx = build_header_index(physical_header, alias_row=alias_row)

    row = ["cmp_001", "audio.mp3", "ignored"]
    projected = row_to_canonical_dict(
        row,
        canonical_headers=["campaign_id", "audio_chip", "column_c"],
        header_index=idx,
    )

    assert projected["campaign_id"] == "cmp_001"
    assert projected["audio_chip"] == "audio.mp3"
    assert projected["column_c"] == "ignored"


def test_ragged_rows_are_padded_to_header_width_before_projection() -> None:
    header = ["first", "second", "third"]
    idx = build_header_index(header)

    projected = row_to_canonical_dict(
        ["v1", "v2"],
        canonical_headers=["first", "second", "third"],
        header_index=idx,
    )

    assert projected["first"] == "v1"
    assert projected["second"] == "v2"
    assert projected["third"] is None


def test_writes_are_exact_header_width() -> None:
    headers = ["a", "b", "c", "d"]
    row = dict_to_row({"a": 1, "c": 3}, headers)

    assert row == [1, None, 3, None]
    assert len(row) == len(headers)
    assert len(pad_row([1, 2], len(headers))) == len(headers)

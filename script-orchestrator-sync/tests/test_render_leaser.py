from __future__ import annotations

import copy
import re
from typing import Any, Dict, List

from so_sync.render_contract import (
    OUTPUTS_INDEX_TAB,
    RENDER_CANONICAL_HEADERS,
    RENDER_STATUS_FAILED,
    RENDER_JOBS_TAB,
    RENDER_RUNS_TAB,
    RENDER_STATUS_LEASED,
    RENDER_STATUS_RETRY,
    RENDER_STATUS_RUNNING,
    RENDER_STATUS_SUCCESS,
)
from so_sync.render_leaser import lease_one_render_job, mark_failed, mark_running, mark_success


class FakeGateway:
    def __init__(self, tabs: Dict[str, List[List[Any]]]):
        self.tabs = {k: copy.deepcopy(v) for k, v in tabs.items()}

    @staticmethod
    def _parse_tab(range_addr: str) -> str:
        m = re.search(r"'([^']+)'!", range_addr)
        if not m:
            raise ValueError(f"Unsupported range format: {range_addr}")
        return m.group(1)

    @staticmethod
    def _parse_row(range_addr: str) -> int:
        m = re.search(r"!A(\d+)", range_addr)
        if not m:
            raise ValueError(f"Unsupported range format: {range_addr}")
        return int(m.group(1))

    def read(self, sheet_id: str, ranges: List[str], **kwargs: Any) -> Dict[str, Any]:
        out = []
        for r in ranges:
            tab = self._parse_tab(r)
            out.append({"range": r, "values": copy.deepcopy(self.tabs.get(tab, []))})
        return {"ok": True, "valueRanges": out}

    def write(self, sheet_id: str, updates: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        for upd in updates:
            tab = self._parse_tab(upd["range"])
            row_num = self._parse_row(upd["range"])
            values = upd.get("values", [])
            table = self.tabs.setdefault(tab, [])
            while len(table) < row_num - 1:
                table.append([])
            for i, row in enumerate(values):
                idx = row_num - 1 + i
                if idx < len(table):
                    table[idx] = list(row)
                else:
                    table.append(list(row))
        return {"ok": True}


def _header(tab: str) -> List[str]:
    return RENDER_CANONICAL_HEADERS[tab]


def test_lease_selects_highest_priority_then_oldest_created() -> None:
    gw = FakeGateway(
        {
            RENDER_JOBS_TAB: [
                _header(RENDER_JOBS_TAB),
                ["job-low", "TRUE", "1", "queued", "", "", "", "", "", "", "noop", "", "", "", "local", "", "", "", "", "", "", "", "", "2026-01-02T00:00:00+00:00", "", "", ""],
                ["job-high-new", "TRUE", "10", "queued", "", "", "", "", "", "", "noop", "", "", "", "local", "", "", "", "", "", "", "", "", "2026-01-03T00:00:00+00:00", "", "", ""],
                ["job-high-old", "TRUE", "10", "queued", "", "", "", "", "", "", "noop", "", "", "", "local", "", "", "", "", "", "", "", "", "2026-01-01T00:00:00+00:00", "", "", ""],
            ],
            OUTPUTS_INDEX_TAB: [_header(OUTPUTS_INDEX_TAB)],
            RENDER_RUNS_TAB: [_header(RENDER_RUNS_TAB)],
        }
    )
    leased = lease_one_render_job(gw, "sheet", "2026-03-01T00:00:00+00:00", 900, "agent-1")
    assert leased is not None
    assert leased["render_job_id"] == "job-high-old"
    assert leased["status"] == RENDER_STATUS_LEASED
    assert leased["lease_owner"] == "agent-1"


def test_status_transitions_running_to_success() -> None:
    gw = FakeGateway(
        {
            RENDER_JOBS_TAB: [
                _header(RENDER_JOBS_TAB),
                ["job-1", "TRUE", "1", "queued", "", "", "", "", "", "", "noop", "", "", "", "local", "", "", "", "", "", "", "", "", "2026-01-01T00:00:00+00:00", "", "", ""],
            ],
            OUTPUTS_INDEX_TAB: [_header(OUTPUTS_INDEX_TAB)],
            RENDER_RUNS_TAB: [_header(RENDER_RUNS_TAB)],
        }
    )
    mark_running(gw, "sheet", "job-1", "run-1", now_utc="2026-03-01T00:00:00+00:00")
    rows = gw.tabs[RENDER_JOBS_TAB]
    status_idx = _header(RENDER_JOBS_TAB).index("status")
    run_idx = _header(RENDER_JOBS_TAB).index("run_id")
    assert rows[1][status_idx] == RENDER_STATUS_RUNNING
    assert rows[1][run_idx] == "run-1"

    mark_success(gw, "sheet", "job-1", "run-1", now_utc="2026-03-01T00:01:00+00:00")
    rows2 = gw.tabs[RENDER_JOBS_TAB]
    lease_owner_idx = _header(RENDER_JOBS_TAB).index("lease_owner")
    lease_until_idx = _header(RENDER_JOBS_TAB).index("lease_until_utc")
    assert rows2[1][status_idx] == RENDER_STATUS_SUCCESS
    assert rows2[1][run_idx] == "run-1"
    assert rows2[1][lease_owner_idx] == ""
    assert rows2[1][lease_until_idx] == ""


def test_mark_failed_clears_lease_and_sets_error_and_retry() -> None:
    gw = FakeGateway(
        {
            RENDER_JOBS_TAB: [
                _header(RENDER_JOBS_TAB),
                ["job-1", "TRUE", "1", "queued", "", "", "", "", "", "", "noop", "", "", "", "local", "", "", "agent", "2026-03-01T00:10:00+00:00", "", "0", "3", "", "2026-01-01T00:00:00+00:00", "", "", ""],
            ],
            OUTPUTS_INDEX_TAB: [_header(OUTPUTS_INDEX_TAB)],
            RENDER_RUNS_TAB: [_header(RENDER_RUNS_TAB)],
        }
    )
    mark_failed(gw, "sheet", "job-1", "run-1", "boom", now_utc="2026-03-01T00:01:00+00:00")
    row = gw.tabs[RENDER_JOBS_TAB][1]
    header = _header(RENDER_JOBS_TAB)
    assert row[header.index("status")] == RENDER_STATUS_RETRY
    assert row[header.index("run_id")] == "run-1"
    assert row[header.index("lease_owner")] == ""
    assert row[header.index("lease_until_utc")] == ""
    assert row[header.index("attempts")] == 1
    assert row[header.index("max_attempts")] == 3
    assert row[header.index("last_error_utc")] == "2026-03-01T00:01:00+00:00"
    assert row[header.index("error_msg")] == "boom"


def test_mark_failed_transitions_to_failed_at_max_attempts() -> None:
    gw = FakeGateway(
        {
            RENDER_JOBS_TAB: [
                _header(RENDER_JOBS_TAB),
                ["job-1", "TRUE", "1", "queued", "", "", "", "", "", "", "noop", "", "", "", "local", "", "", "agent", "2026-03-01T00:10:00+00:00", "", "2", "3", "", "2026-01-01T00:00:00+00:00", "", "", ""],
            ],
            OUTPUTS_INDEX_TAB: [_header(OUTPUTS_INDEX_TAB)],
            RENDER_RUNS_TAB: [_header(RENDER_RUNS_TAB)],
        }
    )
    mark_failed(gw, "sheet", "job-1", "run-2", "boom2", now_utc="2026-03-01T00:02:00+00:00")
    row = gw.tabs[RENDER_JOBS_TAB][1]
    header = _header(RENDER_JOBS_TAB)
    assert row[header.index("status")] == RENDER_STATUS_FAILED
    assert row[header.index("attempts")] == 3
    assert row[header.index("lease_owner")] == ""
    assert row[header.index("lease_until_utc")] == ""

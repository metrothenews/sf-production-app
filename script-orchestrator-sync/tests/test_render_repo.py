from __future__ import annotations

import copy
import re
from typing import Any, Dict, List

from so_sync.render_contract import OUTPUTS_INDEX_TAB, RENDER_CANONICAL_HEADERS, RENDER_JOBS_TAB, RENDER_RUNS_TAB
from so_sync.render_repo import (
    append_outputs_index_rows,
    count_outputs_for_run,
    find_render_run_by_id,
    list_render_jobs,
    read_outputs_index_all,
    read_render_jobs_all,
    upsert_render_run,
)


class FakeGateway:
    def __init__(self, tabs: Dict[str, List[List[Any]]]):
        self.tabs = {k: copy.deepcopy(v) for k, v in tabs.items()}

    @staticmethod
    def _parse_tab_row(range_addr: str) -> tuple[str, int]:
        m = re.search(r"'([^']+)'!A(\d+)", range_addr)
        if not m:
            raise ValueError(f"Unsupported range format: {range_addr}")
        return m.group(1), int(m.group(2))

    def read(self, sheet_id: str, ranges: List[str], **kwargs: Any) -> Dict[str, Any]:
        out = []
        for r in ranges:
            tab_match = re.search(r"'([^']+)'!", r)
            if not tab_match:
                raise ValueError(f"Unsupported range format: {r}")
            tab = tab_match.group(1)
            values = copy.deepcopy(self.tabs.get(tab, []))
            out.append({"range": r, "values": values})
        return {"ok": True, "valueRanges": out}

    def write(self, sheet_id: str, updates: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        for upd in updates:
            tab, row_num = self._parse_tab_row(upd["range"])
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


def _render_job_header() -> List[str]:
    return RENDER_CANONICAL_HEADERS[RENDER_JOBS_TAB]


def _outputs_header() -> List[str]:
    return RENDER_CANONICAL_HEADERS[OUTPUTS_INDEX_TAB]


def _runs_header() -> List[str]:
    return RENDER_CANONICAL_HEADERS[RENDER_RUNS_TAB]


def test_read_render_jobs_handles_ragged_rows_and_enabled_filter() -> None:
    header = _render_job_header()
    gw = FakeGateway(
        {
            RENDER_JOBS_TAB: [
                header,
                ["job-a", "TRUE", "9", "queued", "c1"],
                ["job-b", "false", "1", "queued", "c2"],
            ],
            OUTPUTS_INDEX_TAB: [_outputs_header()],
            RENDER_RUNS_TAB: [_runs_header()],
        }
    )
    rows = read_render_jobs_all(gw, "sheet")
    assert rows[0]["render_job_id"] == "job-a"
    assert rows[0]["renderer"] is None

    enabled = list_render_jobs(gw, "sheet", enabled=True)
    assert [r["render_job_id"] for r in enabled] == ["job-a"]


def test_outputs_append_pads_to_exact_width() -> None:
    header = _outputs_header()
    gw = FakeGateway(
        {
            RENDER_JOBS_TAB: [_render_job_header()],
            OUTPUTS_INDEX_TAB: [header],
            RENDER_RUNS_TAB: [_runs_header()],
        }
    )
    count = append_outputs_index_rows(gw, "sheet", [{"output_id": "o1", "filename": "x.txt"}])
    assert count == 1
    written = gw.tabs[OUTPUTS_INDEX_TAB][1]
    assert len(written) == len(header)
    assert written[0] == "o1"


def test_render_run_upsert_preserves_required_fields() -> None:
    gw = FakeGateway(
        {
            RENDER_JOBS_TAB: [_render_job_header()],
            OUTPUTS_INDEX_TAB: [_outputs_header()],
            RENDER_RUNS_TAB: [_runs_header()],
        }
    )
    run = {
        "run_id": "r-1",
        "run_type": "render",
        "render_job_id": "job-a",
        "status": "queued",
        "created_utc": "2026-01-01T00:00:00+00:00",
        "updated_utc": "2026-01-01T00:00:00+00:00",
    }
    upsert_render_run(gw, "sheet", run)
    found = find_render_run_by_id(gw, "sheet", "r-1")
    assert found is not None
    assert found["run_type"] == "render"
    assert found["status"] == "queued"


def test_count_outputs_for_run_matches_exact_keys() -> None:
    gw = FakeGateway(
        {
            RENDER_JOBS_TAB: [_render_job_header()],
            OUTPUTS_INDEX_TAB: [
                _outputs_header(),
                ["o1", "run-1", "job-a", "", "noop", "", "", "a.txt", "", "", "", ""],
                ["o2", "run-2", "job-a", "", "noop", "", "", "b.txt", "", "", "", ""],
                ["o3", "run-1", "job-a", "", "image", "", "", "c.png", "", "", "", ""],
            ],
            RENDER_RUNS_TAB: [_runs_header()],
        }
    )
    rows = read_outputs_index_all(gw, "sheet")
    count = count_outputs_for_run(rows, run_id="run-1", render_job_id="job-a", artifact_type="noop")
    assert count == 1

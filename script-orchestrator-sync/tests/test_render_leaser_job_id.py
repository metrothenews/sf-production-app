from __future__ import annotations

import copy
import re
from typing import Any

from so_sync.render_contract import RENDER_CANONICAL_HEADERS, RENDER_JOBS_TAB
from so_sync.render_leaser import lease_one_render_job


class FakeGateway:
    def __init__(self, tabs: dict[str, list[list[Any]]]):
        self.tabs = {k: copy.deepcopy(v) for k, v in tabs.items()}

    def read(self, sheet_id: str, ranges: list[str], **kwargs: Any) -> dict[str, Any]:
        out = []
        for r in ranges:
            m = re.search(r"'([^']+)'!", r)
            if not m:
                raise ValueError(r)
            tab = m.group(1)
            out.append({"range": r, "values": copy.deepcopy(self.tabs.get(tab, []))})
        return {"ok": True, "valueRanges": out}

    def write(self, sheet_id: str, updates: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        for upd in updates:
            m = re.search(r"'([^']+)'!A(\d+)", upd["range"])
            if not m:
                raise ValueError(upd["range"])
            tab = m.group(1)
            row_num = int(m.group(2))
            table = self.tabs.setdefault(tab, [])
            while len(table) < row_num - 1:
                table.append([])
            for i, row in enumerate(upd.get("values", [])):
                idx = row_num - 1 + i
                if idx < len(table):
                    table[idx] = list(row)
                else:
                    table.append(list(row))
        return {"ok": True}


def _header() -> list[str]:
    return RENDER_CANONICAL_HEADERS[RENDER_JOBS_TAB]


def _row(render_job_id: str, status: str = "queued", enabled: str = "TRUE") -> list[str]:
    header = _header()
    data = {h: "" for h in header}
    data["render_job_id"] = render_job_id
    data["enabled"] = enabled
    data["status"] = status
    data["priority"] = "1"
    data["created_utc"] = "2026-01-01T00:00:00+00:00"
    return [data.get(h, "") for h in header]


def test_lease_one_render_job_honors_job_id_env(monkeypatch) -> None:
    gw = FakeGateway(
        {
            RENDER_JOBS_TAB: [
                _header(),
                _row("job_a", status="queued"),
                _row("job_b", status="queued"),
            ]
        }
    )
    monkeypatch.setenv("JOB_ID", "job_b")
    leased = lease_one_render_job(gw, "sheet", "2026-01-01T00:00:00+00:00", 900, "agent")
    assert leased is not None
    assert leased["render_job_id"] == "job_b"

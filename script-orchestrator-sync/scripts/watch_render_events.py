#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def _find_latest_events_file() -> Path | None:
    root = Path("/var/folders/9g/cbcs6fhs62v2frgcxld4gcn40000gn/T/so_work")
    candidates = sorted(root.glob("*/logs/events.jsonl"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if candidates:
        return candidates[0]
    tmp_events = sorted((Path("/tmp") / "so_render_logs").glob("*.events.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return tmp_events[0] if tmp_events else None


def _format_event(obj: dict[str, Any]) -> str:
    return (
        f"{obj.get('ts_utc','')} "
        f"run={obj.get('run_id','')} "
        f"cut={obj.get('cut_idx',0)} "
        f"stage={obj.get('stage','')} "
        f"event={obj.get('event','')} "
        f"sev={obj.get('severity','')} "
        f"code={obj.get('code','')} "
        f"msg={obj.get('message','')}"
    ).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Tail render events JSONL.")
    ap.add_argument("--file", default="", help="Path to events.jsonl (defaults to latest run file).")
    ap.add_argument("--follow", action="store_true", help="Keep tailing.")
    ap.add_argument("--lines", type=int, default=30, help="Initial lines to show.")
    args = ap.parse_args()

    events_path = Path(args.file).expanduser().resolve() if args.file else _find_latest_events_file()
    if events_path is None or not events_path.exists():
        raise SystemExit("No events.jsonl found.")

    print(f"watching_events file={events_path}")
    lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-max(1, int(args.lines)) :]:
        try:
            payload = json.loads(line)
        except Exception:
            print(line)
            continue
        print(_format_event(payload))

    if not args.follow:
        return 0

    with events_path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(0, 2)
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.4)
                continue
            try:
                payload = json.loads(line)
            except Exception:
                print(line.rstrip("\n"))
                continue
            print(_format_event(payload))


if __name__ == "__main__":
    raise SystemExit(main())


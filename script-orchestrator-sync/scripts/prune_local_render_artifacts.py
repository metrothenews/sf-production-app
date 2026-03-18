#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path


def _repo_work_root() -> Path:
    return Path(__file__).resolve().parents[1] / "work"


def _tmp_so_work_root() -> Path:
    return Path(tempfile.gettempdir()) / "so_work"


def _delete_path(path: Path) -> None:
    if path.is_file() or path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _iter_delete_candidates(root: Path, *, failure_logs_only: bool) -> list[Path]:
    out: list[Path] = []
    if not root.exists() or not root.is_dir():
        return out

    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not failure_logs_only:
            out.append(child)
            continue
        if child.is_file() or child.is_symlink():
            out.append(child)
            continue
        logs_dir = child / "logs"
        if logs_dir.exists() and logs_dir.is_dir():
            for grandchild in sorted(child.iterdir(), key=lambda p: p.name):
                if grandchild.name == "logs":
                    continue
                out.append(grandchild)
        else:
            out.append(child)
    return out


def _collect_roots() -> list[Path]:
    return [_tmp_so_work_root(), _repo_work_root()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune local render artifacts under temp so_work and repo work/")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print what would be deleted (default).")
    mode.add_argument("--apply", action="store_true", help="Delete matched paths.")
    parser.add_argument(
        "--failure-logs-only",
        action="store_true",
        help="Keep logs/ under each run dir and delete everything else.",
    )
    args = parser.parse_args()

    apply_changes = bool(args.apply)
    roots = _collect_roots()
    total_candidates = 0
    total_deleted = 0

    for root in roots:
        candidates = _iter_delete_candidates(root, failure_logs_only=bool(args.failure_logs_only))
        total_candidates += len(candidates)
        for path in candidates:
            event = {
                "root": str(root),
                "path": str(path),
                "action": "delete" if apply_changes else "would_delete",
            }
            print(json.dumps(event, sort_keys=True))
            if apply_changes:
                _delete_path(path)
                total_deleted += 1

    summary = {
        "mode": "apply" if apply_changes else "dry_run",
        "failure_logs_only": bool(args.failure_logs_only),
        "roots": [str(r) for r in roots],
        "candidates": total_candidates,
        "deleted": total_deleted,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

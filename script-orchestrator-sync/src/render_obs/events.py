from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any


REQUIRED_KEYS = (
    "ts_utc",
    "run_id",
    "render_job_id",
    "cut_idx",
    "stage",
    "attempt",
    "event",
    "severity",
    "code",
    "message",
)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return str(value)


class RenderEventLogger:
    """Append-only JSONL logger for render pipeline events."""

    def __init__(self, path: str, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def rebind(self, path: str) -> None:
        """Switch output path while preserving prior lines."""
        if not self.enabled:
            self.path = Path(path)
            return
        new_path = Path(path)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.resolve() != new_path.resolve():
            previous = self.path.read_text(encoding="utf-8", errors="replace")
            if previous.strip():
                new_path.write_text(previous, encoding="utf-8")
        self.path = new_path

    def emit(
        self,
        *,
        run_id: str,
        render_job_id: str,
        cut_idx: int = 0,
        stage: str = "",
        attempt: int = 0,
        event: str,
        severity: str = "info",
        code: str = "",
        message: str = "",
        **extra: Any,
    ) -> None:
        if not self.enabled:
            return
        payload: dict[str, Any] = {
            "ts_utc": utc_now_iso(),
            "run_id": str(run_id or ""),
            "render_job_id": str(render_job_id or ""),
            "cut_idx": int(cut_idx or 0),
            "stage": str(stage or ""),
            "attempt": int(attempt or 0),
            "event": str(event or ""),
            "severity": str(severity or "info"),
            "code": str(code or ""),
            "message": str(message or ""),
        }
        for key, value in extra.items():
            if value is None:
                continue
            payload[str(key)] = _to_jsonable(value)
        for key in REQUIRED_KEYS:
            if key not in payload:
                payload[key] = ""
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")


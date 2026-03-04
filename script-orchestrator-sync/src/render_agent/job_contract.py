from __future__ import annotations

import json
import re
from typing import Any


DEFAULT_OUTPUT_FOLDER_ID = "YOUR_GOOGLE_SHEET_ID"
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def extract_drive_id(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if _DRIVE_ID_RE.match(raw):
        return raw
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", raw)
    if m:
        return m.group(1)
    m = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", raw)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", raw)
    if m:
        return m.group(1)
    return None


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def parse_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return _dedupe_preserve_order([x for x in (extract_drive_id(str(v)) for v in value) if x])
    raw = str(value).strip()
    if not raw:
        return []
    parsed: list[str] = []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                parsed = [x for x in (extract_drive_id(str(v)) for v in arr) if x]
                return _dedupe_preserve_order(parsed)
        except Exception:
            pass
    parts = re.split(r"[\n,]+", raw)
    parsed = [x for x in (extract_drive_id(p.strip()) for p in parts) if x]
    return _dedupe_preserve_order(parsed)


def parse_target_count(value: Any) -> int:
    try:
        n = int(str(value or "").strip())
        return max(0, n)
    except Exception:
        return 0


def parse_select_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"first", "random", "shuffle_seeded"}:
        return raw
    return "shuffle_seeded"


def parse_select_seed(value: Any) -> str:
    return str(value or "").strip()


def resolve_output_folder_id(value: Any) -> str:
    raw = str(value or "").strip()
    return raw or DEFAULT_OUTPUT_FOLDER_ID

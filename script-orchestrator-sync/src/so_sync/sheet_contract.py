"""Canonical sheet‑as‑DB contract and helper utilities.
This module defines the tab names, the ordered header lists for each tab, and
functions that pad rows to the required length and convert between row lists and
 dictionaries.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional

# ---------------------------------------------------------------------------
# Tab name constants
# ---------------------------------------------------------------------------
INTAKE_TAB = "Intake"
SYNC_JOBS_TAB = "Sync_Jobs"
MAPPING_TAB = "Mapping"
RUNS_LEDGER_TAB = "Runs_Ledger"
ACCESS_CONTROL_TAB = "Access_Control"

# ---------------------------------------------------------------------------
# Canonical header definitions (ordered list)
# ---------------------------------------------------------------------------
CANONICAL_HEADERS: Mapping[str, List[str]] = {
    INTAKE_TAB: [
        "intake_id",
        "created_ts",
        "source_status",
        "source_fingerprint",
        # Business‑specific columns are free‑form; they will appear after these
        # core columns in the sheet.  The contract allows any additional
        # columns – they are simply ignored by the sync engine unless a mapping
        # rule references them.
    ],
    SYNC_JOBS_TAB: [
        "job_id",
        "source_spreadsheet_id",
        "target_spreadsheet_id",
        "source_tab_name",
        "target_tab_name",
        "source_range",
        "target_key_column",
        "write_mode",
        "enabled",
        "schema_version",
        "has_alias_row",   # bool – indicates if source/target sheets have alias row
        "target_range",    # used when write_mode == "overwrite_range"
    ],
    MAPPING_TAB: [
        "job_id",
        "source_col",
        "target_col",
        "transform",
        "default_value",
        "on_missing",
        "trim",
        "schema_version",
    ],
    RUNS_LEDGER_TAB: [
        "run_id",
        "job_id",
        "trigger_ts",
        "status",
        "started_ts",
        "finished_ts",
        "error_msg",
        "rows_processed",
        "source_fingerprint",
        "dedupe_key",
        "schema_version",
    ],
    ACCESS_CONTROL_TAB: [
        "allowed_spreadsheet_id",
        "added_by",
        "added_ts",
        "schema_version",
    ],
}

# ---------------------------------------------------------------------------
# Header alias definitions
# ---------------------------------------------------------------------------
HEADER_ALIASES: Mapping[str, List[str]] = {
    "job_id": ["job id"],
    "source_spreadsheet_id": ["source spreadsheet id", "source sheet id"],
    "target_spreadsheet_id": ["target spreadsheet id", "target sheet id"],
    "source_tab_name": ["source tab"],
    "target_tab_name": ["target tab"],
    "source_range": ["source a1 range"],
    "target_range": ["target a1 range"],
    "target_key_column": ["target key", "target key col"],
    "write_mode": ["write mode"],
    "schema_version": ["schema version"],
    "source_col": ["source column"],
    "target_col": ["target column"],
    "default_value": ["default value"],
    "on_missing": ["on missing"],
    "dedupe_key": ["dedupe key"],
    "source_fingerprint": ["fingerprint"],
}

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------
def normalize_header_key(header: Any) -> str:
    """Normalize header text for tolerant matching.
    Handles case, whitespace, and common punctuation variants.
    """
    text = str(header or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = text.replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]", "", text)
    return re.sub(r"_+", "_", text).strip("_")


def resolve_header_key(header: Any, aliases: Mapping[str, List[str]] | None = None) -> str:
    """Resolve header text to a canonical key using known aliases."""
    normalized = normalize_header_key(header)
    if not normalized:
        return normalized
    alias_map: Dict[str, str] = {}
    source_aliases = aliases or HEADER_ALIASES
    for canonical, raw_aliases in source_aliases.items():
        alias_map[normalize_header_key(canonical)] = canonical
        for alias in raw_aliases:
            alias_map[normalize_header_key(alias)] = canonical
    return alias_map.get(normalized, normalized)


def build_header_index(
    header_row: List[Any],
    *,
    alias_row: List[Any] | None = None,
    aliases: Mapping[str, List[str]] | None = None,
) -> Dict[str, int]:
    """Build a normalized header->column index map.
    If ``alias_row`` is provided, non-empty alias cells define logical names.
    """
    index: Dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        logical = cell
        if alias_row and idx < len(alias_row):
            alias_val = alias_row[idx]
            if str(alias_val or "").strip():
                logical = alias_val
        key = resolve_header_key(logical, aliases)
        if key and key not in index:
            index[key] = idx
    return index


def row_to_canonical_dict(
    raw_row: List[Any],
    *,
    canonical_headers: List[str],
    header_index: Mapping[str, int],
    pad_to_header_len: bool = True,
) -> Dict[str, Any]:
    """Project a raw row into a canonical dict using normalized header lookup."""
    max_idx = max(header_index.values(), default=-1)
    row = pad_row(raw_row, max_idx + 1) if pad_to_header_len else list(raw_row)
    out: Dict[str, Any] = {}
    for name in canonical_headers:
        idx = header_index.get(resolve_header_key(name))
        out[name] = row[idx] if idx is not None and idx < len(row) else None
    return out


def pad_row(row: List[Any], length: int) -> List[Any]:
    """Return a new list padded with ``None`` to exactly ``length`` items.
    ``row`` may be shorter (common when reading via the Sheets Values API).
    """
    if len(row) >= length:
        return row[:length]
    return row + [None] * (length - len(row))

def row_to_dict(padded_row: List[Any], headers: List[str]) -> Dict[str, Any]:
    """Convert a padded row list to a ``{header: value}`` dict.
    ``padded_row`` must have the same length as ``headers``.
    """
    return {h: v for h, v in zip(headers, padded_row)}

def dict_to_row(d: Mapping[str, Any], headers: List[str]) -> List[Any]:
    """Create an ordered row list from a dict according to ``headers``.
    Missing keys are emitted as ``None``.
    """
    return [d.get(h, None) for h in headers]

# ---------------------------------------------------------------------------
# Type coercion / validation helpers
# ---------------------------------------------------------------------------
def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        lowered = val.lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    raise ValueError(f"Cannot coerce {val!r} to bool")

def _coerce_int(val: Any) -> int:
    return int(val)

def _coerce_float(val: Any) -> float:
    return float(val)

def _coerce_timestamp(val: Any) -> str:
    if isinstance(val, (datetime.datetime, datetime.date)):
        dt = val if isinstance(val, datetime.datetime) else datetime.datetime.combine(val, datetime.time.min)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.isoformat()
    if isinstance(val, str):
        return val
    raise ValueError(f"Cannot coerce {val!r} to timestamp string")

def _coerce_json(val: Any) -> str:
    if isinstance(val, (dict, list)):
        return json.dumps(val)
    if isinstance(val, str):
        return val
    raise ValueError(f"Cannot coerce {val!r} to JSON string")

def coerce_value(value: Any, type_spec: str) -> Any:
    if value is None:
        return None
    if type_spec == "string":
        return str(value)
    if type_spec == "bool":
        return _coerce_bool(value)
    if type_spec in {"int", "number"}:
        return _coerce_int(value)
    if type_spec == "float":
        return _coerce_float(value)
    if type_spec == "timestamp":
        return _coerce_timestamp(value)
    if type_spec == "date":
        return str(value)
    if type_spec == "json":
        return _coerce_json(value)
    raise ValueError(f"Unsupported type spec '{type_spec}'")

# ---------------------------------------------------------------------------
# Fingerprint helper (deterministic hash for idempotency)
# ---------------------------------------------------------------------------
def compute_fingerprint(*parts: str) -> str:
    """Return a short SHA‑256 hex digest of the concatenated ``parts``.
    Used to generate ``source_fingerprint`` values.
    """
    hasher = hashlib.sha256()
    for p in parts:
        hasher.update(p.encode("utf-8"))
    return hasher.hexdigest()

# End of sheet_contract.py

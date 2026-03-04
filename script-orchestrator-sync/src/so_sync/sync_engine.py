"""Core sync execution engine.
Implements the workflow described in the contract:
1. Create or fetch a run ledger entry (idempotent).
2. Compute a deterministic source fingerprint.
3. Read Intake rows, apply mapping transforms, and write to the target sheet.
4. Update the run ledger with status, counters, and any error information.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import Any, Dict, List

from .gateway import SheetsGateway
from .repositories import (
    find_run_by_dedupe_key,
    read_mapping,
    read_sync_jobs,
    upsert_run,
    _read_tab as repo_read_tab,
)
from .sheet_contract import (
    compute_fingerprint,
    dict_to_row,
    normalize_header_key,
    pad_row,
    row_to_dict,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transform helpers (limited to the enum set defined in the contract)
# ---------------------------------------------------------------------------
def _apply_transform(value: Any, transform: str) -> Any:
    if value is None:
        return None
    if transform == "none" or not transform:
        return value
    if transform == "uppercase":
        return str(value).upper()
    if transform == "lowercase":
        return str(value).lower()
    if transform == "date_iso":
        try:
            dt = datetime.datetime.fromisoformat(str(value))
        except Exception:
            return value
        return dt.date().isoformat()
    if transform == "number_round":
        try:
            return round(float(value))
        except Exception:
            return value
    return value

# ---------------------------------------------------------------------------
# Core execution function
# ---------------------------------------------------------------------------
def execute_job(
    gateway: SheetsGateway,
    control_sheet_id: str,
    target_sheet_id: str,
    job_id: str,
    trigger: str,
    dedupe_key: str | None = None,
    *,
    skip_target_write: bool = False,
) -> Dict[str, Any]:
    """Run a sync job.
    Returns the run ledger row (after creation or retrieval).
    """
    # 1️⃣ Load job definition
    jobs = read_sync_jobs(gateway, control_sheet_id)
    job = next((j for j in jobs if j["job_id"] == job_id), None)
    if not job:
        raise ValueError(f"Job {job_id} not found or not enabled")

    # 2️⃣ Idempotency check via dedupe_key (if supplied)
    if dedupe_key:
        existing = find_run_by_dedupe_key(gateway, control_sheet_id, dedupe_key)
        if existing and str(existing.get("status", "")).lower() == "success":
            logger.info(
                "event=run_dedupe_hit job_id=%s dedupe_key=%s run_id=%s",
                job_id,
                dedupe_key,
                existing.get("run_id"),
            )
            existing["status_history"] = ["queued", "running", "success"]
            return existing

    # 3️⃣ Create a new run ledger entry (status queued)
    run_id = uuid.uuid4().hex[:32]
    now_iso = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    run_entry: Dict[str, Any] = {
        "run_id": run_id,
        "job_id": job_id,
        "trigger_ts": now_iso,
        "status": "queued",
        "started_ts": None,
        "finished_ts": None,
        "error_msg": None,
        "rows_processed": 0,
        "source_fingerprint": None,
        "dedupe_key": dedupe_key,
        "schema_version": 1,
    }
    status_history: List[str] = []

    def _persist_status(status: str) -> None:
        run_entry["status"] = status
        upsert_run(gateway, control_sheet_id, run_entry)
        status_history.append(status)
        logger.info(
            "event=run_status_update run_id=%s job_id=%s status=%s rows_processed=%s",
            run_entry["run_id"],
            run_entry["job_id"],
            status,
            run_entry.get("rows_processed"),
        )

    _persist_status("queued")
    logger.info(
        "event=run_start run_id=%s job_id=%s control_sheet_id=%s trigger=%s",
        run_id,
        job_id,
        control_sheet_id,
        trigger,
    )

    try:
        # 4️⃣ Compute source fingerprint (include mapping serialization)
        logger.info("event=run_read_mapping run_id=%s tab=Mapping", run_id)
        mappings = read_mapping(gateway, control_sheet_id, job_id)
        mapping_serial = json.dumps(mappings, sort_keys=True)
        source_fingerprint = compute_fingerprint(
            str(job["source_spreadsheet_id"]),
            str(job["source_tab_name"]),
            str(job.get("source_range", "")),
            mapping_serial,
        )
        run_entry["source_fingerprint"] = source_fingerprint
        run_entry["started_ts"] = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
        _persist_status("running")

        # 5️⃣ Load mapping rules (already loaded above)
        mapping_by_src: Dict[str, Dict[str, Any]] = {
            normalize_header_key(m["source_col"]): m for m in mappings if str(m.get("source_col") or "").strip()
        }
        if not mapping_by_src:
            raise ValueError(f"No mapping rows found for job_id={job_id}")

        # 6️⃣ Read source sheet
        has_alias_val = str(job.get("has_alias_row", "")).strip().lower()
        has_alias = has_alias_val in {"true", "1", "yes"}
        logger.info(
            "event=run_read_source run_id=%s sheet_id=%s tab=%s",
            run_id,
            job["source_spreadsheet_id"],
            job["source_tab_name"],
        )
        intake_header, intake_rows = repo_read_tab(
            gateway,
            str(job["source_spreadsheet_id"]),
            str(job["source_tab_name"]),
            has_alias,
        )
        padded_intake = [pad_row(r, len(intake_header)) for r in intake_rows]

        # 7️⃣ Prepare target sheet header information
        effective_target_sheet_id = str(job.get("target_spreadsheet_id") or target_sheet_id or "").strip()
        if not effective_target_sheet_id:
            raise ValueError("No target spreadsheet id provided in job config or execute_job target_sheet_id")
        logger.info(
            "event=run_read_target run_id=%s sheet_id=%s tab=%s",
            run_id,
            effective_target_sheet_id,
            job["target_tab_name"],
        )
        target_header, target_rows = repo_read_tab(
            gateway,
            effective_target_sheet_id,
            str(job["target_tab_name"]),
            has_alias,
        )
        target_key_col = job.get("target_key_column")
        target_key_norm = normalize_header_key(target_key_col) if target_key_col else None
        target_norm_to_idx = {normalize_header_key(h): i for i, h in enumerate(target_header)}
        key_idx = target_norm_to_idx.get(target_key_norm) if target_key_norm else None
        target_norm_to_name = {normalize_header_key(h): h for h in target_header}

        # 8️⃣ Transform each intake row according to mapping
        logger.info("event=run_map_rows run_id=%s row_count=%s", run_id, len(padded_intake))
        output_rows: List[List[Any]] = []
        processed = 0
        for raw_row in padded_intake:
            src_dict = row_to_dict(raw_row, intake_header)
            src_lookup = {normalize_header_key(k): v for k, v in src_dict.items()}
            out_dict: Dict[str, Any] = {h: None for h in target_header}
            for src_col_norm, rule in mapping_by_src.items():
                src_col = str(rule.get("source_col", ""))
                src_val = src_lookup.get(src_col_norm)
                if src_val is None or src_val == "":
                    on_missing = rule.get("on_missing", "fail")
                    if on_missing == "skip":
                        continue
                    if on_missing == "default":
                        src_val = rule.get("default_value")
                    if on_missing == "fail":
                        raise ValueError(f"Missing required source column {src_col} in row {src_dict}")
                if rule.get("trim", True):
                    src_val = str(src_val).strip()
                transformed = _apply_transform(src_val, rule.get("transform", "none"))
                target_col_raw = str(rule.get("target_col", "")).strip()
                target_col_name = target_norm_to_name.get(normalize_header_key(target_col_raw))
                if not target_col_name:
                    raise ValueError(
                        f"Target column '{target_col_raw}' from mapping not found in target header {target_header}"
                    )
                out_dict[target_col_name] = transformed
            output_rows.append(pad_row(dict_to_row(out_dict, target_header), len(target_header)))
            processed += 1

        # 9️⃣ Write to target sheet according to write_mode
        write_mode = job.get("write_mode", "append")
        logger.info("event=run_write_target run_id=%s write_mode=%s dry_run=%s", run_id, write_mode, skip_target_write)
        if not skip_target_write:
            if write_mode == "append":
                start_row = len(target_rows) + (2 if has_alias else 1) + 1
                range_addr = f"{_quote_tab(job['target_tab_name'])}!A{start_row}"
                payload = [{"range": range_addr, "values": output_rows}]
                gateway.write(effective_target_sheet_id, payload)
            elif write_mode == "upsert_by_key" and target_key_col:
                if key_idx is None:
                    raise ValueError(f"target_key_column '{target_key_col}' not found in target header")
                key_to_idx: Dict[str, int] = {}
                for i, r in enumerate(target_rows):
                    padded = pad_row(r, len(target_header))
                    key_val = padded[key_idx]
                    if key_val is not None:
                        key_to_idx[str(key_val)] = i
                updates: List[Dict[str, Any]] = []
                for out_row in output_rows:
                    key_val = out_row[key_idx]
                    if key_val is None:
                        continue
                    if str(key_val) in key_to_idx:
                        row_num = key_to_idx[str(key_val)] + (2 if has_alias else 1) + 1
                        range_addr = f"{_quote_tab(job['target_tab_name'])}!A{row_num}"
                        updates.append({"range": range_addr, "values": [out_row]})
                    else:
                        start_row = len(target_rows) + (2 if has_alias else 1) + 1
                        range_addr = f"{_quote_tab(job['target_tab_name'])}!A{start_row}"
                        updates.append({"range": range_addr, "values": [out_row]})
                        target_rows.append(out_row)
                if updates:
                    gateway.write(effective_target_sheet_id, updates)
            elif write_mode == "overwrite_range":
                target_range = job.get("target_range")
                if not target_range:
                    raise ValueError("overwrite_range requires a target_range definition in the job config")
                payload = [{"range": target_range, "values": output_rows}]
                gateway.write(effective_target_sheet_id, payload)
            else:
                raise ValueError(f"Unsupported write_mode: {write_mode}")

        # 10️⃣ Final ledger update
        run_entry["rows_processed"] = processed
        run_entry["finished_ts"] = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
        _persist_status("success")
        logger.info("event=run_finalize run_id=%s status=success rows_processed=%s", run_id, processed)
    except Exception as e:
        run_entry["error_msg"] = str(e)
        run_entry["finished_ts"] = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
        _persist_status("failed")
        logger.exception("event=run_finalize run_id=%s status=failed error=%s", run_id, e)
        raise

    run_entry["status_history"] = status_history
    return run_entry

def _quote_tab(tab_name: str) -> str:
    escaped = str(tab_name).replace("'", "''")
    return f"'{escaped}'"

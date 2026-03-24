#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Allow importing from ../src without installing a package
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from so_sync.gateway import SheetsGateway  # noqa: E402


# ----------------------------
# Env config
# ----------------------------
GATEWAY_BASE = os.environ.get(
    "SHEETS_JSON_BASE_URL",
    os.environ.get("SO_GATEWAY_BASE", "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app"),
).rstrip("/")
GATEWAY_KEY = os.environ.get("SHEETS_JSON_API_KEY", os.environ.get("SO_GATEWAY_KEY", "")).strip()

SHEET_INTAKE = os.environ.get("SHEET_INTAKE", "YOUR_GOOGLE_SHEET_ID")
INTAKE_TAB = os.environ.get("INTAKE_TAB", "mode")
INTAKE_ROW = int(os.environ.get("INTAKE_ROW", "2"))
FOUND_FOOTAGE_MODE_TAB = os.environ.get("FOUND_FOOTAGE_MODE_TAB", "found-footage-mode")
PERFORMANCE_MODE_TAB = os.environ.get("PERFORMANCE_MODE_TAB", "performance-footage-mode")
HYBRID_MODE_TAB = os.environ.get("HYBRID_MODE_TAB", "hybrid-mode")
MODE_CONTROL_TAB = os.environ.get("MODE_CONTROL_TAB", "mode").strip()

SHEET_PROD = os.environ.get("SHEET_PROD", "YOUR_GOOGLE_SHEET_ID")
PROD_TAB = os.environ.get("PROD_TAB", "SO Campaigns")
TARGET_CAMPAIGN_ID = os.environ.get("TARGET_CAMPAIGN_ID", "demo_campaign_001")
SO_CAMPAIGNS_READ_RANGE = "A1:ZZZ1000"

# Set APPLY=1 to actually write
APPLY = os.environ.get("APPLY", "0") == "1"


# ----------------------------
# Helpers
# ----------------------------
def col_to_a1(col_idx_0: int) -> str:
    n = col_idx_0 + 1
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_drive_id_from_url(url: str) -> Optional[str]:
    url = (url or "").strip()
    if not url:
        return None
    m = re.search(r"/d/([^/]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([^&]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"open\?id=([^&]+)", url)
    if m:
        return m.group(1)
    return None


def parse_drive_file_id(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("http"):
        return parse_drive_id_from_url(raw)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", raw):
        return raw
    return None


def parse_sheet_id_from_url(url: str) -> Optional[str]:
    url = (url or "").strip()
    if not url:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", url):
        return url
    m = re.search(r"/spreadsheets/d/([^/]+)", url)
    if m:
        return m.group(1)
    # fallback to generic drive patterns
    return parse_drive_id_from_url(url)


def first_nonempty_col(values: List[List[Any]]) -> Optional[str]:
    for row in values:
        if not row:
            continue
        v = "" if row[0] is None else str(row[0]).strip()
        if v:
            return v
    return None


def normalize_text_preset_name(raw: str) -> str:
    s = raw.strip()
    if s.endswith(".py"):
        s = s[:-3]
    if s.startswith("text_preset_"):
        s = s[len("text_preset_") :]
    return s


def to_bool(v: Any) -> bool:
    if v is True:
        return True
    if v is False:
        return False
    return str(v).strip().upper() == "TRUE"


def to_int(v: Any) -> Optional[int]:
    try:
        s = str(v).strip()
        if not s:
            return None
        return int(float(s))
    except Exception:
        return None


@dataclass
class Intake:
    number_of_videos: Optional[int] = None
    naming_convention: str = ""
    audio_url_or_text: str = ""
    audio_file_id: str = ""
    copy_srt_url_or_text: str = ""
    copy_srt_file_id: str = ""
    lyrics_srt_url_or_text: str = ""
    lyrics_srt_file_id: str = ""
    executive_preset_sheet_url: str = ""
    perf_included: bool = False
    performance_master_clip_ref: str = ""
    performance_clip_refs: Dict[str, str] = field(default_factory=dict)
    mode: str = ""
    source_tab: str = ""


def _read_named_row(gw: SheetsGateway, *, sheet_id: str, tab_name: str, row_num: int) -> dict[str, Any]:
    rng = f"'{tab_name}'!A1:AZ{row_num}"
    data = gw.read(sheet_id, [rng])
    values = data["valueRanges"][0].get("values", [])
    if len(values) < 2:
        raise RuntimeError(f"Intake range {rng} did not return header+row")
    header = values[0]
    row = values[-1]
    out: dict[str, Any] = {}
    for i, h in enumerate(header):
        key = str(h or "").strip()
        if not key:
            continue
        out[key] = row[i] if i < len(row) else ""
    return out


def _normalize_mode_label(value: str) -> str:
    out = str(value or "").strip().lower()
    out = out.replace("_", " ").replace("-", " ")
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _mode_to_tab_name(mode_value: str) -> str:
    normalized = _normalize_mode_label(mode_value)
    if not normalized:
        return ""
    if "hybrid" in normalized:
        return HYBRID_MODE_TAB
    if "performance" in normalized:
        return PERFORMANCE_MODE_TAB
    if "found" in normalized and "footage" in normalized:
        return FOUND_FOOTAGE_MODE_TAB
    if normalized in {"found footage", "found_footage"}:
        return FOUND_FOOTAGE_MODE_TAB
    return ""


def _mode_implies_performance(mode_value: str) -> bool:
    normalized = _normalize_mode_label(mode_value)
    if not normalized:
        return False
    return ("performance" in normalized) or ("hybrid" in normalized)


def _is_mode_control_tab(tab_name: str) -> bool:
    normalized = str(tab_name or "").strip().lower()
    if not normalized:
        return False
    configured = str(MODE_CONTROL_TAB or "").strip().lower() or "mode"
    return normalized in {configured, "api-read"}


def _first_nonempty(fields: dict[str, Any], names: list[str], default: str = "") -> str:
    for name in names:
        raw = str(fields.get(name) or "").strip()
        if raw:
            return raw
    return str(default or "").strip()


def _resolve_routed_intake_fields(gw: SheetsGateway) -> tuple[dict[str, Any], str, str]:
    control = _read_named_row(gw, sheet_id=SHEET_INTAKE, tab_name=INTAKE_TAB, row_num=INTAKE_ROW)
    mode_value = str(control.get("Mode") or "").strip()
    selected_tab = _mode_to_tab_name(mode_value)
    if _is_mode_control_tab(INTAKE_TAB):
        if not mode_value:
            raise RuntimeError(f"{INTAKE_TAB} intake row is missing required Mode value.")
        if not selected_tab:
            raise RuntimeError(
                f"Unsupported Mode value {mode_value!r}; expected found footage, performance, or hybrid."
            )
        routed = _read_named_row(gw, sheet_id=SHEET_INTAKE, tab_name=selected_tab, row_num=INTAKE_ROW)
        # Mode-tab values are authoritative when non-empty. Control tab provides fallback during migration.
        merged: dict[str, Any] = dict(control)
        for key, val in routed.items():
            if str(val or "").strip():
                merged[key] = val
            elif key not in merged:
                merged[key] = val
        merged["Mode"] = mode_value
        return merged, mode_value, selected_tab
    return control, mode_value, INTAKE_TAB


def read_intake_row(gw: SheetsGateway) -> Intake:
    fields, mode_value, selected_tab = _resolve_routed_intake_fields(gw)

    perf_clips: Dict[str, str] = {}
    for label in ("A", "B", "C", "D", "E", "F"):
        perf_clips[label.lower()] = _first_nonempty(
            fields,
            [
                f"Performance Clip {label} File ID",
                f"performance_clip_{label.lower()}_drive_file_id",
            ],
            default="",
        )

    exec_preset_ref = _first_nonempty(
        fields,
        ["Executive Preset File ID", "Executive Preset"],
        default="",
    )

    return Intake(
        number_of_videos=to_int(_first_nonempty(fields, ["Number of Videos"], default="")),
        naming_convention=_first_nonempty(fields, ["Naming Convention"], default=""),
        audio_url_or_text=_first_nonempty(fields, ["Audio File"], default=""),
        audio_file_id=_first_nonempty(fields, ["Audio File ID"], default=""),
        copy_srt_url_or_text=_first_nonempty(fields, ["copy .srt"], default=""),
        copy_srt_file_id=_first_nonempty(fields, ["copy. srt File ID", "copy .srt File ID"], default=""),
        lyrics_srt_url_or_text=_first_nonempty(fields, ["lyrics .srt"], default=""),
        lyrics_srt_file_id=_first_nonempty(fields, ["lyrics .srt File ID", "lyrics.srt File ID"], default=""),
        executive_preset_sheet_url=exec_preset_ref,
        perf_included=to_bool(
            _first_nonempty(
                fields,
                ["Performance Clips Included", "Performance Clips Included?"],
                default="",
            )
        ),
        performance_master_clip_ref=_first_nonempty(
            fields,
            ["Performance Master Clip File ID", "performance_master_clip_drive_file_id"],
            default="",
        ),
        performance_clip_refs=perf_clips,
        mode=mode_value,
        source_tab=selected_tab,
    )


def read_preset_sheet_firsts(
    gw: SheetsGateway, preset_sheet_url: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (stitch_preset_dsl, performance_preset_dsl, text_preset_name)
    using the first non-empty entries:
      - FF Presets column A
      - Performance Presets column B
      - Lyric Presets column C
    """
    preset_sheet_id = parse_sheet_id_from_url(preset_sheet_url)
    if not preset_sheet_id:
        return (None, None, None)

    data = gw.read(preset_sheet_id, ["Sheet1!A2:A200", "Sheet1!B2:B200", "Sheet1!C2:C200"])
    a_vals = data["valueRanges"][0].get("values", [])
    b_vals = data["valueRanges"][1].get("values", [])
    c_vals = data["valueRanges"][2].get("values", [])

    stitch = first_nonempty_col(a_vals)
    performance = first_nonempty_col(b_vals)
    lyric_raw = first_nonempty_col(c_vals)
    text_preset = normalize_text_preset_name(lyric_raw) if lyric_raw else None
    return (stitch, performance, text_preset)


def read_prod_campaigns(gw: SheetsGateway) -> Tuple[List[str], List[List[Any]]]:
    data = gw.read(SHEET_PROD, [f"{PROD_TAB}!{SO_CAMPAIGNS_READ_RANGE}"])
    values = data["valueRanges"][0].get("values", [])
    if not values:
        raise RuntimeError("Production SO Campaigns appears empty")
    header = [str(x).strip() for x in values[0]]
    rows = values[1:]
    return header, rows


def find_campaign_row_num(header: List[str], rows: List[List[Any]], campaign_id: str) -> int:
    if "campaign_id" not in header:
        raise RuntimeError("SO Campaigns missing campaign_id header")
    id_col = header.index("campaign_id")

    for sheet_row_num, r in enumerate(rows, start=2):  # header is row 1
        if id_col < len(r) and str(r[id_col]).strip() == campaign_id:
            return sheet_row_num
    raise RuntimeError(f"campaign_id not found: {campaign_id}")


def build_updates(
    header: List[str],
    target_row_num: int,
    intake: Intake,
    stitch_preset_dsl: Optional[str],
    performance_preset_dsl: Optional[str],
    text_preset_name: Optional[str],
) -> List[Dict[str, Any]]:
    col = {name: i for i, name in enumerate(header)}

    def set_cell(field: str, value: Any) -> Optional[Dict[str, Any]]:
        if field not in col:
            return None
        a1 = col_to_a1(col[field])
        return {"range": f"'{PROD_TAB}'!{a1}{target_row_num}", "values": [[value]]}

    # Audio
    audio_url = intake.audio_url_or_text
    audio_id = parse_drive_file_id(intake.audio_file_id or "") or parse_drive_file_id(audio_url)

    # SRT choice for v0: prefer lyrics; else copy; else blank.
    srt_url = intake.lyrics_srt_url_or_text or intake.copy_srt_url_or_text
    srt_id = (
        parse_drive_file_id(intake.lyrics_srt_file_id or "")
        or parse_drive_file_id(intake.copy_srt_file_id or "")
        or parse_drive_file_id(srt_url)
    )

    performance_enabled = bool(intake.perf_included or _mode_implies_performance(intake.mode))
    edit_mode = "FF_PLUS_PERF" if performance_enabled else "FF_ONLY"
    render_mode = "performance" if performance_enabled else "found_footage"
    perf_mode = ""
    perf_audio_policy = ""
    if render_mode == "performance":
        perf_mode = "preset" if str(performance_preset_dsl or "").strip() else "simple_sync_cut"
        perf_audio_policy = "master_only"

    notes_parts = []
    if intake.naming_convention:
        notes_parts.append(f"naming_convention={intake.naming_convention}")
    if intake.number_of_videos is not None:
        notes_parts.append(f"number_of_videos={intake.number_of_videos}")
    notes_parts.append(f"synced_from={intake.source_tab or INTAKE_TAB}")
    if intake.mode:
        notes_parts.append(f"mode={intake.mode}")
    notes = " | ".join(notes_parts)

    updates: List[Dict[str, Any]] = []
    for u in [
        set_cell("enabled", "TRUE"),
        set_cell("notes", notes),
        set_cell("audio_chip", audio_url),
        set_cell("audio_drive_file_id", audio_id or ""),
        set_cell("srt_chip", srt_url),
        set_cell("srt_drive_file_id", srt_id or ""),
        set_cell("stitch_preset_dsl", stitch_preset_dsl or ""),
        set_cell("text_preset_name", text_preset_name or ""),
        set_cell("edit_mode", edit_mode),
        set_cell("render_mode", render_mode),
        set_cell("performance_mode", perf_mode),
        set_cell("performance_audio_policy", perf_audio_policy),
        set_cell("performance_preset_dsl", performance_preset_dsl or ""),
    ]:
        if u is not None:
            updates.append(u)

    perf_master_id = parse_drive_file_id(intake.performance_master_clip_ref or "")
    if perf_master_id:
        u = set_cell("performance_master_clip_drive_file_id", perf_master_id)
        if u is not None:
            updates.append(u)

    for label in ("a", "b", "c", "d", "e", "f"):
        raw = str(intake.performance_clip_refs.get(label) or "").strip()
        clip_id = parse_drive_file_id(raw)
        if not clip_id:
            continue
        u = set_cell(f"performance_clip_{label}_drive_file_id", clip_id)
        if u is not None:
            updates.append(u)

    return updates


def main() -> int:
    gw = SheetsGateway(GATEWAY_BASE, GATEWAY_KEY)

    intake = read_intake_row(gw)
    stitch, perf_preset, text_preset = read_preset_sheet_firsts(gw, intake.executive_preset_sheet_url)

    header, rows = read_prod_campaigns(gw)
    target_row_num = find_campaign_row_num(header, rows, TARGET_CAMPAIGN_ID)

    updates = build_updates(header, target_row_num, intake, stitch, perf_preset, text_preset)

    print("=== DRY PLAN ===")
    print(f"Gateway: {GATEWAY_BASE}")
    print(
        f"Intake:  sheet={SHEET_INTAKE} tab={INTAKE_TAB} row={INTAKE_ROW} "
        f"mode={intake.mode or '(unset)'} source_tab={intake.source_tab or INTAKE_TAB}"
    )
    print(f"Prod:    sheet={SHEET_PROD} tab={PROD_TAB} target_campaign_id={TARGET_CAMPAIGN_ID} row={target_row_num}")
    print(
        "Derived: "
        f"stitch_preset_dsl={stitch!r} "
        f"performance_preset_dsl={perf_preset!r} "
        f"text_preset_name={text_preset!r}"
    )
    print("Updates:")
    for u in updates:
        print(f"  {u['range']}: {u['values'][0][0]!r}")

    if not APPLY:
        print("\nNo write performed. Set APPLY=1 to write.")
        return 0

    resp = gw.write(SHEET_PROD, updates)
    print("\nWRITE OK:", resp.get("ok"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

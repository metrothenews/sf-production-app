#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
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
INTAKE_TAB = os.environ.get("INTAKE_TAB", "Sheet2")
INTAKE_ROW = int(os.environ.get("INTAKE_ROW", "2"))

SHEET_PROD = os.environ.get("SHEET_PROD", "YOUR_GOOGLE_SHEET_ID")
PROD_TAB = os.environ.get("PROD_TAB", "SO Campaigns")
TARGET_CAMPAIGN_ID = os.environ.get("TARGET_CAMPAIGN_ID", "demo_campaign_001")

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


def parse_sheet_id_from_url(url: str) -> Optional[str]:
    url = (url or "").strip()
    if not url:
        return None
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
    number_of_videos: Optional[int]
    naming_convention: str
    audio_url_or_text: str
    copy_srt_url_or_text: str
    lyrics_srt_url_or_text: str
    executive_preset_sheet_url: str
    perf_included: bool


def read_intake_row(gw: SheetsGateway) -> Intake:
    rng = f"{INTAKE_TAB}!A1:AE{INTAKE_ROW}"
    data = gw.read(SHEET_INTAKE, [rng])
    values = data["valueRanges"][0].get("values", [])
    if len(values) < 2:
        raise RuntimeError(f"Intake range {rng} did not return header+row")

    header = values[0]
    row = values[-1]
    idx = {str(h).strip(): i for i, h in enumerate(header) if str(h).strip()}

    def get(name: str) -> Any:
        i = idx.get(name)
        if i is None or i >= len(row):
            return ""
        return row[i]

    return Intake(
        number_of_videos=to_int(get("Number of Videos")),
        naming_convention=str(get("Naming Convention") or "").strip(),
        audio_url_or_text=str(get("Audio File") or "").strip(),
        copy_srt_url_or_text=str(get("copy .srt") or "").strip(),
        lyrics_srt_url_or_text=str(get("lyrics .srt") or "").strip(),
        executive_preset_sheet_url=str(get("Executive Preset") or "").strip(),
        perf_included=to_bool(get("Performance Clips Included")),
    )


def read_preset_sheet_firsts(gw: SheetsGateway, preset_sheet_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (stitch_preset_dsl, text_preset_name) using the first non-empty entries:
      - FF Presets column A
      - Lyric Presets column C
    """
    preset_sheet_id = parse_sheet_id_from_url(preset_sheet_url)
    if not preset_sheet_id:
        return (None, None)

    data = gw.read(preset_sheet_id, ["Sheet1!A2:A200", "Sheet1!C2:C200"])
    a_vals = data["valueRanges"][0].get("values", [])
    c_vals = data["valueRanges"][1].get("values", [])

    stitch = first_nonempty_col(a_vals)
    lyric_raw = first_nonempty_col(c_vals)
    text_preset = normalize_text_preset_name(lyric_raw) if lyric_raw else None
    return (stitch, text_preset)


def read_prod_campaigns(gw: SheetsGateway) -> Tuple[List[str], List[List[Any]]]:
    data = gw.read(SHEET_PROD, [f"{PROD_TAB}!A1:AJ1000"])
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
    audio_id = parse_drive_id_from_url(audio_url) if audio_url.startswith("http") else None

    # SRT choice for v0: prefer lyrics; else copy; else blank.
    srt_url = intake.lyrics_srt_url_or_text or intake.copy_srt_url_or_text
    srt_id = parse_drive_id_from_url(srt_url) if (srt_url and srt_url.startswith("http")) else None

    edit_mode = "FF_PLUS_PERF" if intake.perf_included else "FF_ONLY"

    notes_parts = []
    if intake.naming_convention:
        notes_parts.append(f"naming_convention={intake.naming_convention}")
    if intake.number_of_videos is not None:
        notes_parts.append(f"number_of_videos={intake.number_of_videos}")
    notes_parts.append("synced_from=intake_sheet2")
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
    ]:
        if u is not None:
            updates.append(u)

    return updates


def main() -> int:
    gw = SheetsGateway(GATEWAY_BASE, GATEWAY_KEY)

    intake = read_intake_row(gw)
    stitch, text_preset = read_preset_sheet_firsts(gw, intake.executive_preset_sheet_url)

    header, rows = read_prod_campaigns(gw)
    target_row_num = find_campaign_row_num(header, rows, TARGET_CAMPAIGN_ID)

    updates = build_updates(header, target_row_num, intake, stitch, text_preset)

    print("=== DRY PLAN ===")
    print(f"Gateway: {GATEWAY_BASE}")
    print(f"Intake:  sheet={SHEET_INTAKE} tab={INTAKE_TAB} row={INTAKE_ROW}")
    print(f"Prod:    sheet={SHEET_PROD} tab={PROD_TAB} target_campaign_id={TARGET_CAMPAIGN_ID} row={target_row_num}")
    print(f"Derived: stitch_preset_dsl={stitch!r} text_preset_name={text_preset!r}")
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

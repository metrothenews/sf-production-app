from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compile_config_to_render_job.py"
_SPEC = importlib.util.spec_from_file_location("compile_config_to_render_job", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import compile script from {_SCRIPT_PATH}")
compiler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compiler)


def test_select_asset_drive_file_id_prefers_id_column_over_chip_name() -> None:
    cfg = {
        "Audio File": "bad news (explicit_v3).wav",
        "Audio File ID": "YOUR_GOOGLE_SHEET_ID",
        "lyrics .srt": "i feel bad lyrics v2.srt",
        "lyrics .srt File ID": "YOUR_GOOGLE_SHEET_ID",
    }
    audio = compiler.select_asset_drive_file_id(
        cfg,
        id_key="Audio File ID",
        chip_key="Audio File",
        label="Audio File",
        optional=False,
        assets_folder_id="",
        clips_library_folder_id="",
    )
    lyrics = compiler.select_asset_drive_file_id(
        cfg,
        id_key="lyrics .srt File ID",
        chip_key="lyrics .srt",
        label="lyrics .srt",
        optional=False,
        assets_folder_id="",
        clips_library_folder_id="",
    )
    assert audio == "YOUR_GOOGLE_SHEET_ID"
    assert lyrics == "YOUR_GOOGLE_SHEET_ID"

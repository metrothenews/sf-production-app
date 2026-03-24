from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_intake_to_so.py"
_SPEC = importlib.util.spec_from_file_location("sync_intake_to_so", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import sync script from {_SCRIPT_PATH}")
sync_mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sync_mod
_SPEC.loader.exec_module(sync_mod)


def _updates_to_field_map(updates: list[dict], header: list[str]) -> dict[str, str]:
    by_col = {sync_mod.col_to_a1(i): name for i, name in enumerate(header)}
    out: dict[str, str] = {}
    for row in updates:
        m = re.search(r"!([A-Z]+)\d+$", str(row.get("range") or ""))
        if not m:
            continue
        col = m.group(1)
        field = by_col.get(col)
        if not field:
            continue
        out[field] = str((row.get("values") or [[""]])[0][0] or "")
    return out


def test_read_preset_sheet_firsts_reads_performance_column() -> None:
    class _FakeGateway:
        def read(self, _sheet_id: str, _ranges: list[str]):
            return {
                "valueRanges": [
                    {"values": [["4a2br2cw"]]},
                    {"values": [["2a2b2c"]]},
                    {"values": [["text_preset_brat_words_manual.py"]]},
                ]
            }

    stitch, perf, text = sync_mod.read_preset_sheet_firsts(
        _FakeGateway(),
        "https://docs.google.com/spreadsheets/d/sheet123/edit#gid=0",
    )
    assert stitch == "4a2br2cw"
    assert perf == "2a2b2c"
    assert text == "brat_words_manual"


def test_parse_sheet_id_from_url_accepts_plain_id() -> None:
    assert sync_mod.parse_sheet_id_from_url("YOUR_GOOGLE_SHEET_ID") == "YOUR_GOOGLE_SHEET_ID"


def test_resolve_routed_intake_fields_reads_mode_tab_and_falls_back() -> None:
    class _FakeGateway:
        def read(self, _sheet_id: str, ranges: list[str]):
            rng = ranges[0]
            if "'mode'!" in rng:
                return {
                    "valueRanges": [
                        {
                            "values": [
                                ["Mode", "Audio File", "Audio File ID", "Executive Preset File ID", "Performance Clips Included"],
                                ["performance", "audio_from_api.wav", "audio_id_api_1234567890", "preset_sheet_id_1234567890", "FALSE"],
                            ]
                        }
                    ]
                }
            if "performance-footage-mode" in rng:
                return {
                    "valueRanges": [
                        {
                            "values": [
                                ["Audio File", "Audio File ID", "Number of Videos", "Naming Convention", "Performance Clips Included?"],
                                ["audio_from_mode.wav", "", "6", "Campaign X", "TRUE"],
                            ]
                        }
                    ]
                }
            raise AssertionError(f"Unexpected range {rng}")

    fields, mode, tab = sync_mod._resolve_routed_intake_fields(_FakeGateway())
    assert mode == "performance"
    assert tab == "performance-footage-mode"
    assert fields["Audio File"] == "audio_from_mode.wav"
    assert fields["Audio File ID"] == "audio_id_api_1234567890"
    assert fields["Performance Clips Included?"] == "TRUE"


def test_read_intake_row_prefers_explicit_id_fields() -> None:
    class _FakeGateway:
        def read(self, _sheet_id: str, ranges: list[str]):
            rng = ranges[0]
            if "'mode'!" in rng:
                return {
                    "valueRanges": [
                        {
                            "values": [
                                ["Mode", "Executive Preset File ID"],
                                ["found footage", "preset_sheet_id_1234567890"],
                            ]
                        }
                    ]
                }
            if "found-footage-mode" in rng:
                return {
                    "valueRanges": [
                        {
                            "values": [
                                [
                                    "Number of Videos",
                                    "Naming Convention",
                                    "Audio File",
                                    "Audio File ID",
                                    "lyrics .srt",
                                    "lyrics .srt File ID",
                                    "copy .srt",
                                    "copy. srt File ID",
                                    "Performance Clips Included?",
                                ],
                                [
                                    "4",
                                    "Campaign Y",
                                    "audio_name.wav",
                                    "audio_id_mode_1234567890",
                                    "lyrics_name.srt",
                                    "lyrics_id_mode_1234567890",
                                    "",
                                    "",
                                    "FALSE",
                                ],
                            ]
                        }
                    ]
                }
            raise AssertionError(f"Unexpected range {rng}")

    intake = sync_mod.read_intake_row(_FakeGateway())
    assert intake.mode == "found footage"
    assert intake.source_tab == "found-footage-mode"
    assert intake.audio_file_id == "audio_id_mode_1234567890"
    assert intake.lyrics_srt_file_id == "lyrics_id_mode_1234567890"
    assert intake.executive_preset_sheet_url == "preset_sheet_id_1234567890"
    assert intake.perf_included is False


def test_build_updates_dual_write_performance_mode_with_preset() -> None:
    header = [
        "campaign_id",
        "enabled",
        "notes",
        "audio_chip",
        "audio_drive_file_id",
        "srt_chip",
        "srt_drive_file_id",
        "stitch_preset_dsl",
        "text_preset_name",
        "edit_mode",
        "render_mode",
        "performance_mode",
        "performance_audio_policy",
        "performance_preset_dsl",
    ]
    intake = sync_mod.Intake(
        number_of_videos=5,
        naming_convention="Campaign A",
        audio_url_or_text="https://drive.google.com/file/d/audio_12345678901/view",
        audio_file_id="",
        copy_srt_url_or_text="",
        copy_srt_file_id="",
        lyrics_srt_url_or_text="https://drive.google.com/file/d/lyrics_1234567890/view",
        lyrics_srt_file_id="",
        executive_preset_sheet_url="https://docs.google.com/spreadsheets/d/preset_sheet/edit#gid=0",
        perf_included=True,
        performance_master_clip_ref="",
        performance_clip_refs={},
    )
    updates = sync_mod.build_updates(
        header,
        target_row_num=2,
        intake=intake,
        stitch_preset_dsl="4a4b4c",
        performance_preset_dsl="2a2b2c",
        text_preset_name="brat_words_manual",
    )
    mapped = _updates_to_field_map(updates, header)
    assert mapped["edit_mode"] == "FF_PLUS_PERF"
    assert mapped["render_mode"] == "performance"
    assert mapped["performance_mode"] == "preset"
    assert mapped["performance_audio_policy"] == "master_only"
    assert mapped["performance_preset_dsl"] == "2a2b2c"
    assert mapped["stitch_preset_dsl"] == "4a4b4c"
    assert mapped["text_preset_name"] == "brat_words_manual"


def test_build_updates_dual_write_found_footage_mode() -> None:
    header = [
        "campaign_id",
        "edit_mode",
        "render_mode",
        "performance_mode",
        "performance_audio_policy",
        "performance_preset_dsl",
    ]
    intake = sync_mod.Intake(
        number_of_videos=3,
        naming_convention="Campaign B",
        audio_url_or_text="",
        audio_file_id="",
        copy_srt_url_or_text="",
        copy_srt_file_id="",
        lyrics_srt_url_or_text="",
        lyrics_srt_file_id="",
        executive_preset_sheet_url="",
        perf_included=False,
        performance_master_clip_ref="",
        performance_clip_refs={},
    )
    updates = sync_mod.build_updates(
        header,
        target_row_num=2,
        intake=intake,
        stitch_preset_dsl=None,
        performance_preset_dsl=None,
        text_preset_name=None,
    )
    mapped = _updates_to_field_map(updates, header)
    assert mapped["edit_mode"] == "FF_ONLY"
    assert mapped["render_mode"] == "found_footage"
    assert mapped["performance_mode"] == ""
    assert mapped["performance_audio_policy"] == ""
    assert mapped["performance_preset_dsl"] == ""


def test_build_updates_performance_without_preset_defaults_simple_sync_cut() -> None:
    header = ["edit_mode", "render_mode", "performance_mode", "performance_audio_policy"]
    intake = sync_mod.Intake(
        number_of_videos=2,
        naming_convention="Campaign C",
        audio_url_or_text="",
        audio_file_id="",
        copy_srt_url_or_text="",
        copy_srt_file_id="",
        lyrics_srt_url_or_text="",
        lyrics_srt_file_id="",
        executive_preset_sheet_url="",
        perf_included=True,
        performance_master_clip_ref="",
        performance_clip_refs={},
    )
    updates = sync_mod.build_updates(
        header,
        target_row_num=2,
        intake=intake,
        stitch_preset_dsl=None,
        performance_preset_dsl=None,
        text_preset_name=None,
    )
    mapped = _updates_to_field_map(updates, header)
    assert mapped["render_mode"] == "performance"
    assert mapped["performance_mode"] == "simple_sync_cut"
    assert mapped["performance_audio_policy"] == "master_only"


def test_build_updates_mode_performance_overrides_legacy_flag() -> None:
    header = ["edit_mode", "render_mode", "performance_mode", "performance_audio_policy"]
    intake = sync_mod.Intake(
        number_of_videos=2,
        naming_convention="Campaign D",
        audio_url_or_text="",
        audio_file_id="",
        copy_srt_url_or_text="",
        copy_srt_file_id="",
        lyrics_srt_url_or_text="",
        lyrics_srt_file_id="",
        executive_preset_sheet_url="",
        perf_included=False,
        performance_master_clip_ref="",
        performance_clip_refs={},
        mode="performance",
    )
    updates = sync_mod.build_updates(
        header,
        target_row_num=2,
        intake=intake,
        stitch_preset_dsl=None,
        performance_preset_dsl=None,
        text_preset_name=None,
    )
    mapped = _updates_to_field_map(updates, header)
    assert mapped["edit_mode"] == "FF_PLUS_PERF"
    assert mapped["render_mode"] == "performance"
    assert mapped["performance_mode"] == "simple_sync_cut"
    assert mapped["performance_audio_policy"] == "master_only"


def test_read_prod_campaigns_uses_wide_range() -> None:
    class _FakeGateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[str]]] = []

        def read(self, sheet_id: str, ranges: list[str]):
            self.calls.append((sheet_id, ranges))
            return {"valueRanges": [{"values": [["campaign_id"], ["demo_campaign_001"]]}]}

    gw = _FakeGateway()
    header, rows = sync_mod.read_prod_campaigns(gw)

    assert header == ["campaign_id"]
    assert rows == [["demo_campaign_001"]]
    assert len(gw.calls) == 1
    sheet_id, ranges = gw.calls[0]
    assert sheet_id == sync_mod.SHEET_PROD
    assert ranges == [f"{sync_mod.PROD_TAB}!A1:ZZZ1000"]

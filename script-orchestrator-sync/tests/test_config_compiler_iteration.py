from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compile_config_to_render_job.py"
_SPEC = importlib.util.spec_from_file_location("compile_config_to_render_job", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import compile script from {_SCRIPT_PATH}")
compiler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compiler)


def test_required_run_count_ceil_division() -> None:
    assert compiler._required_run_count(12, 2) == 6
    assert compiler._required_run_count(13, 2) == 7
    with pytest.raises(RuntimeError, match=r"Number of Videos must be >= 1"):
        compiler._required_run_count(0, 2)


def test_render_job_id_for_iteration_suffixing() -> None:
    assert compiler._render_job_id_for_iteration("cfg_abc", 1, 1) == "cfg_abc"
    assert compiler._render_job_id_for_iteration("cfg_abc", 1, 6) == "cfg_abc_r01"
    assert compiler._render_job_id_for_iteration("cfg_abc", 6, 6) == "cfg_abc_r06"


def test_normalize_text_preset_name() -> None:
    assert compiler._normalize_text_preset_name("text_preset_brat_words_manual.py") == "brat_words_manual"
    assert compiler._normalize_text_preset_name("brat_words_manual") == "brat_words_manual"
    assert compiler._normalize_text_preset_name("") == ""


def test_resolve_text_preset_override_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TEXT_PRESET_OVERRIDE", "text_preset_brat_words_manual_lightgrey_narrow_pixelate_v007_option2.py")
    value, source = compiler._resolve_text_preset_override_from_cfg({})
    assert value == "brat_words_manual_lightgrey_narrow_pixelate_v007_option2"
    assert source == "env:TEXT_PRESET_OVERRIDE"


def test_resolve_text_preset_override_from_config() -> None:
    value, source = compiler._resolve_text_preset_override_from_cfg(
        {
            "Lyric Preset Override": "text_preset_brat_words_manual_lightgrey_narrow_pixelate_v007_option2.py",
        }
    )
    assert value == "brat_words_manual_lightgrey_narrow_pixelate_v007_option2"
    assert source == "config:lyric_text_preset_override"


def test_apply_text_preset_override_updates_preset_and_rows(capsys) -> None:
    rows = [
        {"stitch_preset": "4a4b4c", "text_preset": "old_a", "performance_preset": "4a4b4c"},
        {"stitch_preset": "4a4b4c", "text_preset": "old_b", "performance_preset": "4a4b4c"},
    ]
    preset = {"text_preset": "default_v007"}
    compiler._apply_text_preset_override(
        dynamic_preset_rows=rows,
        preset=preset,
        text_preset_override="brat_words_manual_lightgrey_narrow_pixelate_v007_option2",
        source="env:TEXT_PRESET_OVERRIDE",
    )
    assert preset["text_preset"] == "brat_words_manual_lightgrey_narrow_pixelate_v007_option2"
    assert rows[0]["text_preset"] == "brat_words_manual_lightgrey_narrow_pixelate_v007_option2"
    assert rows[1]["text_preset"] == "brat_words_manual_lightgrey_narrow_pixelate_v007_option2"
    out = capsys.readouterr().out
    assert "text_preset_override_applied" in out


def test_normalize_edit_mode_defaults_to_found_footage() -> None:
    assert compiler.normalize_edit_mode("") == "found_footage"
    assert compiler.normalize_edit_mode("performance") == "performance"
    with pytest.raises(RuntimeError, match=r"Invalid Render Mode"):
        compiler.normalize_edit_mode("nope")


def test_resolve_render_mode_from_cfg_uses_backfill_defaults() -> None:
    assert compiler._resolve_render_mode_from_cfg({"edit_mode": "FF_ONLY"}) == "found_footage"
    assert compiler._resolve_render_mode_from_cfg({"edit_mode": "FF_PLUS_PERF"}) == "performance"
    assert compiler._resolve_render_mode_from_cfg({"render_mode": "performance"}) == "performance"


def test_resolve_render_mode_from_cfg_falls_back_to_mode_column() -> None:
    assert compiler._resolve_render_mode_from_cfg({"Mode": "performance"}) == "performance"
    assert compiler._resolve_render_mode_from_cfg({"Mode": "hybrid"}) == "performance"
    assert compiler._resolve_render_mode_from_cfg({"Mode": "found footage"}) == "found_footage"


def test_normalize_performance_mode() -> None:
    assert compiler.normalize_performance_mode("simple_sync_cut") == "simple_sync_cut"
    assert compiler.normalize_performance_mode("preset") == "preset"
    with pytest.raises(RuntimeError, match=r"Invalid Performance Mode"):
        compiler.normalize_performance_mode("bad_mode")


def test_resolve_performance_mode_from_cfg_defaults_simple_sync_cut() -> None:
    assert compiler._resolve_performance_mode_from_cfg({}, render_mode="found_footage") == ""
    assert compiler._resolve_performance_mode_from_cfg({}, render_mode="performance") == "simple_sync_cut"
    assert (
        compiler._resolve_performance_mode_from_cfg({"performance_mode": "preset"}, render_mode="performance")
        == "preset"
    )


def test_resolve_performance_mode_from_cfg_honors_env_override(monkeypatch) -> None:
    monkeypatch.setenv("PERFORMANCE_MODE_OVERRIDE", "preset")
    assert compiler._resolve_performance_mode_from_cfg({}, render_mode="performance") == "preset"


def test_resolve_performance_preset_timing_mode_defaults_beat_cuts(monkeypatch) -> None:
    monkeypatch.delenv("PERFORMANCE_PRESET_TIMING_MODE", raising=False)
    assert compiler.resolve_performance_preset_timing_mode() == "beat_cuts"


def test_resolve_performance_preset_timing_mode_honors_env(monkeypatch) -> None:
    monkeypatch.setenv("PERFORMANCE_PRESET_TIMING_MODE", "normalized")
    assert compiler.resolve_performance_preset_timing_mode() == "normalized"


def test_resolve_performance_audio_policy_defaults_master_only() -> None:
    assert compiler._resolve_performance_audio_policy_from_cfg({}, render_mode="performance") == "master_only"
    assert (
        compiler._resolve_performance_audio_policy_from_cfg(
            {"performance_audio_policy": "master_only"},
            render_mode="performance",
        )
        == "master_only"
    )
    with pytest.raises(RuntimeError, match=r"Invalid performance audio policy"):
        compiler._resolve_performance_audio_policy_from_cfg(
            {"performance_audio_policy": "mixed"},
            render_mode="performance",
        )


def test_parse_performance_preset_tokens() -> None:
    out = compiler.parse_performance_preset_tokens("4a2br2cw")
    assert out[0] == {"duration": 4, "clip": "a", "modifiers": ""}
    assert out[1] == {"duration": 2, "clip": "b", "modifiers": "r"}
    assert out[2] == {"duration": 2, "clip": "c", "modifiers": "w"}
    out_decimal = compiler.parse_performance_preset_tokens("0.5a1.5br")
    assert out_decimal[0] == {"duration": 0.5, "clip": "a", "modifiers": ""}
    assert out_decimal[1] == {"duration": 1.5, "clip": "b", "modifiers": "r"}
    with pytest.raises(RuntimeError, match=r"Unsupported performance preset modifier"):
        compiler.parse_performance_preset_tokens("4az")


def test_performance_preset_field_alias_plural() -> None:
    cfg = {"Performance Presets": "4a2br2cw"}
    raw = compiler._cfg_value_by_names(cfg, compiler.PERFORMANCE_PRESET_FIELD_NAMES, default="")
    assert str(raw) == "4a2br2cw"


def test_parse_performance_clip_map() -> None:
    cfg = {
        "Performance Clip A File ID": "A" * 22,
        "Performance Clip B File ID": "B" * 22,
    }
    out = compiler._parse_performance_clip_map(cfg)
    assert out == {"a": "A" * 22, "b": "B" * 22}
    with pytest.raises(RuntimeError, match=r"must be a Drive file id"):
        compiler._parse_performance_clip_map({"Performance Clip A File ID": "not-an-id"})


def test_parse_performance_clip_map_reads_snake_case_aliases() -> None:
    cfg = {
        "performance_clip_a_drive_file_id": "A" * 22,
        "performance_clip_b_drive_file_id": "B" * 22,
    }
    out = compiler._parse_performance_clip_map(cfg)
    assert out == {"a": "A" * 22, "b": "B" * 22}


def test_parse_performance_clip_map_auto_resolves_from_source_folder(monkeypatch) -> None:
    monkeypatch.setenv("PERFORMANCE_PRESET_AUTO_CLIPS", "1")

    class _Drive:
        def list_files_in_folder(self, _folder_id: str):
            return [
                {"id": "B" * 22, "name": "clip_b.mp4", "mimeType": "video/mp4"},
                {"id": "A" * 22, "name": "clip_a.mp4", "mimeType": "video/mp4"},
                {"id": "Z" * 22, "name": "notes.txt", "mimeType": "text/plain"},
            ]

    out = compiler._parse_performance_clip_map(
        {},
        drive_client=_Drive(),
        source_folder_id="folder_abc",
        listing_cache={},
    )
    assert out == {"a": "A" * 22, "b": "B" * 22}


def test_config_range_default_covers_shifted_columns() -> None:
    assert compiler.CONFIG_RANGE_DEFAULT == "A1:ZZ2"


def test_config_tab_default_is_mode() -> None:
    assert compiler.CONFIG_TAB_DEFAULT == "mode"


def test_mode_to_tab_name() -> None:
    assert compiler._mode_to_tab_name("found footage") == "found-footage-mode"
    assert compiler._mode_to_tab_name("performance") == "performance-footage-mode"
    assert compiler._mode_to_tab_name("hybrid") == "hybrid-mode"
    assert compiler._mode_to_tab_name("unknown") == ""


def test_read_config_row_routes_mode_tab_with_control_fallback() -> None:
    class _FakeGateway:
        def read(self, _sheet_id: str, ranges: list[str]):
            rng = ranges[0]
            if "'mode'!A1:ZZ2" == rng:
                return {
                    "valueRanges": [
                        {
                            "values": [
                                ["Mode", "Render Mode", "Audio File ID"],
                                ["performance", "found_footage", "audio_id_from_api_1234567890"],
                            ]
                        }
                    ]
                }
            if "'performance-footage-mode'!A1:ZZ2" == rng:
                return {
                    "valueRanges": [
                        {
                            "values": [
                                ["Render Mode", "Number of Videos", "Naming Convention", "Audio File ID"],
                                ["performance", "6", "Campaign Z", ""],
                            ]
                        }
                    ]
                }
            raise AssertionError(f"Unexpected range {rng}")

    out = compiler._read_config_row(
        _FakeGateway(),
        "sheet_x",
        "mode",
        "A1:ZZ2",
    )
    assert out["Mode"] == "performance"
    assert out["_mode_source_tab"] == "performance-footage-mode"
    assert out["Render Mode"] == "performance"
    assert out["Number of Videos"] == "6"
    assert out["Audio File ID"] == "audio_id_from_api_1234567890"


def test_resolve_stitch_bpm_prefers_song_bpm_column() -> None:
    cfg = {"Song BPM": "136", "BPM": "120"}
    assert compiler.resolve_stitch_bpm(cfg) == 136.0


def test_resolve_song_title_overlay_enabled() -> None:
    cfg = {"Display Song Name?": "TRUE", "Song Name": "Track Name"}
    out = compiler.resolve_song_title_overlay(cfg)
    assert out == {"enabled": True, "text": "Track Name"}


def test_resolve_song_title_overlay_honors_disable_override(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY_SONG_NAME_OVERRIDE", "0")
    cfg = {"Display Song Name?": "TRUE", "Song Name": "Track Name"}
    out = compiler.resolve_song_title_overlay(cfg)
    assert out == {"enabled": False, "text": ""}


def test_resolve_song_title_overlay_requires_text_when_enabled() -> None:
    with pytest.raises(RuntimeError, match=r"Song Name is empty"):
        compiler.resolve_song_title_overlay({"Display Song Name?": "TRUE", "Song Name": ""})


def test_resolve_stitch_bpm_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_STITCH_BPM", "128")
    assert compiler.resolve_stitch_bpm({}) == 128.0


def test_resolve_stitch_bpm_rejects_invalid_value() -> None:
    import pytest

    with pytest.raises(RuntimeError, match=r"Invalid BPM value"):
        compiler.resolve_stitch_bpm({"Song BPM": "abc"})


def test_read_preset_rows_from_sheet1_layout() -> None:
    class _FakeGateway:
        def read(self, _sheet_id: str, _ranges: list[str]):
            return {
                "valueRanges": [
                    {
                        "values": [
                            ["FF Presets", "Performance Presets", "Lyric Presets", "Copy Presets"],
                            [
                                "1p1u1f1c1m1t",
                                "4a2br2cw",
                                "text_preset_brat_words_manual_lightgrey_narrow_pixelate_v007.py",
                                "",
                            ],
                            [],
                            ["2p2u2c2t2m2f", "", "", ""],
                        ]
                    }
                ]
            }

    rows = compiler._read_preset_rows(_FakeGateway(), "sheet_id", tab="Sheet1")
    assert rows == [
        {
            "stitch_preset": "1p1u1f1c1m1t",
            "text_preset": "brat_words_manual_lightgrey_narrow_pixelate_v007",
            "performance_preset": "4a2br2cw",
        },
        {
            "stitch_preset": "2p2u2c2t2m2f",
            "text_preset": "brat_words_manual_lightgrey_narrow_pixelate_v007",
            "performance_preset": "4a2br2cw",
        },
    ]


def test_autofill_performance_presets_column_when_all_blank() -> None:
    class _FakeGateway:
        def __init__(self) -> None:
            self.updates = []

        def read(self, _sheet_id: str, _ranges: list[str]):
            return {
                "valueRanges": [
                    {
                        "values": [
                            ["FF Presets", "Performance Presets", "Lyric Presets"],
                            ["4p4c4f4t2p2u2c2m", "", "text_preset_a.py"],
                            ["4p4sbc4f4t2sp2u2c2wm", "", "text_preset_b.py"],
                        ]
                    }
                ]
            }

        def write(self, _sheet_id: str, updates: list[dict[str, object]]):
            self.updates = list(updates)
            return {"ok": True}

    gw = _FakeGateway()
    filled = compiler._autofill_performance_presets_column(
        gw,
        "sheet_id",
        tab="Sheet1",
        fill_value="4a2br2cw",
    )
    assert filled == 2
    assert gw.updates == [
        {"range": "'Sheet1'!B2", "values": [["4a2br2cw"]]},
        {"range": "'Sheet1'!B3", "values": [["4a2br2cw"]]},
    ]


def test_autofill_performance_presets_column_skips_when_any_present() -> None:
    class _FakeGateway:
        def __init__(self) -> None:
            self.write_calls = 0

        def read(self, _sheet_id: str, _ranges: list[str]):
            return {
                "valueRanges": [
                    {
                        "values": [
                            ["FF Presets", "Performance Presets", "Lyric Presets"],
                            ["4p4c4f4t2p2u2c2m", "4a2br2cw", "text_preset_a.py"],
                            ["4p4sbc4f4t2sp2u2c2wm", "", "text_preset_b.py"],
                        ]
                    }
                ]
            }

        def write(self, _sheet_id: str, _updates: list[dict[str, object]]):
            self.write_calls += 1
            return {"ok": True}

    gw = _FakeGateway()
    filled = compiler._autofill_performance_presets_column(
        gw,
        "sheet_id",
        tab="Sheet1",
        fill_value="4a2br2cw",
    )
    assert filled == 0
    assert gw.write_calls == 0


def test_non_multiple_video_count_trims_last_iteration() -> None:
    cfg = {
        "Cut Clips?": "TRUE",
        "Clip 1": "TRUE",
        "Clip 1 Start": "00:00:00:00",
        "Clip 1 End": "00:00:02:00",
        "Clip 2": "TRUE",
        "Clip 2 Start": "00:00:02:00",
        "Clip 2 End": "00:00:04:00",
        "Clip 3": "TRUE",
        "Clip 3 Start": "00:00:04:00",
        "Clip 3 End": "00:00:06:00",
    }

    base_cuts = compiler.build_cuts_from_config(cfg, naming_convention="Example_000", start_index=1)
    clip_count = len(base_cuts)
    assert clip_count == 3

    number_of_videos = 17
    required_runs = compiler._required_run_count(number_of_videos, clip_count)
    assert required_runs == 6

    run_cut_counts: list[int] = []
    for i in range(required_runs):
        run_cuts = compiler.build_cuts_from_config(
            cfg,
            naming_convention="Example_000",
            start_index=(i * clip_count) + 1,
        )
        remaining = max(0, number_of_videos - (i * clip_count))
        if remaining > 0 and len(run_cuts) > remaining:
            run_cuts = run_cuts[:remaining]
        run_cut_counts.append(len(run_cuts))

    assert run_cut_counts == [3, 3, 3, 3, 3, 2]
    assert sum(run_cut_counts) == number_of_videos


def test_select_random_collection_pools_for_cuts_avoids_consecutive_repeats() -> None:
    pools = [
        {"collection_num": "1"},
        {"collection_num": "2"},
        {"collection_num": "3"},
        {"collection_num": "4"},
        {"collection_num": "5"},
        {"collection_num": "6"},
    ]
    seq_rows, _ = compiler._select_random_collection_pools_for_cuts(
        pools,
        cut_count=20,
        rng=random.Random(42),
        previous_collection_num="",
    )
    seq = [str(row["collection_num"]) for row in seq_rows]
    assert len(seq) == 20
    for idx in range(1, len(seq)):
        assert seq[idx] != seq[idx - 1]


def test_select_random_collection_pools_for_cuts_carries_previous_across_calls() -> None:
    pools = [{"collection_num": "1"}, {"collection_num": "2"}, {"collection_num": "3"}]
    rng = random.Random(7)
    first_rows, previous = compiler._select_random_collection_pools_for_cuts(
        pools,
        cut_count=5,
        rng=rng,
        previous_collection_num="",
    )
    second_rows, _ = compiler._select_random_collection_pools_for_cuts(
        pools,
        cut_count=5,
        rng=rng,
        previous_collection_num=previous,
    )
    assert first_rows
    assert second_rows
    assert first_rows[-1]["collection_num"] != second_rows[0]["collection_num"]


def test_select_random_collection_pools_for_cuts_errors_on_invalid_input() -> None:
    with pytest.raises(RuntimeError, match=r"No collection pools available"):
        compiler._select_random_collection_pools_for_cuts([], cut_count=1, rng=random.Random(1))
    with pytest.raises(RuntimeError, match=r"At least two"):
        compiler._select_random_collection_pools_for_cuts(
            [{"collection_num": "1"}],
            cut_count=2,
            rng=random.Random(1),
        )

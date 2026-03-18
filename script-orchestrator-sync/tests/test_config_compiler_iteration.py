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


def test_config_range_default_covers_shifted_columns() -> None:
    assert compiler.CONFIG_RANGE_DEFAULT == "A1:ZZ2"


def test_resolve_stitch_bpm_prefers_song_bpm_column() -> None:
    cfg = {"Song BPM": "136", "BPM": "120"}
    assert compiler.resolve_stitch_bpm(cfg) == 136.0


def test_resolve_song_title_overlay_enabled() -> None:
    cfg = {"Display Song Name?": "TRUE", "Song Name": "Track Name"}
    out = compiler.resolve_song_title_overlay(cfg)
    assert out == {"enabled": True, "text": "Track Name"}


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
                            ["1p1u1f1c1m1t", "", "text_preset_brat_words_manual_lightgrey_narrow_pixelate_v007.py", ""],
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
        },
        {
            "stitch_preset": "2p2u2c2t2m2f",
            "text_preset": "brat_words_manual_lightgrey_narrow_pixelate_v007",
        },
    ]


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

from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_build_ffmpeg_cut_command() -> None:
    cmd = render_agent.build_ffmpeg_cut_command(
        ffmpeg_bin="ffmpeg",
        input_path="/tmp/final.mp4",
        start="00:00:05.000",
        end="00:00:12.000",
        output_path="/tmp/short_01.mp4",
    )
    assert cmd == [
        "ffmpeg",
        "-y",
        "-ss",
        "00:00:05.000",
        "-to",
        "00:00:12.000",
        "-i",
        "/tmp/final.mp4",
        "-c",
        "copy",
        "/tmp/short_01.mp4",
    ]


def test_build_ffmpeg_audio_trim_command() -> None:
    cmd = render_agent.build_ffmpeg_audio_trim_command(
        ffmpeg_bin="ffmpeg",
        input_path="/tmp/full.wav",
        start="00:00:05.000",
        end="00:00:12.000",
        output_path="/tmp/cut.wav",
    )
    assert cmd == [
        "ffmpeg",
        "-y",
        "-i",
        "/tmp/full.wav",
        "-af",
        "atrim=start=5.000:end=12.000,asetpts=PTS-STARTPTS",
        "-vn",
        "-acodec",
        "pcm_s16le",
        "/tmp/cut.wav",
    ]


def test_build_ffmpeg_master_audio_cut_command() -> None:
    cmd = render_agent.build_ffmpeg_master_audio_cut_command(
        ffmpeg_bin="ffmpeg",
        video_input_path="/tmp/master.mp4",
        audio_input_path="/tmp/master.wav",
        start="00:00:05.000",
        end="00:00:12.000",
        output_path="/tmp/cut.mp4",
        fps=30,
        aspect="9:16",
    )
    assert cmd[0:6] == ["ffmpeg", "-y", "-i", "/tmp/master.mp4", "-i", "/tmp/master.wav"]
    assert "-filter_complex" in cmd
    filter_idx = cmd.index("-filter_complex")
    assert "trim=start=5.000:end=12.000" in cmd[filter_idx + 1]
    assert "atrim=start=5.000:end=12.000" in cmd[filter_idx + 1]
    assert "scale=1080:1920" in cmd[filter_idx + 1]
    assert "-map" in cmd
    assert "[v]" in cmd
    assert "[a]" in cmd
    assert "-maxrate" in cmd
    assert "-bufsize" in cmd
    assert cmd[-1] == "/tmp/cut.mp4"


def test_build_vse_text_command_includes_auto_baseline_override() -> None:
    cmd = render_agent.build_vse_text_command(
        {
            "input_video_path": "/tmp/in.mp4",
            "input_srt_path": "/tmp/in.srt",
            "input_audio_path": "/tmp/in.wav",
            "fps": "30",
            "aspect": "9:16",
            "preset": "p",
            "bg_mode": "none",
            "output_local_path": "/tmp/out.mp4",
            "render_end_seconds": 15.0,
            "auto_baseline_wps": 4.989123,
        },
        blender_bin="blender",
        script_path="/tmp/vse_text.py",
    )
    assert "--auto-baseline-wps" in cmd
    i = cmd.index("--auto-baseline-wps")
    assert cmd[i + 1] == "4.989123"


def test_build_vse_text_command_includes_song_title_when_enabled() -> None:
    cmd = render_agent.build_vse_text_command(
        {
            "input_video_path": "/tmp/in.mp4",
            "input_srt_path": "/tmp/in.srt",
            "input_audio_path": "/tmp/in.wav",
            "fps": "30",
            "aspect": "9:16",
            "preset": "p",
            "bg_mode": "none",
            "output_local_path": "/tmp/out.mp4",
            "display_song_name": "TRUE",
            "song_name": "My Song",
        },
        blender_bin="blender",
        script_path="/tmp/vse_text.py",
    )
    assert "--song-title" in cmd
    i = cmd.index("--song-title")
    assert cmd[i + 1] == "My Song"


def test_extract_reveal_auto_speed_baseline_from_log(tmp_path) -> None:
    log = tmp_path / "lyrics.log"
    log.write_text(
        "\n".join(
            [
                "foo",
                "reveal_auto_speed_baseline baseline_wps=4.989 source=computed quantile=0.700",
                "bar",
            ]
        ),
        encoding="utf-8",
    )
    assert render_agent._extract_reveal_auto_speed_baseline_from_log(str(log)) == 4.989


def test_slice_and_shift_srt_file(tmp_path) -> None:
    src = tmp_path / "in.srt"
    src.write_text(
        "\n".join(
            [
                "1",
                "00:00:01,000 --> 00:00:03,000",
                "hello",
                "",
                "2",
                "00:00:04,000 --> 00:00:06,000",
                "world",
                "",
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.srt"
    had_rows = render_agent._slice_and_shift_srt_file(
        input_path=str(src),
        output_path=str(out),
        start_s=2.0,
        end_s=5.0,
    )
    assert had_rows is True
    assert out.read_text(encoding="utf-8").strip().splitlines() == [
        "1",
        "00:00:00,000 --> 00:00:01,000",
        "hello",
        "",
        "2",
        "00:00:02,000 --> 00:00:03,000",
        "world",
    ]


def test_slice_and_shift_srt_file_autofixes_resolve_hour_offset(tmp_path) -> None:
    src = tmp_path / "in_resolve_hour.srt"
    src.write_text(
        "\n".join(
            [
                "1",
                "01:00:01,000 --> 01:00:03,000",
                "hello",
                "",
                "2",
                "01:00:04,000 --> 01:00:06,000",
                "world",
                "",
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.srt"
    had_rows = render_agent._slice_and_shift_srt_file(
        input_path=str(src),
        output_path=str(out),
        start_s=2.0,
        end_s=5.0,
    )
    assert had_rows is True
    assert out.read_text(encoding="utf-8").strip().splitlines() == [
        "1",
        "00:00:00,000 --> 00:00:01,000",
        "hello",
        "",
        "2",
        "00:00:02,000 --> 00:00:03,000",
        "world",
    ]


def test_slice_and_shift_srt_file_keeps_boundary_touching_cue(tmp_path) -> None:
    src = tmp_path / "in_boundary.srt"
    src.write_text(
        "\n".join(
            [
                "1",
                "00:00:01,000 --> 00:00:02,000",
                "touch-start",
                "",
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out_boundary.srt"
    had_rows = render_agent._slice_and_shift_srt_file(
        input_path=str(src),
        output_path=str(out),
        start_s=2.0,
        end_s=5.0,
    )
    assert had_rows is True
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:00,001"
    assert lines[2] == "touch-start"


def test_count_srt_cues_counts_valid_time_ranges(tmp_path) -> None:
    src = tmp_path / "in.srt"
    src.write_text(
        "\n".join(
            [
                "1",
                "00:00:01,000 --> 00:00:03,000",
                "hello",
                "",
                "2",
                "00:00:04.000 --> 00:00:06.000",
                "world",
                "",
                "bad",
                "not-a-time --> nope",
                "ignored",
            ]
        ),
        encoding="utf-8",
    )
    assert render_agent._count_srt_cues(str(src)) == 2


def test_require_nonempty_srt_raises_when_empty(tmp_path, monkeypatch) -> None:
    src = tmp_path / "empty.srt"
    src.write_text("", encoding="utf-8")
    monkeypatch.delenv("ALLOW_EMPTY_LYRICS_SRT", raising=False)
    try:
        render_agent._require_nonempty_srt(str(src), label="unit_test")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "zero subtitle cues" in str(exc)


def test_require_nonempty_srt_can_be_overridden(tmp_path, monkeypatch) -> None:
    src = tmp_path / "empty.srt"
    src.write_text("", encoding="utf-8")
    monkeypatch.setenv("ALLOW_EMPTY_LYRICS_SRT", "1")
    assert render_agent._require_nonempty_srt(str(src), label="unit_test") == 0


def test_pipeline_per_cut_render_enabled_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("PIPELINE_PER_CUT_RENDER", raising=False)
    assert render_agent._pipeline_per_cut_render_enabled() is True
    monkeypatch.setenv("PIPELINE_PER_CUT_RENDER", "1")
    assert render_agent._pipeline_per_cut_render_enabled() is True


def test_should_fallback_to_legacy_full_render(monkeypatch) -> None:
    monkeypatch.delenv("ALLOW_EMPTY_LYRICS_SRT", raising=False)
    assert (
        render_agent._should_fallback_to_legacy_full_render(
            main_lyrics_cues=12,
            cuts_with_lyrics=0,
        )
        is True
    )
    assert (
        render_agent._should_fallback_to_legacy_full_render(
            main_lyrics_cues=12,
            cuts_with_lyrics=1,
        )
        is False
    )
    monkeypatch.setenv("ALLOW_EMPTY_LYRICS_SRT", "1")
    assert (
        render_agent._should_fallback_to_legacy_full_render(
            main_lyrics_cues=12,
            cuts_with_lyrics=0,
        )
        is False
    )


def test_pipeline_effects_pass2_enabled_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("PIPELINE_EFFECTS_PASS2_ENABLED", raising=False)
    assert render_agent._pipeline_effects_pass2_enabled() is True
    monkeypatch.setenv("PIPELINE_EFFECTS_PASS2_ENABLED", "0")
    assert render_agent._pipeline_effects_pass2_enabled() is False


def test_assert_expected_cut_output_passes_when_within_tolerance(monkeypatch) -> None:
    monkeypatch.setattr(render_agent, "_probe_media_duration_seconds", lambda _p: 10.03)
    monkeypatch.setattr(render_agent, "_probe_media_video_frame_count", lambda _p: 300)
    render_agent._assert_expected_cut_output(
        output_path="/tmp/out.mp4",
        expected_duration_s=10.0,
        expected_frames=300,
        duration_tolerance_s=0.2,
        frame_tolerance=2,
    )


def test_assert_expected_cut_output_raises_on_short_duration(monkeypatch) -> None:
    monkeypatch.setattr(render_agent, "_probe_media_duration_seconds", lambda _p: 9.0)
    monkeypatch.setattr(render_agent, "_probe_media_video_frame_count", lambda _p: 300)
    try:
        render_agent._assert_expected_cut_output(
            output_path="/tmp/out.mp4",
            expected_duration_s=10.0,
            expected_frames=300,
            duration_tolerance_s=0.2,
            frame_tolerance=2,
        )
        assert False, "expected duration gate failure"
    except RuntimeError as exc:
        assert "duration gate failed" in str(exc)

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compile_config_to_render_job.py"
_SPEC = importlib.util.spec_from_file_location("compile_config_to_render_job", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import compile script from {_SCRIPT_PATH}")
compiler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compiler)


def _prepare_common(monkeypatch, *, stitch_preset: str):
    monkeypatch.setenv("CONTROL_SHEET_ID", "sheet_1")
    monkeypatch.setenv("SHEETS_JSON_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("CLIPS_LIBRARY_SOURCE", "gcs_mirror")
    monkeypatch.setenv("CLIPS_LIBRARY_MIRROR_BUCKET", "mirror-bucket")
    monkeypatch.setenv("CLIPS_LIBRARY_MIRROR_PREFIX", "library-mirror")

    class _FakeGateway:
        def __init__(self, *args, **kwargs):
            _ = args
            _ = kwargs

    class _FakeDrive:
        def list_files_in_folder(self, folder_id: str):
            _ = folder_id
            return []

    monkeypatch.setattr(compiler, "SheetsGateway", _FakeGateway)
    monkeypatch.setattr(compiler, "drive_upload_from_env", lambda: _FakeDrive())
    monkeypatch.setattr(compiler, "ensure_drive_upload_credentials", lambda: "")
    monkeypatch.setattr(
        compiler,
        "_read_config_row",
        lambda gateway, config_sheet_id, config_tab, config_range: {
            "Naming Convention": "campaign_001",
            "Number of Videos": "1",
            "Executive Preset": "exec_preset_1",
            "Audio File ID": "audio_id",
            "lyrics .srt File ID": "lyrics_id",
            "copy. srt File ID": "",
            "Executive Preset File ID": "",
        },
    )
    monkeypatch.setattr(
        compiler,
        "build_cuts_from_config",
        lambda cfg, *, naming_convention, start_index=1, fps=30: [
            {"id": "clip_1", "start": "00:00:00.000", "end": "00:00:05.000", "filename": "short_001.mp4"}
        ],
    )
    monkeypatch.setattr(
        compiler,
        "select_asset_drive_file_id",
        lambda cfg, *, id_key, chip_key, label, optional, assets_folder_id, clips_library_folder_id: str(
            cfg.get(id_key) or ""
        ),
    )
    monkeypatch.setattr(
        compiler,
        "EXEC_PRESET_MAP",
        {
            "exec_preset_1": {
                "stitch_preset": stitch_preset,
                "text_preset": "brat_words_manual_lightgrey_narrow_pixelate_v007",
                "fps": 30,
                "aspect": "9:16",
                "bg_mode": "none",
                "seed": 42,
            }
        },
    )
    monkeypatch.setattr(
        compiler,
        "_load_collection_pools_from_gcs_mirror",
        lambda *, bucket, prefix: (
            [
                {
                    "collection_num": "5",
                    "folder_id": "",
                    "files": [
                        {
                            "id": "clip_p_1",
                            "name": "people_1.mp4",
                            "mimeType": "video/mp4",
                            "size": "10",
                            "category": "P",
                            "collection_num": "5",
                            "gcs_uri": "gs://mirror-bucket/library-mirror/clips/collection-5/P/clip_p_1__people_1.mp4",
                            "gcs_object": "library-mirror/clips/collection-5/P/clip_p_1__people_1.mp4",
                        }
                    ],
                }
            ],
            "library-mirror/indexes/library_mirror_index.json",
            {"generated_utc": "2026-01-01T00:00:00+00:00", "total_clips": 1},
        ),
    )
    monkeypatch.setattr(compiler, "_upload_manifest", lambda manifest, *, naming_convention, output_parent_folder_id: "manifest_1")
    monkeypatch.setattr(compiler, "find_render_job_by_id", lambda gw, control_sheet_id, render_job_id: {})


def test_gcs_mirror_stale_warning_emitted(monkeypatch, capsys) -> None:
    _prepare_common(monkeypatch, stitch_preset="4p")
    monkeypatch.setenv("CLIPS_LIBRARY_MIRROR_WARN_STALE_HOURS", "1")
    calls = {"upsert": 0}

    def _fake_upsert(gw, control_sheet_id, row):
        _ = gw
        _ = control_sheet_id
        _ = row
        calls["upsert"] += 1

    monkeypatch.setattr(compiler, "upsert_render_job", _fake_upsert)

    rc = compiler.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert calls["upsert"] == 1
    assert "mirror_freshness_warn" in out


def test_gcs_mirror_fails_before_queue_when_category_missing(monkeypatch) -> None:
    _prepare_common(monkeypatch, stitch_preset="4m")
    calls = {"upsert": 0}
    monkeypatch.setattr(compiler, "upsert_render_job", lambda gw, control_sheet_id, row: calls.__setitem__("upsert", calls["upsert"] + 1))
    with pytest.raises(RuntimeError, match=r"Mirror category coverage check failed"):
        compiler.main()
    assert calls["upsert"] == 0


def test_clips_source_mode_defaults_to_drive_without_explicit_flag(monkeypatch) -> None:
    monkeypatch.delenv("CLIPS_LIBRARY_SOURCE", raising=False)
    monkeypatch.setenv("CLIPS_LIBRARY_MIRROR_BUCKET", "mirror-bucket")
    assert compiler._clips_source_mode() == "drive"

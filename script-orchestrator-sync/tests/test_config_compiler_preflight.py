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


def test_preflight_requires_google_application_credentials(monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(Exception, match=r"GOOGLE_APPLICATION_CREDENTIALS is required"):
        compiler.ensure_drive_upload_credentials()


def test_main_missing_creds_does_not_call_upsert(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_SHEET_ID", "sheet_1")
    monkeypatch.setenv("SHEETS_JSON_BASE_URL", "https://example.invalid")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    called = {"upsert": 0}

    def _fake_upsert(*args, **kwargs):
        called["upsert"] += 1

    monkeypatch.setattr(compiler, "upsert_render_job", _fake_upsert)
    with pytest.raises(Exception, match=r"GOOGLE_APPLICATION_CREDENTIALS is required"):
        compiler.main()
    assert called["upsert"] == 0


def test_build_render_job_upsert_payload_sets_queued_and_clears_lease() -> None:
    existing = {
        "priority": "7",
        "output_filename": "x.mp4",
        "attempts": "1",
        "max_attempts": "3",
        "created_utc": "2026-03-03T00:00:00+00:00",
        "lease_owner": "h",
        "lease_until_utc": "x",
        "run_id": "r",
        "error_msg": "err",
    }
    preset = {
        "stitch_preset": "4a4b4c",
        "text_preset": "preset_x",
        "seed": 42,
        "fps": 30,
        "aspect": "9:16",
        "bg_mode": "none",
    }
    row = compiler.build_render_job_upsert_payload(
        existing,
        render_job_id="cfg_1",
        naming_convention="camp",
        clips_library_folder_id="clips_folder",
        manifest_drive_file_id="manifest_id",
        audio_id="audio_id",
        lyrics_id="lyrics_id",
        copy_id="",
        preset=preset,
        output_parent_folder_id="out_parent",
        now_iso="2026-03-03T01:00:00+00:00",
    )
    assert row["status"] == "queued"
    assert row["enabled"] == "TRUE"
    assert row["lease_owner"] == ""
    assert row["lease_until_utc"] == ""
    assert row["run_id"] == ""
    assert row["error_msg"] == ""
    assert row["attempts"] == "0"
    assert row["max_attempts"] == "3"
